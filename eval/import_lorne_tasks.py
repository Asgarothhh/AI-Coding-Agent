"""Convert selected lorne bench tasks into the openJiuwen harness format.

The source is the lorne bench tree at ``/home/eq/techlb/TCA/bench/tasks``.
Every converted task becomes ``<out>/lorne_<source_id>/`` with a
``task.json`` and a ``workspace/`` directory::

    lorne_py-fix-add/
        task.json
        workspace/
            calc.py
            test_lorne_py_fix_add.py

The conversion is deterministic and idempotent: the destination directory is
recreated from scratch on every run and stale ``lorne_*`` task directories are
pruned.  Only the ten requested tasks are converted; see :data:`CODING_TASKS`
and :data:`FILE_LOOKUP_TASKS`.

Design notes
------------
* ``check``/``expected`` mirror the harness conventions: coding tasks use
  ``unittest`` / ``tests_pass``, file_lookup tasks use ``exact_str`` / the exact
  answer path.
* Coding workspaces get a stdlib-only ``test_*.py`` wrapper that reproduces
  the assertions of the original ``check.sh``.  The wrapper never reads
  ``check.sh`` and does not grep the workspace in a way that could match
  itself (see the ``py-rename-func`` old-name scan).
* ``py-write-test`` is inverted: the original asked for a test file and used
  a weak pytest/grep checker.  The converted task ships a real unittest
  suite (``test_fizz.py``) and asks the solver to implement ``fizz.py``.
* ``read-const`` and ``read-def`` are converted as ``file_lookup`` tasks: the
  coding solver only patches ``*.py`` files, so a lookup needs the generic
  solver path.  They keep ``exact_str`` / the exact relative answer path.

Usage::

    python3.11 eval/import_lorne_tasks.py
    python3.11 eval/import_lorne_tasks.py --src /home/eq/techlb/TCA/bench/tasks
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import tomllib

log = logging.getLogger("import_lorne_tasks")

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_SRC: Final = Path("/home/eq/techlb/TCA/bench/tasks")
DEFAULT_OUT: Final = REPO_ROOT / "eval" / "external" / "lorne"

CODING_REWARD: Final = 2
FILE_LOOKUP_REWARD: Final = 1

#: Artefacts that must never leak from the source repository.
_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".DS_Store")
_FORBIDDEN_TOP_LEVEL: Final = frozenset({"check.sh", "solution.patch", "task.toml"})

CODING_SUFFIX: Final = (
    "\n\nWork only inside the current directory. Run the tests; fix source "
    "files only; write ONLY DONE to answer.txt."
)
FILE_LOOKUP_SUFFIX: Final = (
    "\n\nLocate the file inside the current directory and write its path, "
    "relative to the current directory, to answer.txt. Write ONLY the path "
    "to answer.txt."
)

# --------------------------------------------------------------------------- #
# Embedded test wrappers
# --------------------------------------------------------------------------- #
_PY_ARGPARSE_FLAG_TEST: Final = '''"""Contract tests for lorne_py-argparse-flag.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class ArgparseShoutTests(unittest.TestCase):
    def test_shout_prints_uppercase_greeting(self) -> None:
        proc = _run(sys.executable, "cli.py", "--name", "bob", "--shout")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "HELLO, BOB!")

    def test_default_output_is_unchanged(self) -> None:
        proc = _run(sys.executable, "cli.py", "--name", "bob")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "Hello, bob!")


if __name__ == "__main__":
    unittest.main()
'''

_PY_DOCSTRING_AST_TEST: Final = '''"""Contract tests for lorne_py-docstring-ast.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class AreaAnnotationTests(unittest.TestCase):
    def test_area_has_annotations_and_docstring(self) -> None:
        source = (HERE / "geometry.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "area"
            ),
            None,
        )
        self.assertIsNotNone(function, "function area not found")
        assert function is not None  # narrowed for type checkers

        for argument in function.args.args:
            self.assertIsNotNone(
                argument.annotation,
                f"missing annotation for {argument.arg}",
            )
        self.assertIsInstance(function.returns, ast.Name)
        self.assertEqual(getattr(function.returns, "id", None), "float")
        self.assertTrue(ast.get_docstring(function), "missing docstring")


if __name__ == "__main__":
    unittest.main()
'''

_PY_EMPTY_LIST_TEST: Final = '''"""Contract tests for lorne_py-empty-list.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from stats import average


class EmptyListTests(unittest.TestCase):
    def test_average_of_empty_list_is_zero(self) -> None:
        self.assertEqual(average([]), 0.0)

    def test_average_of_non_empty_list(self) -> None:
        self.assertEqual(average([1, 3]), 2.0)


if __name__ == "__main__":
    unittest.main()
