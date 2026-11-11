"""Convert verified external dataset samples into harness task directories.

The input is a local directory of small, pre-verified JSONL samples (see
``eval/external/LICENSES.md`` for provenance).  The output is one task tree per
dataset below ``eval/external/<dataset>/``; every task follows the harness
contract (``task.json`` + optional ``workspace/``).

The conversion is deterministic and network-free.  It only reads the named
sample files, never the full upstream datasets, and it never copies reference
solutions into a task workspace:

* MBPP: the reference ``code`` stays in ``_source/``; the workspace gets
  ``NotImplementedError`` stubs plus the generated tests.
* QuixBugs: only ``buggy_code`` is written; ``correct_code`` is dropped.

Usage::

    python3.11 eval/import_datasets.py
    python3.11 eval/import_datasets.py --src /tmp/external-datasets
    python3.11 eval/import_datasets.py --out /tmp/external-tasks

After conversion, run the bundled verification snippets documented in
``eval/external/README.md``.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import csv
import json
import logging
import re
import shutil
import sys
import textwrap
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = Path("/tmp/external-datasets")
DEFAULT_OUT = REPO_ROOT / "eval" / "external"

#: Names of the generated dataset directories and their source sample files.
SAMPLE_FILES: Mapping[str, str] = {
    "mbpp": "mbpp_sample.jsonl",
    "quixbugs": "quixbugs_sample.jsonl",
    "gsm8k": "gsm8k_sample.jsonl",
    "wikitable": "wtq_sample.jsonl",
    "hotpotqa": "hotpotqa_sample.jsonl",
}

#: Tasks requested by the conversion brief.
TASK_LIMITS: Mapping[str, int] = {
    "mbpp": 15,
    "quixbugs": 10,
    "gsm8k": 15,
    "wikitable": 15,
    "hotpotqa": 10,
}

REWARDS: Mapping[str, int] = {
    "mbpp": 2,
    "quixbugs": 2,
    "gsm8k": 3,
    "wikitable": 3,
    "hotpotqa": 3,
}

_BUILTIN_NAMES = frozenset(dir(builtins)) | {"__import__"}
_INT_RE = re.compile(r"[+-]?\d+")
_WHITESPACE_RE = re.compile(r"\s+")

log = logging.getLogger("import_datasets")


# -- generic helpers ----------------------------------------------------------
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise TypeError(f"{path}:{line_no}: expected a JSON object")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: sample is empty")
    return rows


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _task_payload(
    *,
    task_id: str,
    task_type: str,
    instruction: str,
    reward: int,
    expected: str,
    check: str,
) -> dict[str, Any]:
    """Build ``task.json`` with exactly the six contract fields, in order."""

    return {
        "task_id": task_id,
        "task_type": task_type,
        "instruction": instruction,
        "reward": reward,
        "expected": expected,
        "check": check,
    }


def _reset_generated_task_dirs(dataset_dir: Path, dataset: str) -> None:
    prefix = f"ext_{dataset}_"
    if not dataset_dir.is_dir():
        dataset_dir.mkdir(parents=True)
        return
    for child in sorted(dataset_dir.iterdir()):
        if child.is_dir() and child.name.startswith(prefix):
            shutil.rmtree(child)


def _new_task_dir(dataset_dir: Path, dataset: str, index: int) -> tuple[Path, str]:
    task_id = f"ext_{dataset}_{index}"
    task_dir = dataset_dir / task_id
    if task_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing task: {task_dir}")
    task_dir.mkdir(parents=True)
    return task_dir, task_id


def _copy_source(src_dir: Path, dataset_dir: Path, sample_file: str) -> Path:
    source_dir = dataset_dir / "_source"
    if source_dir.exists():
        shutil.rmtree(source_dir)
    source_dir.mkdir(parents=True)
    destination = source_dir / sample_file
    shutil.copyfile(src_dir / sample_file, destination)
    return destination


def _strip_caches(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
        elif path.is_file() and path.suffix in {".pyc", ".pyo"}:
            path.unlink()


def _normalize_answer(value: object) -> str:
    """NFKC + whitespace-collapsed answer text.

    The same normalization is applied to WTQ expectations and is a superset of
    the plain ``strip()`` used by the generic solver, so exact-string checks
    survive HTML entities and non-breaking spaces in the source tables.
    """

    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


# -- MBPP ---------------------------------------------------------------------
def _top_level_defs(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _called_function_names(test_list: Sequence[str]) -> list[str]:
    """Return non-builtin function names called by the test asserts, in order."""

    names: list[str] = []
    for test in test_list:
        source = str(test).strip()
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise ValueError(f"MBPP test is not valid Python: {source!r}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
                if name not in _BUILTIN_NAMES:
                    names.append(name)
    return _ordered_unique(names)


def _positional_arity(test_list: Sequence[str], name: str) -> tuple[int, bool]:
    """Largest positional arity used for ``name`` and whether calls are simple."""

    arities: list[int] = []
    simple = True
    for test in test_list:
        try:
            tree = ast.parse(str(test).strip())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            ):
                continue
            if node.keywords:
                simple = False
            if any(isinstance(arg, ast.Starred) for arg in node.args):
                simple = False
            arities.append(len(node.args))
    if not arities:
        return 0, False
    return max(arities), simple


def _stub_source(name: str, test_list: Sequence[str]) -> str:
    arity, simple = _positional_arity(test_list, name)
    signature = (
        ", ".join(f"arg{index}" for index in range(arity))
        if simple and arity > 0
        else "*args, **kwargs"
    )
    if simple and arity == 0:
        signature = ""
    return (
        f"def {name}({signature}):\n"
        f'    """TODO: implement {name} so the generated tests pass."""\n'
        f"    raise NotImplementedError({name!r})\n"
    )


def _mbpp_targets(row: Mapping[str, Any]) -> list[str]:
    test_list = [str(item) for item in row.get("test_list") or []]
    defs = _top_level_defs(str(row.get("code") or ""))
    called = _called_function_names(test_list)
    if called:
        targets = called
    else:
        targets = defs
    if not targets:
        raise ValueError(f"MBPP task {row.get('task_id')}: could not infer a function name")
    return _ordered_unique(targets)


def _mbpp_test_module(task_id: str, row: Mapping[str, Any], targets: Sequence[str]) -> str:
    lines: list[str] = [
        f'"""MBPP-derived tests for {task_id}."""',
        "",
        "import unittest",
        "",
    ]
    imports = [str(item).strip() for item in row.get("test_imports") or [] if str(item).strip()]
    lines.extend(imports)
    if imports:
        lines.append("")
    lines.append("from mylib import " + ", ".join(targets))
    lines.extend(["", "", "class TestMyLib(unittest.TestCase):"])

    test_list = [str(item).strip() for item in row.get("test_list") or []]
    if not test_list:
        raise ValueError(f"MBPP task {row.get('task_id')}: empty test_list")
    for index, assertion in enumerate(test_list, start=1):
        lines.append(f"    def test_case_{index:02d}(self):")
        lines.append(textwrap.indent(assertion, "        "))
        lines.append("")

    lines.extend(
        [
            "",
            'if __name__ == "__main__":',
            "    unittest.main()",
            "",
        ]
    )
    return "\n".join(lines)


def convert_mbpp(rows: Sequence[Mapping[str, Any]], dataset_dir: Path) -> int:
    ordered = sorted(rows, key=lambda row: int(row["task_id"]))[: TASK_LIMITS["mbpp"]]
    for index, row in enumerate(ordered, start=1):
        task_dir, task_id = _new_task_dir(dataset_dir, "mbpp", index)
        task_number = int(row["task_id"])
        targets = _mbpp_targets(row)
        workspace = task_dir / "workspace"
        workspace.mkdir()

        stub_text = '"""Stubs for the MBPP-derived task.\n\nImplement every function below.\n"""\n\n\n'
        stub_text += "\n\n\n".join(_stub_source(name, row.get("test_list") or []) for name in targets)
        _write_text(workspace / "mylib.py", stub_text.rstrip("\n") + "\n")
        _write_text(workspace / "test_mylib.py", _mbpp_test_module(task_id, row, targets))

        instruction = (
            "The file mylib.py contains unimplemented stubs for the functions exercised "
            "by test_mylib.py. Implement them so that `python3 test_mylib.py` passes. "
            "Do not edit the tests. Work only inside the current directory. "
            "When you are done, write ONLY the word DONE to the file answer.txt in the "
            "current directory."
        )
        _write_json(
            task_dir / "task.json",
            _task_payload(
                task_id=task_id,
                task_type="coding",
                instruction=instruction,
                reward=REWARDS["mbpp"],
                expected="tests_pass",
                check="unittest",
            ),
        )
        log.info("mbpp %s <- upstream task %s (%s)", task_id, task_number, ", ".join(targets))
    return len(ordered)


# -- QuixBugs ----------------------------------------------------------------
def _quixbugs_function_name(code: str, program: str) -> str:
    defs = _top_level_defs(code)
    if program in defs:
        return program
    if defs:
        return defs[0]
    raise ValueError(f"QuixBugs program {program!r}: no function definition found")


def _quixbugs_test_module(program: str, function: str, tests: Sequence[Any]) -> str:
    lines: list[str] = [
        f'"""QuixBugs-derived tests for {program}."""',
        "",
        "import unittest",
        "",
        f"from {program} import {function}",
        "",
        "",
        f"class Test{program.title().replace('_', '')}(unittest.TestCase):",
    ]
    if not tests:
        raise ValueError(f"QuixBugs program {program!r}: no test cases")
    for index, case in enumerate(tests, start=1):
        if not isinstance(case, (list, tuple)) or len(case) != 2:
            raise ValueError(f"QuixBugs program {program!r}: unsupported test case {case!r}")
        args, expected = case
        if not isinstance(args, (list, tuple)):
            raise TypeError(f"QuixBugs program {program!r}: args must be a list")
        call = f"{function}({', '.join(repr(arg) for arg in args)})"
        lines.append(f"    def test_case_{index:02d}(self):")
        lines.append(f"        self.assertEqual({call}, {expected!r})")
        lines.append("")
    lines.extend(
        [
            "",
            'if __name__ == "__main__":',
            "    unittest.main()",
            "",
        ]
    )
    return "\n".join(lines)


def convert_quixbugs(rows: Sequence[Mapping[str, Any]], dataset_dir: Path) -> int:
    ordered = sorted(rows, key=lambda row: str(row["program"]))[: TASK_LIMITS["quixbugs"]]
    for index, row in enumerate(ordered, start=1):
        program = str(row["program"])
        task_dir, task_id = _new_task_dir(dataset_dir, "quixbugs", index)
        buggy_code = str(row.get("buggy_code") or "")
        if not buggy_code.strip():
            raise ValueError(f"QuixBugs program {program!r}: empty buggy_code")
        function = _quixbugs_function_name(buggy_code, program)
        tests = row.get("tests")
        if not isinstance(tests, list):
            raise TypeError(f"QuixBugs program {program!r}: tests must be a list")

        workspace = task_dir / "workspace"
        workspace.mkdir()
        _write_text(workspace / f"{program}.py", buggy_code.rstrip("\n") + "\n")
        _write_text(
            workspace / f"test_{program}.py",
            _quixbugs_test_module(program, function, tests),
        )

        instruction = (
            f"The file {program}.py contains a buggy implementation. Fix it so that "
            f"`python3 test_{program}.py` passes. Do not edit the tests. Work only inside "
            "the current directory. When you are done, write ONLY the word DONE to the "
            "file answer.txt in the current directory."
        )
        _write_json(
            task_dir / "task.json",
            _task_payload(
                task_id=task_id,
                task_type="coding",
                instruction=instruction,
                reward=REWARDS["quixbugs"],
                expected="tests_pass",
                check="unittest",
            ),
        )
        log.info("quixbugs %s <- %s (%d cases)", task_id, program, len(tests))
    return len(ordered)


# -- GSM8K -------------------------------------------------------------------
def _gsm8k_expected(answer: str) -> str:
    if "####" not in answer:
        raise ValueError(f"GSM8K answer has no '####' marker: {answer!r}")
    value = answer.rsplit("####", 1)[1].strip().replace(",", "")
    if not _INT_RE.fullmatch(value):
        raise ValueError(f"GSM8K answer is not an integer: {value!r}")
    return value


def convert_gsm8k(rows: Sequence[Mapping[str, Any]], dataset_dir: Path) -> int:
    ordered = rows[: TASK_LIMITS["gsm8k"]]
    for index, row in enumerate(ordered, start=1):
        task_dir, task_id = _new_task_dir(dataset_dir, "gsm8k", index)
        question = str(row["question"]).strip()
        expected = _gsm8k_expected(str(row["answer"]))
        (task_dir / "workspace").mkdir()
        instruction = (
            f"{question}\n\n"
            "Solve it and write ONLY the final integer to the file answer.txt in the "
            "current directory."
        )
        _write_json(
            task_dir / "task.json",
            _task_payload(
                task_id=task_id,
                task_type="gsm8k",
                instruction=instruction,
                reward=REWARDS["gsm8k"],
                expected=expected,
                check="exact_int",
            ),
        )
        log.info("gsm8k %s -> %s", task_id, expected)
    return len(ordered)


# -- WikiTableQuestions ------------------------------------------------------
def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _write_table_csv(path: Path, title: str, header: Sequence[object], rows: Sequence[Sequence[object]]) -> None:
    if not header:
        raise ValueError(f"WTQ table {title!r}: empty header")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow([_csv_cell(cell) for cell in header])
        for row in rows:
            if len(row) != len(header):
                raise ValueError(
                    f"WTQ table {title!r}: row has {len(row)} cells, header has {len(header)}"
                )
            writer.writerow([_csv_cell(cell) for cell in row])


def convert_wikitable(rows: Sequence[Mapping[str, Any]], dataset_dir: Path) -> int:
    ordered = sorted(rows, key=lambda row: str(row["id"]))[: TASK_LIMITS["wikitable"]]
    for index, row in enumerate(ordered, start=1):
        task_dir, task_id = _new_task_dir(dataset_dir, "wikitable", index)
        table = row.get("table")
        if not isinstance(table, dict):
            raise TypeError(f"WTQ {row.get('id')}: table must be an object")
        header = table.get("header")
        table_rows = table.get("rows")
        if not isinstance(header, list) or not isinstance(table_rows, list):
            raise TypeError(f"WTQ {row.get('id')}: header/rows must be lists")
        answers = row.get("answers")
        if not isinstance(answers, list) or not answers:
            raise ValueError(f"WTQ {row.get('id')}: no answers")
        expected = _normalize_answer(answers[0])
        if not expected:
            raise ValueError(f"WTQ {row.get('id')}: empty normalized answer")

        workspace = task_dir / "workspace"
        workspace.mkdir()
        _write_table_csv(workspace / "table.csv", str(row.get("id")), header, table_rows)

        question = str(row["question"]).strip()
        instruction = (
            f"{question}\n\n"
            "Use the table in table.csv and write ONLY the final answer to the file "
            "answer.txt in the current directory."
        )
        _write_json(
            task_dir / "task.json",
            _task_payload(
                task_id=task_id,
                task_type="table_qa",
                instruction=instruction,
                reward=REWARDS["wikitable"],
                expected=expected,
                check="exact_str",
            ),
        )
        log.info("wikitable %s <- %s (%d rows)", task_id, row.get("id"), len(table_rows))
    return len(ordered)


# -- HotpotQA ----------------------------------------------------------------
def _render_context(context: Mapping[str, Any], row_id: str) -> str:
    titles = context.get("title")
    sentence_groups = context.get("sentences")
    if not isinstance(titles, list) or not isinstance(sentence_groups, list):
        raise TypeError(f"HotpotQA {row_id}: context title/sentences must be lists")
    if len(titles) != len(sentence_groups):
        raise ValueError(f"HotpotQA {row_id}: context title/sentences length mismatch")
    if not titles:
        raise ValueError(f"HotpotQA {row_id}: empty context")

    sections: list[str] = []
    for title, sentences in zip(titles, sentence_groups):
        if not isinstance(sentences, list):
            raise TypeError(f"HotpotQA {row_id}: paragraph {title!r} is not a list")
        body = "".join(str(sentence) for sentence in sentences).strip()
        sections.append(f"# {str(title).strip()}\n\n{body}\n")
    return "\n".join(sections).strip() + "\n"


def convert_hotpotqa(rows: Sequence[Mapping[str, Any]], dataset_dir: Path) -> int:
    ordered = sorted(rows, key=lambda row: str(row["id"]))[: TASK_LIMITS["hotpotqa"]]
    for index, row in enumerate(ordered, start=1):
        task_dir, task_id = _new_task_dir(dataset_dir, "hotpotqa", index)
        row_id = str(row.get("id"))
        context = row.get("context")
        if not isinstance(context, dict):
            raise TypeError(f"HotpotQA {row_id}: context must be an object")
        answer = str(row.get("answer") or "").strip()
        if not answer:
            raise ValueError(f"HotpotQA {row_id}: empty answer")

        workspace = task_dir / "workspace"
        workspace.mkdir()
        _write_text(workspace / "context.md", _render_context(context, row_id))

        question = str(row["question"]).strip()
        instruction = (
            f"{question}\n\n"
            "Use only context.md and write ONLY the final answer to the file answer.txt "
            "in the current directory."
        )
        _write_json(
            task_dir / "task.json",
            _task_payload(
                task_id=task_id,
                task_type="context_qa",
                instruction=instruction,
                reward=REWARDS["hotpotqa"],
                expected=answer,
                check="exact_str",
            ),
        )
        log.info("hotpotqa %s <- %s", task_id, row_id)
    return len(ordered)


# -- driver -------------------------------------------------------------------
_CONVERTERS = {
    "mbpp": convert_mbpp,
    "quixbugs": convert_quixbugs,
    "gsm8k": convert_gsm8k,
    "wikitable": convert_wikitable,
    "hotpotqa": convert_hotpotqa,
}


def _validate_sample_size(dataset: str, rows: Sequence[Mapping[str, Any]]) -> None:
    required = TASK_LIMITS[dataset]
    if len(rows) < required:
        raise ValueError(
            f"{dataset}: sample has {len(rows)} row(s), {required} required for a stable task set"
        )


def convert_all(src: Path, out: Path) -> dict[str, int]:
    if not src.is_dir():
        raise FileNotFoundError(f"source sample directory does not exist: {src}")
    out.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    seen_ids: set[str] = set()
    for dataset, sample_file in SAMPLE_FILES.items():
        sample_path = src / sample_file
        if not sample_path.is_file():
            raise FileNotFoundError(f"missing sample file: {sample_path}")
        rows = _read_jsonl(sample_path)
        _validate_sample_size(dataset, rows)
        dataset_dir = out / dataset
        _reset_generated_task_dirs(dataset_dir, dataset)
        dataset_dir.mkdir(parents=True, exist_ok=True)
        _copy_source(src, dataset_dir, sample_file)
        counts[dataset] = _CONVERTERS[dataset](rows, dataset_dir)
        for index in range(1, counts[dataset] + 1):
            task_id = f"ext_{dataset}_{index}"
            if task_id in seen_ids:  # pragma: no cover - defensive
                raise ValueError(f"duplicate task id: {task_id}")
            seen_ids.add(task_id)
        log.info("%s: %d task(s) from %s", dataset, counts[dataset], sample_file)

    _strip_caches(out)
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="directory with the verified JSONL samples")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (default: eval/external)")
    parser.add_argument("--verbose", "-v", action="store_true", help="log every converted task")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        counts = convert_all(args.src, args.out)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    total = sum(counts.values())
    print(f"wrote {total} task(s) to {args.out}", file=sys.stderr)
    for dataset, count in counts.items():
        print(f"  {dataset:<10} {count:>3}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
