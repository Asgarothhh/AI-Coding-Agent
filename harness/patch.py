"""Minimal SEARCH/REPLACE patcher with unique-match safety.

Ladder: exact → newline/trailing whitespace → indentation → optional unique
fuzzy match (ratio >= 0.92). Zero or more than one match never writes.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from .store import atomic_write_text
from .task import resolve_task_path

__all__ = [
    "FUZZY_THRESHOLD",
    "PatchAmbiguous",
    "PatchError",
    "PatchNotFound",
    "SearchReplaceHunk",
    "apply_search_replace",
    "apply_search_replace_file",
    "closest_window",
    "is_protected_test_name",
    "parse_search_replace",
]

FUZZY_THRESHOLD = 0.92
_PROTECTED = re.compile(r"(^test_.*\.py$)|(.*_test\.py$)", re.IGNORECASE)


class PatchError(ValueError):
    """A SEARCH/REPLACE hunk could not be applied safely."""


class PatchNotFound(PatchError):
    def __init__(self, message: str, closest: str = "") -> None:
        super().__init__(message)
        self.closest = closest


class PatchAmbiguous(PatchError):
    """SEARCH matched more than one location."""


@dataclass(frozen=True, slots=True)
class SearchReplaceHunk:
    path: str
    search: str
    replace: str


@dataclass(frozen=True, slots=True)
class ApplyResult:
    text: str
    method: str


def is_protected_test_name(name: str) -> bool:
    candidate = Path(name.strip().replace("\\", "/")).name
    return bool(_PROTECTED.match(candidate))


def closest_window(haystack: str, needle: str, radius: int = 120) -> str:
    if not haystack:
        return ""
    if not needle.strip():
        return haystack[:radius]
    matcher = difflib.SequenceMatcher(a=haystack, b=needle, autojunk=False)
    match = matcher.find_longest_match(0, len(haystack), 0, len(needle))
    start = max(0, match.a - 40)
    end = min(len(haystack), match.a + max(match.size, 1) + radius)
    return haystack[start:end]


def _count_exact(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    count = 0
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return count
        count += 1
        start = index + max(len(needle), 1)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _rstrip_lines(text: str) -> str:
    return "\n".join(line.rstrip() for line in _normalize_newlines(text).split("\n"))


def _dedent_common(text: str) -> str:
    lines = _normalize_newlines(text).split("\n")
    indents = [len(line) - len(line.lstrip(" \t")) for line in lines if line.strip()]
    if not indents:
        return text
    width = min(indents)
    if width <= 0:
        return "\n".join(lines)
    stripped: list[str] = []
    for line in lines:
        if not line.strip():
            stripped.append("")
        else:
            stripped.append(line[width:])
    return "\n".join(stripped)


def _apply_unique(haystack: str, needle: str, replacement: str, method: str) -> ApplyResult | None:
    count = _count_exact(haystack, needle)
    if count == 1:
        return ApplyResult(haystack.replace(needle, replacement, 1), method)
    if count > 1:
        raise PatchAmbiguous(f"{method} match is ambiguous ({count} locations)")
    return None


def _fuzzy_unique(haystack: str, needle: str) -> str | None:
    if not needle.strip() or len(needle) < 8:
        return None
    window = max(len(needle), 8)
    best: tuple[float, int] | None = None
    second = 0.0
    limit = max(0, len(haystack) - window + 1)
    step = max(1, window // 8)
    for start in range(0, limit + 1, step):
        chunk = haystack[start : start + window]
        ratio = difflib.SequenceMatcher(a=chunk, b=needle, autojunk=False).ratio()
        if best is None or ratio > best[0]:
            if best is not None:
                second = best[0]
            best = (ratio, start)
        elif ratio > second:
            second = ratio
    if best is None or best[0] < FUZZY_THRESHOLD:
        return None
    if second >= FUZZY_THRESHOLD or (best[0] - second) < 0.02:
        return None
    return haystack[best[1] : best[1] + window]


def apply_search_replace(haystack: str, search: str, replace: str) -> ApplyResult:
    if not search:
        raise PatchNotFound("SEARCH is empty", closest_window(haystack, replace))

    exact = _apply_unique(haystack, search, replace, "exact")
    if exact is not None:
        return exact

    hay_nl = _normalize_newlines(haystack)
    search_nl = _normalize_newlines(search)
    replace_nl = _normalize_newlines(replace)
    newline = _apply_unique(hay_nl, search_nl, replace_nl, "newline")
    if newline is not None:
        return newline

    hay_ws = _rstrip_lines(haystack)
    search_ws = _rstrip_lines(search)
    replace_ws = _rstrip_lines(replace)
    whitespace = _apply_unique(hay_ws, search_ws, replace_ws, "whitespace")
    if whitespace is not None:
        return whitespace

    hay_ind = _dedent_common(haystack)
    search_ind = _dedent_common(search)
    indent = _apply_unique(hay_ind, search_ind, _dedent_common(replace), "indent")
    if indent is not None:
        mapped = _apply_unique(
            hay_nl,
            _normalize_newlines(search_ind),
            _normalize_newlines(replace),
            "indent",
        )
        if mapped is not None:
            return mapped
        raise PatchNotFound("SEARCH indent match could not be mapped", closest_window(haystack, search))

    raise PatchNotFound("SEARCH not found", closest_window(haystack, search))


def parse_search_replace(text: str, default_path: str = "") -> list[SearchReplaceHunk]:
    """Parse Aider-style SEARCH/REPLACE blocks."""

    pattern = re.compile(
        r"(?:(?:FILE:?\s*)?(?P<path>[\w./\\-]+\.py)\s*\n)?"
        r"<<<<<<< SEARCH\n(?P<search>.*?)\n=======\n(?P<replace>.*?)\n>>>>>>> REPLACE",
        re.DOTALL,
    )
    hunks: list[SearchReplaceHunk] = []
    for match in pattern.finditer(text):
        path = (match.group("path") or default_path).strip()
        hunks.append(
            SearchReplaceHunk(
                path=path,
                search=match.group("search"),
                replace=match.group("replace"),
            )
        )
    return hunks


def apply_search_replace_file(
    task_dir: Path,
    relative: str,
    search: str,
    replace: str,
    *,
    workdir: Path | None = None,
) -> ApplyResult:
    root = Path(task_dir)
    work = Path(workdir) if workdir is not None else root
    if is_protected_test_name(relative):
        raise PatchError(f"refusing to patch test file {relative!r}")
    unresolved = root / Path(relative)
    if unresolved.is_symlink() or (work / Path(relative).name).is_symlink():
        raise PatchError(f"refusing to patch symlink {relative!r}")
    try:
        safe = resolve_task_path(root, relative)
    except ValueError as exc:
        raise PatchError(str(exc)) from exc
    try:
        safe.relative_to(work.resolve())
    except ValueError as exc:
        raise PatchError(f"patch target escapes workdir: {relative}") from exc
    if not safe.is_file():
        raise PatchNotFound(f"file not found: {relative}")
    original = safe.read_text(encoding="utf-8")
    result = apply_search_replace(original, search, replace)
    atomic_write_text(safe, result.text)
    return result
