"""Generic LLM solver for task types without a specialised solver.

This is the last-resort path used by the runner for unknown ``task_type``
values.  It sends one bounded digest of the task instruction and workspace to
the model, asks for the final answer in the shape required by the task's
``check`` field, and extracts exactly that answer.

The context is deliberately small (``_CONTEXT_CHAR_CAP`` characters of user
prompt): the full instruction, a workspace inventory, and a few relevant text
files.  Files named in the instruction win over small unnamed files so a
single call stays focused and cheap.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..budget import BudgetExhausted
from ..llm import LLMError
from ..task import Task
from .base import SolverContext, SolveResult

__all__ = ["solve"]

log = logging.getLogger(__name__)

#: Hard cap on the user prompt sent by this solver.
_CONTEXT_CHAR_CAP: Final = 8_000
_INSTRUCTION_LIMIT: Final = 6_200
_INVENTORY_LIMIT: Final = 1_400
_INVENTORY_ENTRIES: Final = 120
_MAX_FILES: Final = 12
_PER_FILE_LIMIT: Final = 2_400
_MAX_SCANNED_FILES: Final = 500
_MAX_FILE_BYTES: Final = 262_144
_MAX_ANSWER_CHARS: Final = 2_000

_SHAPE_INT: Final = "exact_int"
_SHAPE_STR: Final = "exact_str"
_SHAPE_DONE: Final = "tests_pass"
_SHAPE_UNKNOWN: Final = "unknown"

_SHAPE_HINTS: dict[str, str] = {
    _SHAPE_INT: (
        "Reply with exactly one integer (digits only, optional leading '-') "
        "on a single line and nothing else."
    ),
    _SHAPE_STR: (
        "Reply with the exact answer string on a single line, without quotes "
        "or commentary."
    ),
    _SHAPE_DONE: (
        "If the requested work is complete, reply with exactly DONE; otherwise "
        "reply with the blocking reason on one line."
    ),
    _SHAPE_UNKNOWN: "Reply with the final answer on a single line, without explanation.",
}

_IGNORED_FILES: Final = frozenset(
    {"answer.txt", "task.json", "solutions.json", "trace.jsonl"}
)

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
        ".json",
        ".jsonl",
        ".ndjson",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".sh",
        ".bash",
        ".sql",
        ".html",
        ".htm",
        ".xml",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".lua",
        ".cs",
        ".kt",
        ".swift",
        ".scala",
    }
)

_FILE_REF_RE: Final = re.compile(
    r"(?<![\w./-])((?:\./)?(?:[\w.-]+/)*[\w.-]+"
    r"\.(?:csv|tsv|json|jsonl|ndjson|md|markdown|txt|py|yaml|yml|log|ini|cfg|"
    r"conf|toml|sh|sql|html?|xml))(?![\w])",
    re.IGNORECASE,
)
_FENCE_RE: Final = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_LABEL_RE: Final = re.compile(
    r"^(?:final\s+answer|answer|result|output)\s*[:\-]\s*", re.IGNORECASE
)
_ANSWER_IS_RE: Final = re.compile(
    r"^the\s+answer\s+is\s*[:\-]?\s*", re.IGNORECASE
)
_INT_RE: Final = re.compile(r"[+-]?\d+")


@dataclass(frozen=True, slots=True)
class _FileCandidate:
    path: Path
    relative: str
    size: int


def _shape_for(check: str) -> str:
    """Map a ``task.json`` ``check`` value to an answer shape."""

    value = (check or "").strip().lower()
    if value.startswith(_SHAPE_INT):
        return _SHAPE_INT
    if value in {"unittest", _SHAPE_DONE}:
        return _SHAPE_DONE
    if value.startswith(_SHAPE_STR):
        return _SHAPE_STR
    return _SHAPE_UNKNOWN


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n... [truncated]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _instruction_refs(instruction: str) -> set[str]:
    refs: set[str] = set()
    for match in _FILE_REF_RE.finditer(instruction):
        raw = match.group(1).replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        if raw.lower() in _IGNORED_FILES:
            continue
        refs.add(raw.lower())
    return refs


def _scan_workspace(workdir: Path) -> list[_FileCandidate]:
    if not workdir.is_dir():
        return []
    candidates: list[_FileCandidate] = []
    try:
        paths = sorted(workdir.rglob("*"))
    except OSError:  # pragma: no cover - unreadable workspace
        return []
    for path in paths:
        if len(candidates) >= _MAX_SCANNED_FILES:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(workdir).as_posix()
            size = path.stat().st_size
        except (OSError, ValueError):
            continue
        if path.name.lower() in _IGNORED_FILES:
            continue
        candidates.append(_FileCandidate(path=path, relative=relative, size=size))
    return candidates


def _matches_ref(candidate: _FileCandidate, refs: set[str]) -> bool:
    if not refs:
        return False
    relative = candidate.relative.lower()
    name = candidate.path.name.lower()
    return (
        relative in refs
        or name in refs
        or any(relative.endswith("/" + ref) for ref in refs)
    )


def _inventory_order(
    candidates: Sequence[_FileCandidate], refs: set[str]
) -> list[_FileCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            0 if _matches_ref(candidate, refs) else 1,
            candidate.relative,
        ),
    )


def _content_order(
    candidates: Sequence[_FileCandidate], refs: set[str]
) -> list[_FileCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            0 if _matches_ref(candidate, refs) else 1,
            0 if candidate.path.suffix.lower() in _TEXT_SUFFIXES else 1,
            candidate.size,
            candidate.relative,
        ),
    )


def _inventory_block(candidates: Sequence[_FileCandidate]) -> str:
    lines = ["WORKSPACE INVENTORY:"]
    if not candidates:
        lines.append("- (empty workspace)")
        return "\n".join(lines)

    used = len(lines[0])
    for index, candidate in enumerate(candidates):
        line = f"- {candidate.relative} ({candidate.size} bytes)"
        if used + len(line) + 1 > _INVENTORY_LIMIT:
            remaining = len(candidates) - index
            lines.append(f"- ... {remaining} more file(s) omitted")
            break
        lines.append(line)
        used += len(line) + 1
        if index + 1 >= _INVENTORY_ENTRIES:
            remaining = len(candidates) - index - 1
            if remaining > 0:
                lines.append(f"- ... {remaining} more file(s) omitted")
            break
    return "\n".join(lines)


def _read_text_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    if "\x00" in text:
        return None
    return text


def _system_prompt(shape: str) -> str:
    return (
        "You solve one self-contained task using only the instruction and the "
        "workspace digest below. Do not describe your reasoning. "
        + _SHAPE_HINTS[shape]
    )


def _build_user_prompt(task: Task, shape: str, retry: bool = False) -> str:
    instruction = task.instruction.strip() or "(empty instruction)"
    instruction = _truncate(instruction, _INSTRUCTION_LIMIT)

    candidates = _scan_workspace(task.workdir)
    refs = _instruction_refs(task.instruction)

    prefix_parts = [
        "TASK INSTRUCTION:",
        instruction,
        "",
        "REQUIRED ANSWER FORMAT:",
        _SHAPE_HINTS[shape],
        "",
        _inventory_block(_inventory_order(candidates, refs)),
        "",
        "FILE CONTENTS:",
    ]
    prefix = "\n".join(prefix_parts) + "\n"
    remaining = _CONTEXT_CHAR_CAP - len(prefix)

    if remaining <= 0:
        prompt = prefix[:_CONTEXT_CHAR_CAP]
    else:
        blocks: list[str] = []
        for candidate in _content_order(candidates, refs):
            if remaining <= 80 or len(blocks) >= _MAX_FILES:
                break
            text = _read_text_file(candidate.path)
            if text is None:
                continue
            limit = min(_PER_FILE_LIMIT, max(0, remaining - 60))
            if limit <= 0:
                break
            body = _truncate(text, limit)
            block = f"### FILE: {candidate.relative}\n```\n{body}\n```\n"
            if len(block) > remaining:
                block = _truncate(block, remaining)
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)

        if blocks:
            prompt = prefix + "\n".join(blocks)
        else:
            prompt = prefix + "(no readable text files)\n"

    if retry:
        prompt += (
            "\n\nYour previous reply did not contain a usable final answer. "
            "Reply with ONLY the required answer, without explanation or code fences."
        )
    return prompt[:_CONTEXT_CHAR_CAP]


def _build_messages(task: Task, shape: str, retry: bool = False) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _system_prompt(shape)},
        {"role": "user", "content": _build_user_prompt(task, shape, retry=retry)},
    ]


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) >= 6:
        inner = stripped[3:-3]
        newline = inner.find("\n")
        if newline >= 0:
            inner = inner[newline + 1 :]
        return inner.strip()
    blocks = _FENCE_RE.findall(stripped)
    if blocks:
        return blocks[-1].strip()
    lines = [
        line for line in stripped.splitlines() if not line.strip().startswith("```")
    ]
    return "\n".join(lines).strip()


def _stringify_json(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("answer", "result", "output", "final_answer", "final"):
            if key in value and value[key] is not None:
                return _stringify_json(value[key])
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return None


def _json_answer(text: str) -> str | None:
    stripped = text.strip()
    try:
        obj: object = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        start = stripped.find("{")
        if start < 0:
            return None
        try:
            obj, _ = json.JSONDecoder().raw_decode(stripped[start:])
        except json.JSONDecodeError:
            return None
    return _stringify_json(obj)


def _clean_line(value: str) -> str:
    line = value.strip()
    if len(line) >= 2 and line.startswith("`") and line.endswith("`"):
        line = line[1:-1].strip()
    for _ in range(2):
        if len(line) >= 2 and line[0] == line[-1] and line[0] in {'"', "'"}:
            line = line[1:-1].strip()
    for _ in range(2):
        cleaned = _LABEL_RE.sub("", line, count=1)
        cleaned = _ANSWER_IS_RE.sub("", cleaned, count=1)
        if cleaned == line:
            break
        line = cleaned.strip()
    return line


def _extract_answer(text: str, shape: str) -> str | None:
    """Extract the final answer from a model reply.

    Code fences are stripped first, then a JSON object's ``answer`` / ``result``
    / ``output`` value is preferred, and otherwise the last non-empty line is
    used.  The whole reply (reasoning) is never returned.
    """

    if not text or not text.strip():
        return None

    cleaned = _strip_code_fences(text)
    value = _json_answer(cleaned)
    if value is None:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        for line in reversed(lines):
            candidate = _clean_line(line)
            if candidate:
                value = candidate
                break
    if value is None:
        return None

    value = _clean_line(value)
    if "\n" in value:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        value = lines[-1] if lines else ""
    if not value or len(value) > _MAX_ANSWER_CHARS:
        return None

    if shape == _SHAPE_INT:
        compact = value.replace(",", "").replace(" ", "").rstrip(".")
        if _INT_RE.fullmatch(compact):
            return compact
        return value
    if shape == _SHAPE_DONE and value.rstrip(".").strip().upper() == "DONE":
        return "DONE"
    return value


def _is_valid(answer: str | None, shape: str) -> bool:
    if not answer or "\n" in answer or len(answer) > _MAX_ANSWER_CHARS:
        return False
    if shape == _SHAPE_INT:
        return _INT_RE.fullmatch(answer) is not None
    if shape == _SHAPE_DONE:
        return answer == "DONE"
    return True


def _model_name(ctx: SolverContext) -> str:
    config = getattr(ctx.llm, "config", None)
    return str(getattr(config, "model", "") or "unknown")


def _notes(model: str, attempts: int, why: str) -> str:
    return f"model={model or 'unknown'}; attempts={attempts}; {why}"


def solve(task: Task, ctx: SolverContext) -> SolveResult:
    """Solve an unknown task type with one bounded model call (+ one retry)."""

    shape = _shape_for(task.check)
    if ctx.llm is None or not getattr(ctx.llm, "available", False):
        return SolveResult(
            None,
            0.0,
            "generic",
            _notes("unknown", 0, "no LLM is available for this task type"),
        )

    model = _model_name(ctx)
    tokens = 0.0
    attempts = 0
    last_problem = "no attempt made"

    for attempt in range(2):
        messages = _build_messages(task, shape, retry=attempt > 0)
        estimated = min(int(ctx.options.max_tokens), 512) + 160
        try:
            ctx.budget.check(task.task_id, estimated_cost=estimated)
        except BudgetExhausted as exc:
            log.info("generic solver %s: budget stop (%s)", task.task_id, exc)
            return SolveResult(
                None,
                tokens,
                "generic",
                f"budget stop: {exc} (model={model}; attempts={attempts})",
            )

        try:
            response = ctx.llm.chat(
                messages,
                max_tokens=ctx.options.max_tokens,
                attempt=attempt,
            )
        except BudgetExhausted as exc:
            log.info("generic solver %s: budget stop during call (%s)", task.task_id, exc)
            return SolveResult(
                None,
                tokens,
                "generic",
                f"budget stop: {exc} (model={model}; attempts={attempts})",
            )
        except LLMError as exc:
            log.warning("generic solver %s: LLM error: %s", task.task_id, exc)
            return SolveResult(
                None,
                tokens,
                "generic",
                f"LLM error: {exc} (model={model}; attempts={attempts})",
            )

        attempts += 1
        model = str(getattr(response, "model", "") or model)
        tokens += ctx.budget.charge(
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            task.task_id,
        )

        text = str(getattr(response, "text", "") or "")
        answer = _extract_answer(text, shape)
        if answer is not None and _is_valid(answer, shape):
            if ctx.options.verbose:
                log.info(
                    "generic solver %s: answer %r on attempt %d with model %s",
                    task.task_id,
                    answer,
                    attempts,
                    model,
                )
            return SolveResult(
                answer,
                tokens,
                "generic",
                _notes(model, attempts, "answer extracted"),
            )

        last_problem = "empty answer" if not text.strip() else "malformed answer"
        if ctx.options.verbose:
            log.info(
                "generic solver %s: attempt %d produced %s",
                task.task_id,
                attempts,
                last_problem,
            )

    return SolveResult(
        None,
        tokens,
        "generic",
        _notes(model, attempts, f"{last_problem} after {attempts} attempt(s)"),
    )