'''

_PY_FIX_ADD_TEST: Final = '''"""Contract tests for lorne_py-fix-add.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from calc import add


class AddTests(unittest.TestCase):
    def test_add_adds(self) -> None:
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
'''

_PY_FIX_IMPORT_TEST: Final = '''"""Contract tests for lorne_py-fix-import.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class ImportPathTests(unittest.TestCase):
    def test_main_prints_hello_world(self) -> None:
        proc = subprocess.run(
            [sys.executable, "main.py"],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "hello-world")


if __name__ == "__main__":
    unittest.main()
'''

_PY_RENAME_FUNC_TEST: Final = '''"""Contract tests for lorne_py-rename-func.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.  The
old-name scan deliberately skips this wrapper so the test file cannot match
itself.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import utils


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class RenameFunctionTests(unittest.TestCase):
    def test_renamed_function_is_available(self) -> None:
        self.assertEqual(utils.total([1, 2, 3]), 6)
        self.assertFalse(hasattr(utils, "calc_total"))

    def test_entrypoints_still_work(self) -> None:
        main = _run(sys.executable, "main.py")
        self.assertEqual(main.returncode, 0, main.stderr)
        self.assertEqual(main.stdout.strip(), "6")

        report = _run(sys.executable, "report.py")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(report.stdout.strip(), "total=9")

    def test_old_name_is_absent_from_the_workspace(self) -> None:
        wrapper = Path(__file__).resolve()
        for path in sorted(HERE.rglob("*")):
            if not path.is_file() or path.resolve() == wrapper:
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            self.assertNotIn(
                "calc_total",
                text,
                f"old name calc_total still present in {path.name}",
            )


if __name__ == "__main__":
    unittest.main()
'''

_PY_TRACE_BUG_TEST: Final = '''"""Contract tests for lorne_py-trace-bug.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import app


class TraceBugTests(unittest.TestCase):
    def test_entrypoint_prints_bob(self) -> None:
        proc = subprocess.run(
            [sys.executable, "app.py"],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "Bob")

    def test_get_returns_the_requested_person(self) -> None:
        self.assertEqual(app.get("Ann")["name"], "Ann")


if __name__ == "__main__":
    unittest.main()
