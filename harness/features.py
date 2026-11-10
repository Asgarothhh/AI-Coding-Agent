"""Cheap, offline task features for the cost estimator.

The estimator must describe the *potential prompt context* of a task, not the
whole task directory.  A deterministic solver never sends files to a model, so
its feature vector carries no file content.  The coding solver only sends the
top-level ``*.py`` files it actually reads (sources and tests); those are the
files counted here, with the same truncation limits as
:mod:`harness.solvers.coding`.

Everything in this module is standard-library only, performs no network I/O
and never calls a model.  Repeated calls with unchanged files produce the same
:class:`Features` value.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .task import Task

__all__ = ["DETERMINISTIC_SOLVERS", "Features", "extract_features"]


#: Mirrors ``_MAX_FILES``-style bounds so a pathological directory cannot slow
#: down the estimator.  Challenge tasks are far below these limits.
_MAX_FILES: Final = 64
_MAX_KB_FILES: Final = 512
#: Coding solver truncation limits (see ``harness/solvers/coding.py``).
_SOURCE_LIMIT: Final = 12_000
_TEST_LIMIT: Final = 8_000
_DEFAULT_FILE_LIMIT: Final = 20_000

#: Solvers that run without any LLM call.
DETERMINISTIC_SOLVERS: Final = frozenset({"math", "search", "data_analysis"})

_TEXT_SUFFIXES: Final = frozenset(
    {
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        ".csv",
        ".tsv",
        ".log",
        ".ini",
        ".cfg",
        ".conf",
        ".toml",
        ".yaml",
        ".yml",
    }
)
_CODE_SUFFIXES: Final = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".sh",
        ".bash",
        ".zsh",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".php",
        ".lua",
        ".sql",
    }
)
_JSON_SUFFIXES: Final = frozenset({".json", ".jsonl", ".ndjson"})
_PROMPT_SUFFIXES: Final = _TEXT_SUFFIXES | _CODE_SUFFIXES | _JSON_SUFFIXES


@dataclass(frozen=True, slots=True)
class Features:
    """Deterministic task features used by :class:`harness.estimator.CostEstimator`.

    ``text_chars``, ``code_chars`` and ``json_chars`` are the character counts
    of the files that may enter the prompt, split by content type so the
    estimator can apply a different ``chars_per_token`` coefficient to each
    (spec 6.3).  They are additive with ``instruction_chars``:
    :attr:`prompt_chars` returns the sum without any solver scaffolding.
    """

    task_type: str
    instruction_chars: int
    file_count: int
    total_file_bytes: int
    kb_bytes: int
    has_tests: bool
    solver: str
    model: str
    text_chars: int = 0
    code_chars: int = 0
    json_chars: int = 0

    @property
    def prompt_chars(self) -> int:
        """Characters of user-visible prompt content (no system scaffolding)."""

        return self.instruction_chars + self.text_chars + self.code_chars + self.json_chars

    def as_dict(self) -> dict[str, object]:
        return {
            "task_type": self.task_type,
            "instruction_chars": self.instruction_chars,
            "file_count": self.file_count,
            "total_file_bytes": self.total_file_bytes,
            "kb_bytes": self.kb_bytes,
            "has_tests": self.has_tests,
            "solver": self.solver,
            "model": self.model,
            "text_chars": self.text_chars,
            "code_chars": self.code_chars,
            "json_chars": self.json_chars,
            "prompt_chars": self.prompt_chars,
        }


def _is_test_file(path: Path) -> bool:
    name = path.name
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")


def _content_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _JSON_SUFFIXES:
        return "json"
    if suffix in _CODE_SUFFIXES:
        return "code"
    return "text"


def _prompt_files(workdir: Path, solver: str) -> list[tuple[Path, int]]:
    """Return ``(path, char_limit)`` pairs that the solver may put in a prompt."""

    if solver in DETERMINISTIC_SOLVERS:
        return []
    try:
        if solver == "coding":
            # Exactly what ``harness.solvers.coding._source_files`` /
            # ``_test_files`` read: top-level ``*.py`` only.
            files: list[tuple[Path, int]] = []
            for path in sorted(workdir.glob("*.py")):
                if len(files) >= _MAX_FILES:
                    break
                if path.is_file():
                    files.append((path, _TEST_LIMIT if _is_test_file(path) else _SOURCE_LIMIT))
            return files

        files = []
        for path in sorted(workdir.iterdir()):
            if len(files) >= _MAX_FILES:
                break
            if path.is_file() and path.suffix.lower() in _PROMPT_SUFFIXES:
                files.append((path, _DEFAULT_FILE_LIMIT))
        return files
    except OSError:  # pragma: no cover - unreadable workdir
        return []


def _kb_bytes(workdir: Path) -> int:
    """Bytes under ``kb/`` (a cheap feature, no content is read)."""

    kb = workdir / "kb"
    if not kb.is_dir():
        return 0
    total = 0
    seen = 0
    try:
        for path in sorted(kb.rglob("*")):
            if seen >= _MAX_KB_FILES:
                break
            if not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:  # pragma: no cover - file vanished
                continue
            seen += 1
    except OSError:  # pragma: no cover - unreadable kb
        return total
    return total


def _has_tests(workdir: Path, files: Iterable[Path]) -> bool:
    if any(_is_test_file(path) for path in files):
        return True
    try:
        return (workdir / "tests").is_dir()
    except OSError:  # pragma: no cover
        return False


def extract_features(task: Task, solver: str, model: str) -> Features:
    """Build :class:`Features` for ``task`` under a given solver/model.

    Only files the solver could send to the model are read.  Unreadable files
    are skipped rather than failing the estimate.
    """

    solver_name = str(solver or "generic")
    model_name = str(model or "unknown")
    instruction = getattr(task, "instruction", None) or ""
    task_type = str(getattr(task, "task_type", None) or "unknown")

    workdir: Path | None = getattr(task, "workdir", None)
    if workdir is None:
        task_dir = getattr(task, "task_dir", None)
        workdir = Path(task_dir) if task_dir is not None else None
    if workdir is not None:
        workdir = Path(workdir)

    candidates = _prompt_files(workdir, solver_name) if workdir is not None else []

    file_count = 0
    total_file_bytes = 0
    text_chars = 0
    code_chars = 0
    json_chars = 0

    for path, limit in candidates:
        try:
            size = path.stat().st_size
        except OSError:  # pragma: no cover - file vanished
            continue
        file_count += 1
        total_file_bytes += size
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                content = handle.read(limit + 1)
        except OSError:  # pragma: no cover - unreadable file
            continue
        chars = min(len(content), limit)
        kind = _content_kind(path)
        if kind == "json":
            json_chars += chars
        elif kind == "code":
            code_chars += chars
        else:
            text_chars += chars

    return Features(
        task_type=task_type,
        instruction_chars=len(instruction),
        file_count=file_count,
        total_file_bytes=total_file_bytes,
        kb_bytes=_kb_bytes(workdir) if workdir is not None else 0,
        has_tests=_has_tests(workdir, (path for path, _ in candidates)) if workdir is not None else False,
        solver=solver_name,
        model=model_name,
        text_chars=text_chars,
        code_chars=code_chars,
        json_chars=json_chars,
    )
