"""Coding-task solver with test protection and bounded repair loops.

Flow:

1. run the shipped tests;
2. optionally try a tiny deterministic mutation search (operators,
   comparisons, parity, ``sorted``/``reversed``);
3. otherwise ask the LLM for a full-file replacement, apply it, verify that
   the test files were not touched, and rerun the tests;
4. write ``DONE`` only when every test passes.

The test files are hashed before any write and restored if the LLM tries to
modify them.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .base import SolveResult, SolverContext
from ..budget import BudgetExhausted
from ..llm import LLMError
from ..store import atomic_write_bytes, atomic_write_text
from ..task import Task

__all__ = ["solve"]

_SOURCE_LIMIT = 12_000
_TEST_LIMIT = 8_000
_OUTPUT_LIMIT = 6_000


@dataclass(frozen=True, slots=True)
class _TestRun:
    passed: bool
    output: str
    command: str


@dataclass(frozen=True, slots=True)
class _TestSnapshot:
    files: dict[Path, bytes]
    hashes: dict[Path, str]


@dataclass(frozen=True, slots=True)
class _Mutation:
    file: Path
    line_no: int
    new_line: str
    label: str


class _PatchError(ValueError):
    pass


# -- test execution -----------------------------------------------------------
def _test_files(workdir: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in ("test_*.py", "*_test.py"):
        found.extend(sorted(workdir.glob(pattern)))
    # Stable de-duplication.
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _source_files(workdir: Path) -> list[Path]:
    skip = set(_test_files(workdir))
    return [path for path in sorted(workdir.glob("*.py")) if path not in skip]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _run_tests(workdir: Path, timeout: float) -> _TestRun:
    tests = _test_files(workdir)
    if not tests:
        return _TestRun(False, "no test files found", "")

    env = os.environ.copy()
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(workdir) + (os.pathsep + existing_path if existing_path else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    python = sys.executable or "python3"
    chunks: list[str] = []
    for test in tests:
        command = [python, "-B", test.name]
        command_text = " ".join(command)
        try:
            process = subprocess.run(
                command,
                cwd=str(workdir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            chunks.append(f"$ {command_text}\nTIMEOUT after {timeout:.0f}s")
            return _TestRun(False, _truncate("\n".join(chunks), _OUTPUT_LIMIT), command_text)
        except OSError as exc:
            chunks.append(f"$ {command_text}\nOSError: {exc}")
            return _TestRun(False, _truncate("\n".join(chunks), _OUTPUT_LIMIT), command_text)

        output = (process.stdout or "") + (process.stderr or "")
        chunks.append(f"$ {command_text}\n{output.strip()}")
        if process.returncode != 0:
            return _TestRun(False, _truncate("\n".join(chunks), _OUTPUT_LIMIT), command_text)

    return _TestRun(True, _truncate("\n".join(chunks), _OUTPUT_LIMIT), " ".join(f"{python} -B {t.name}" for t in tests))


# -- test-file protection -----------------------------------------------------
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot_tests(workdir: Path) -> _TestSnapshot:
    files: dict[Path, bytes] = {}
    hashes: dict[Path, str] = {}
    for path in _test_files(workdir):
        data = path.read_bytes()
        files[path] = data
        hashes[path] = _sha256(data)
    return _TestSnapshot(files=files, hashes=hashes)


def _verify_tests(snapshot: _TestSnapshot) -> list[str]:
    violations: list[str] = []
    for path, expected in snapshot.hashes.items():
        if not path.is_file():
            violations.append(f"{path.name}: missing")
            continue
        actual = _sha256(path.read_bytes())
        if actual != expected:
            violations.append(f"{path.name}: modified")
    return violations


def _restore_tests(snapshot: _TestSnapshot) -> None:
    for path, data in snapshot.files.items():
        atomic_write_bytes(path, data)


# -- deterministic mutation repair -------------------------------------------
def _mutate_line(line: str) -> list[tuple[str, str]]:
    replacements: tuple[tuple[str, str], ...] = (
        ("+=", "-="),
        ("-=", "+="),
        (" <= ", " >= "),
        (" >= ", " <= "),
        (" < ", " > "),
        (" > ", " < "),
        (" == ", " != "),
        (" != ", " == "),
        ("% 2 == 0", "% 2 == 1"),
        ("% 2 == 1", "% 2 == 0"),
        ("% 2 != 0", "% 2 != 1"),
        ("% 2 != 1", "% 2 != 0"),
        ("sorted(", "reversed("),
        ("reversed(", "sorted("),
        (" + ", " - "),
        (" - ", " + "),
    )
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for old, new in replacements:
        if old not in line:
            continue
        mutated = line.replace(old, new, 1)
        if mutated != line and mutated not in seen:
            seen.add(mutated)
            candidates.append((mutated, f"{old!r}->{new!r}"))
    return candidates


def _apply_mutations(originals: dict[Path, str], mutations: Sequence[_Mutation]) -> None:
    by_file: dict[Path, list[_Mutation]] = {}
    for mutation in mutations:
        by_file.setdefault(mutation.file, []).append(mutation)

    for path, file_mutations in by_file.items():
        lines = originals[path].splitlines(keepends=True)
        for mutation in sorted(file_mutations, key=lambda m: m.line_no, reverse=True):
            index = mutation.line_no - 1
            if index < 0 or index >= len(lines):  # pragma: no cover - defensive
                continue
            original_line = lines[index]
            if original_line.endswith("\r\n"):
                ending = "\r\n"
            elif original_line.endswith("\n"):
                ending = "\n"
            else:
                ending = ""
            lines[index] = mutation.new_line + ending
        atomic_write_text(path, "".join(lines))


def _restore_sources(originals: dict[Path, str]) -> None:
    for path, text in originals.items():
        atomic_write_text(path, text)


def _mutation_repair(
    workdir: Path,
    ctx: SolverContext,
) -> tuple[bool, _TestRun | None, int]:
    originals: dict[Path, str] = {}
    for path in _source_files(workdir):
        try:
            originals[path] = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return False, None, 0

    if not originals:
        return False, None, 0

    mutations: list[_Mutation] = []
    seen: set[tuple[Path, int, str]] = set()
    for path, text in originals.items():
        for line_no, line in enumerate(text.splitlines(), start=1):
            for new_line, label in _mutate_line(line):
                key = (path, line_no, new_line)
                if key in seen:
                    continue
                seen.add(key)
                mutations.append(_Mutation(path, line_no, new_line, label))

    combinations: list[tuple[_Mutation, ...]] = [(m,) for m in mutations]
    for first, second in itertools.combinations(mutations, 2):
        if first.file == second.file and first.line_no == second.line_no:
            continue
        combinations.append((first, second))

    deadline = time.monotonic() + ctx.options.mutation_time_budget
    max_attempts = 80
    attempts = 0
    last_run: _TestRun | None = None

    for combo in combinations:
        if attempts >= max_attempts or time.monotonic() > deadline:
            break
        attempts += 1
        _apply_mutations(originals, combo)
        run = _run_tests(workdir, ctx.options.test_timeout)
        last_run = run
        if run.passed:
            return True, run, attempts
        _restore_sources(originals)

    _restore_sources(originals)
    return False, last_run, attempts


# -- LLM patch loop -----------------------------------------------------------
def _build_messages(
    task: Task,
    workdir: Path,
    test_output: str,
    iteration: int,
) -> list[dict[str, str]]:
    sources = _source_files(workdir)
    tests = _test_files(workdir)

    source_blocks: list[str] = []
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        source_blocks.append(f"### FILE: {path.name}\n```python\n{_truncate(text, _SOURCE_LIMIT)}\n```")

    test_blocks: list[str] = []
    for path in tests:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        test_blocks.append(f"### TEST (read-only): {path.name}\n```python\n{_truncate(text, _TEST_LIMIT)}\n```")

    system = (
        "You are a precise Python repair agent. Fix the source files so the provided "
        "tests pass. Never modify test files. Return ONLY a JSON object of the form "
        '{"files": {"name.py": "<full corrected file content>"}} with no prose.'
    )
    user = (
        f"Task instruction:\n{task.instruction}\n\n"
        f"Attempt: {iteration + 1}\n\n"
        "Source files:\n" + "\n\n".join(source_blocks) + "\n\n"
        "Tests (do not edit):\n" + "\n\n".join(test_blocks) + "\n\n"
        f"Current test output:\n```\n{_truncate(test_output, _OUTPUT_LIMIT)}\n```\n\n"
        "Return the complete corrected content of every source file that must change."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _safe_patch_name(name: str, workdir: Path) -> str | None:
    candidate = name.strip().replace("\\", "/")
    if "/" in candidate:
        return None
    if Path(candidate).name != candidate:
        return None
    if not candidate.endswith(".py"):
        return None
    lowered = candidate.lower()
    if lowered.startswith("test_") or lowered.endswith("_test.py"):
        return None
    if not (workdir / candidate).is_file():
        return None
    return candidate


def _parse_patch(text: str, workdir: Path) -> dict[str, str] | None:
    stripped = text.strip()
    obj: object = None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(stripped[start:])
            except json.JSONDecodeError:
                obj = None

    if isinstance(obj, dict):
        raw_files = obj.get("files") if isinstance(obj.get("files"), dict) else obj
        if isinstance(raw_files, dict):
            patch: dict[str, str] = {}
            for name, content in raw_files.items():
                if isinstance(name, str) and isinstance(content, str):
                    safe = _safe_patch_name(name, workdir)
                    if safe is not None:
                        patch[safe] = content
            if patch:
                return patch

    patch = {}
    labelled = re.compile(
        r"(?m)^(?:#+\s*)?(?:FILE:?\s*)?([\w.-]+\.py)\s*\n```(?:python|py)?\s*\n(.*?)```",
        re.DOTALL,
    )
    for name, content in labelled.findall(text):
        safe = _safe_patch_name(name, workdir)
        if safe is not None:
            patch[safe] = content
    if patch:
        return patch

    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    sources = [path.name for path in _source_files(workdir)]
    if len(blocks) == 1 and len(sources) == 1:
        return {sources[0]: blocks[0]}
    return None


def _apply_patch(workdir: Path, patch: dict[str, str]) -> None:
    for name, content in patch.items():
        safe = _safe_patch_name(name, workdir)
        if safe is None:
            raise _PatchError(f"refusing to patch {name!r}")
        atomic_write_text(workdir / safe, content)


# -- entry point --------------------------------------------------------------
def solve(task: Task, ctx: SolverContext) -> SolveResult:
    workdir = task.workdir
    tests = _test_files(workdir)
    sources = _source_files(workdir)
    if not tests:
        return SolveResult(None, 0.0, "coding", "no test files found")
    if not sources:
        return SolveResult(None, 0.0, "coding", "no source files found")

    snapshot = _snapshot_tests(workdir)

    initial = _run_tests(workdir, ctx.options.test_timeout)
    if initial.passed:
        return SolveResult("DONE", 0.0, "coding", "tests already pass")

    if ctx.options.mutation_repair:
        solved, run, attempts = _mutation_repair(workdir, ctx)
        if solved:
            violations = _verify_tests(snapshot)
            if violations:
                _restore_tests(snapshot)
                return SolveResult(None, 0.0, "coding", "test protection violation: " + ", ".join(violations))
            return SolveResult("DONE", 0.0, "coding", f"mutation repair solved tests in {attempts} attempt(s)")
        last_output = run.output if run is not None else initial.output
    else:
        last_output = initial.output

    if ctx.llm is None or not ctx.llm.available:
        return SolveResult(None, 0.0, "coding", "tests fail and no LLM is available")

    tokens = 0.0
    notes = "tests still fail"
    for iteration in range(max(0, ctx.options.max_iterations)):
        messages = _build_messages(task, workdir, last_output, iteration)
        estimated = min(ctx.options.max_tokens, 768) + 128
        try:
            ctx.budget.check(task.task_id, estimated_cost=estimated)
        except BudgetExhausted as exc:
            return SolveResult(None, tokens, "coding", f"budget stop: {exc}")

        try:
            response = ctx.llm.chat(
                messages,
                max_tokens=ctx.options.max_tokens,
                attempt=iteration,
            )
        except LLMError as exc:
            return SolveResult(None, tokens, "coding", f"LLM error: {exc}")

        cost = ctx.budget.charge(
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            task.task_id,
        )
        tokens += cost

        patch = _parse_patch(response.text, workdir)
        if not patch:
            notes = "LLM did not return a usable full-file patch"
            continue

        try:
            _apply_patch(workdir, patch)
        except _PatchError as exc:
            notes = str(exc)
            continue

        violations = _verify_tests(snapshot)
        if violations:
            _restore_tests(snapshot)
            return SolveResult(None, tokens, "coding", "test protection violation: " + ", ".join(violations))

        run = _run_tests(workdir, ctx.options.test_timeout)
        last_output = run.output
        notes = f"iteration {iteration + 1}: tests still fail"
        if run.passed:
            return SolveResult("DONE", tokens, "coding", f"tests pass after {iteration + 1} LLM iteration(s)")

    return SolveResult(None, tokens, "coding", notes)