'''

_TEST_FIZZ: Final = '''"""Behavioral unittest suite for fizz.py.

Converted from the original lorne ``py-write-test`` checker: the weak
pytest/grep check is replaced by explicit assertions against ``fizzbuzz``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fizz import fizzbuzz


class FizzBuzzTests(unittest.TestCase):
    def test_plain_numbers(self) -> None:
        self.assertEqual(fizzbuzz(1), "1")
        self.assertEqual(fizzbuzz(2), "2")
        self.assertEqual(fizzbuzz(7), "7")

    def test_multiples_of_three(self) -> None:
        self.assertEqual(fizzbuzz(3), "fizz")
        self.assertEqual(fizzbuzz(6), "fizz")

    def test_multiples_of_five(self) -> None:
        self.assertEqual(fizzbuzz(5), "buzz")
        self.assertEqual(fizzbuzz(10), "buzz")

    def test_multiples_of_fifteen(self) -> None:
        self.assertEqual(fizzbuzz(15), "fizzbuzz")
        self.assertEqual(fizzbuzz(30), "fizzbuzz")


if __name__ == "__main__":
    unittest.main()
'''

_FIZZ_STUB: Final = '''"""fizzbuzz implementation used by test_fizz.py."""


def fizzbuzz(n: int) -> str:
    raise NotImplementedError("implement fizzbuzz")
'''


@dataclass(frozen=True, slots=True)
class CodingSpec:
    """Conversion metadata for one coding task."""

    test_file: str
    test_source: str
    extra_files: Mapping[str, str] = field(default_factory=dict)
    instruction: str | None = None


CODING_TASKS: Final[Mapping[str, CodingSpec]] = {
    "py-argparse-flag": CodingSpec(
        test_file="test_lorne_py_argparse_flag.py",
        test_source=_PY_ARGPARSE_FLAG_TEST,
    ),
    "py-docstring-ast": CodingSpec(
        test_file="test_lorne_py_docstring_ast.py",
        test_source=_PY_DOCSTRING_AST_TEST,
    ),
    "py-empty-list": CodingSpec(
        test_file="test_lorne_py_empty_list.py",
        test_source=_PY_EMPTY_LIST_TEST,
    ),
    "py-fix-add": CodingSpec(
        test_file="test_lorne_py_fix_add.py",
        test_source=_PY_FIX_ADD_TEST,
    ),
    "py-fix-import": CodingSpec(
        test_file="test_lorne_py_fix_import.py",
        test_source=_PY_FIX_IMPORT_TEST,
    ),
    "py-rename-func": CodingSpec(
        test_file="test_lorne_py_rename_func.py",
        test_source=_PY_RENAME_FUNC_TEST,
    ),
    "py-trace-bug": CodingSpec(
        test_file="test_lorne_py_trace_bug.py",
        test_source=_PY_TRACE_BUG_TEST,
    ),
    "py-write-test": CodingSpec(
        test_file="test_fizz.py",
        test_source=_TEST_FIZZ,
        extra_files={"fizz.py": _FIZZ_STUB},
        instruction=(
            "Реализуй функцию fizzbuzz в fizz.py так, чтобы проходил набор тестов "
            "test_fizz.py: для чисел, кратных 15, вернуть \"fizzbuzz\"; для кратных "
            "3 — \"fizz\"; для кратных 5 — \"buzz\"; иначе вернуть str(n)."
        ),
    ),
}

FILE_LOOKUP_TASKS: Final[Mapping[str, str]] = {
    "read-const": "settings/defaults.py",
    "read-def": "lib/payments/stripe_client.py",
}


# --------------------------------------------------------------------------- #
# IO helpers
# --------------------------------------------------------------------------- #
def _load_prompt(source_dir: Path) -> str:
    task_toml = source_dir / "task.toml"
    if not task_toml.is_file():
        raise FileNotFoundError(f"missing {task_toml}")
    data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{task_toml}: missing or empty 'prompt'")
    return prompt.strip()


def _copy_workspace(source_repo: Path, destination: Path) -> None:
    if not source_repo.is_dir():
        raise FileNotFoundError(f"missing repo directory: {source_repo}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source_repo, destination, ignore=_IGNORE)

    # Belt and braces: remove ignored artefacts even if a future copytree
    # change stops honouring the ignore list for nested directories.
    for directory_name in ("__pycache__", ".pytest_cache"):
        for path in destination.rglob(directory_name):
            if path.is_dir():
                shutil.rmtree(path)
    for file_pattern in ("*.pyc", ".DS_Store"):
        for path in destination.rglob(file_pattern):
            if path.is_file():
                path.unlink()
    for name in _FORBIDDEN_TOP_LEVEL:
        forbidden = destination / name
        if forbidden.exists():
            if forbidden.is_dir():
                shutil.rmtree(forbidden)
            else:
                forbidden.unlink()


def _write_workspace_files(destination: Path, files: Mapping[str, str]) -> None:
    for relative, content in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _write_task_json(destination: Path, payload: Mapping[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    (destination / "task.json").write_text(text, encoding="utf-8")


def _convert_coding(
    source_root: Path,
    out_root: Path,
    source_id: str,
    spec: CodingSpec,
) -> Path:
    source_dir = source_root / source_id
    prompt = spec.instruction if spec.instruction is not None else _load_prompt(source_dir)
    destination = out_root / f"lorne_{source_id}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    workspace = destination / "workspace"
    _copy_workspace(source_dir / "repo", workspace)

    files = dict(spec.extra_files)
    files[spec.test_file] = spec.test_source
    _write_workspace_files(workspace, files)

    _write_task_json(
        destination,
        {
            "task_id": f"lorne_{source_id}",
            "task_type": "coding",
            "instruction": prompt + CODING_SUFFIX,
            "reward": CODING_REWARD,
            "expected": "tests_pass",
            "check": "unittest",
        },
    )
    return destination


def _convert_file_lookup(
    source_root: Path,
    out_root: Path,
    source_id: str,
    expected: str,
) -> Path:
    source_dir = source_root / source_id
    prompt = _load_prompt(source_dir)
    destination = out_root / f"lorne_{source_id}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    _copy_workspace(source_dir / "repo", destination / "workspace")

    _write_task_json(
        destination,
        {
            "task_id": f"lorne_{source_id}",
            "task_type": "file_lookup",
            "instruction": prompt + FILE_LOOKUP_SUFFIX,
            "reward": FILE_LOOKUP_REWARD,
            "expected": expected,
            "check": "exact_str",
        },
    )
    return destination


# --------------------------------------------------------------------------- #
# README
# --------------------------------------------------------------------------- #
def _readme() -> str:
    rows: list[str] = []
    for source_id, spec in CODING_TASKS.items():
        rows.append(
            f"| `lorne_{source_id}` | `{source_id}` | coding | {CODING_REWARD} "
            f"| `unittest` | `tests_pass` | `{spec.test_file}` |"
        )
    for source_id, expected in FILE_LOOKUP_TASKS.items():
        rows.append(
            f"| `lorne_{source_id}` | `{source_id}` | file_lookup | {FILE_LOOKUP_REWARD} "
            f"| `exact_str` | `{expected}` | - |"
        )
    table = "\n".join(rows)
    return f"""# Lorne bench tasks (converted)

Generated by `python3.11 eval/import_lorne_tasks.py` from
`/home/eq/techlb/TCA/bench/tasks`.

## Source and license

The source tasks come from the lorne bench repository at
`/home/eq/techlb/TCA/bench`, which is owned by the user. No third-party
license header was present in the copied files; the conversion is limited to
that user-owned material and keeps the original prompts.

## Tasks

Ten tasks are converted: eight `coding` tasks and two `file_lookup` tasks.

| converted id | source id | task_type | reward | check | expected | test wrapper |
|---|---|---|---:|---|---|---|
{table}

## Conversion decisions

- Converted ids are prefixed with `lorne_`; each task directory matches its
  `task_id`.
- `reward` is `2` for coding tasks and `1` for file_lookup tasks.
- `check`/`expected` follow the harness examples: coding uses
  `unittest` / `tests_pass`, file_lookup uses `exact_str` / the exact relative
  answer path.
- The instruction is the original `prompt` plus the required answer-file
  sentence: coding tasks must run the tests, fix sources only and write
  `DONE`; file_lookup tasks must locate the file and write only its relative
  path to `answer.txt`.
- `workspace/` is a copy of the lorne `repo/` directory with `__pycache__`,
  `.pytest_cache`, `*.pyc` and `.DS_Store` removed. `check.sh`,
  `solution.patch` and `task.toml` are never copied.
- Every coding workspace ships a stdlib-only `test_*.py` wrapper that
  reproduces the original `check.sh` assertions. The wrappers do not read
  `check.sh` at runtime and use `subprocess` only to invoke the project's own
  entrypoints (`python cli.py`, `python main.py`, ...).
- `py-rename-func`: the old-name absence check scans the workspace but skips
  the wrapper file itself, so the test cannot match its own source.
- `py-write-test`: the original weak pytest/grep checker was replaced by a
  real unittest suite in `test_fizz.py`, and the task now asks to implement
  `fizz.py`. The workspace ships an unimplemented stub so the task starts
  failing.
- `read-const` and `read-def` are converted as `file_lookup` (an unknown
  `task_type`), so the runner falls back to the generic solver instead of the
  deterministic `search` solver. The workspaces ship the original repository
  files (including `README.md`).

## Exclusions

- `json-add-key` and `sh-fix-regex`: removed from the converted set. The
  coding solver only patches `*.py` files, while the real fixes touch
  `config.json` and `slugify.sh`; the companion `.py` shims used earlier were
  a workaround, not a real solution.
- `py-fix-test`: the original check requires editing a broken test file while
  the harness coding solver protects test files and restores them; the two
  rules conflict.
- `js-*` tasks: the target image has no Node.js runtime.
- `mk-fix-path`: the target image has no `make`.
- `py-off-by-one`: convertible in principle but not part of the requested
  conversion set.

## Verification

```bash
python3.11 -c "from harness.task import load_tasks; \\
  print(len(load_tasks('eval/external/lorne')))"          # 10
python3.11 run_agent.py --tasks-dir eval/external/lorne \\
  --offline --dry-run --max-workers 1                     # 10 planned
```
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help=f"lorne tasks directory (default: {DEFAULT_SRC})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"destination directory (default: {DEFAULT_OUT})",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def _validate_accounting() -> None:
    if set(CODING_TASKS) & set(FILE_LOOKUP_TASKS):
        raise ValueError("coding and file_lookup task sets overlap")
    total = len(CODING_TASKS) + len(FILE_LOOKUP_TASKS)
    if total != 10:
        raise ValueError(f"expected 10 tasks, configured {total}")


def _prune_stale(out: Path, expected_ids: set[str]) -> None:
    """Remove previously generated ``lorne_*`` dirs that are no longer wanted."""

    for child in sorted(out.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not child.name.startswith("lorne_"):
            continue
        if child.name not in expected_ids:
            log.info("removing stale task directory %s", child)
            shutil.rmtree(child)


def convert(src: Path, out: Path) -> list[Path]:
    if not src.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {src}")
    _validate_accounting()
    out.mkdir(parents=True, exist_ok=True)

    expected_ids = {
        f"lorne_{source_id}" for source_id in (*CODING_TASKS, *FILE_LOOKUP_TASKS)
    }
    _prune_stale(out, expected_ids)

    written: list[Path] = []
    for source_id, spec in CODING_TASKS.items():
        written.append(_convert_coding(src, out, source_id, spec))
    for source_id, expected in FILE_LOOKUP_TASKS.items():
        written.append(_convert_file_lookup(src, out, source_id, expected))

    (out / "README.md").write_text(_readme(), encoding="utf-8")
    return written


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        written = convert(args.src, args.out)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        log.error("conversion failed: %s", exc)
        return 1

    print(f"converted {len(written)} tasks into {args.out}")
    for path in written:
        print(f"  {path.name}")
    print(f"README: {args.out / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
