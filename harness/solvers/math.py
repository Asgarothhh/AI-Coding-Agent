"""Deterministic arithmetic word-problem solver.

The official examples use a small family of templates ("starts with N;
split/double/ship/receive ...").  This module parses those templates
sequentially and also exposes a safe arithmetic expression evaluator.  It
returns ``None`` when the instruction does not match, letting the runner
report a clean failure instead of guessing.
"""

from __future__ import annotations

import ast
import math
import re
from typing import Callable

from .base import SolveResult, SolverContext
from ..task import Task

__all__ = ["format_number", "safe_eval", "solve"]

_INITIAL_RE = re.compile(
    r"(?:starts?|begins?|initially\s+has|has)\s+(?:with\s+)?([\d,]+)",
    re.IGNORECASE,
)

_Operation = Callable[["re.Match[str]", int], int]

_OPERATION_PATTERNS: tuple[tuple[re.Pattern[str], int, _Operation], ...] = (
    (
        re.compile(r"\bsplit\s+(?:equally\s+)?into\s+(\d+)", re.IGNORECASE),
        0,
        lambda m, value: value // int(m.group(1)),
    ),
    (
        re.compile(r"\bdivid(?:e|ed)\s+(?:equally\s+)?(?:by|into)\s+(\d+)", re.IGNORECASE),
        0,
        lambda m, value: value // int(m.group(1)),
    ),
    (
        re.compile(
            r"\bships?\s+([\d,]+)\s+(?:boxes?|units?|items?)\s+per\s+day\s+for\s+(\d+)\s+days?",
            re.IGNORECASE,
        ),
        0,
        lambda m, value: value - _int(m.group(1)) * int(m.group(2)),
    ),
    (
        re.compile(r"\breceives?\s+(\d+)\s+deliveries?\s+of\s+([\d,]+)", re.IGNORECASE),
        0,
        lambda m, value: value + int(m.group(1)) * _int(m.group(2)),
    ),
    (
        re.compile(r"\bdoubled?\b", re.IGNORECASE),
        0,
        lambda _m, value: value * 2,
    ),
    (
        re.compile(r"\btripled?\b", re.IGNORECASE),
        0,
        lambda _m, value: value * 3,
    ),
    (
        re.compile(r"\bincreases?\s+by\s+([\d.]+)\s*%", re.IGNORECASE),
        1,
        lambda m, value: int(value * (1.0 + float(m.group(1)) / 100.0)),
    ),
    (
        re.compile(r"\bdecreases?\s+by\s+([\d.]+)\s*%", re.IGNORECASE),
        1,
        lambda m, value: int(value * (1.0 - float(m.group(1)) / 100.0)),
    ),
    (
        re.compile(
            r"\b(?:sells?|ships?|removes?|loses?|gives?\s+away)\s+([\d,]+)\s+(?:boxes?|units?|items?)?",
            re.IGNORECASE,
        ),
        2,
        lambda m, value: value - _int(m.group(1)),
    ),
    (
        re.compile(r"\breceives?\s+([\d,]+)\s+(?:boxes?|units?|items?)", re.IGNORECASE),
        2,
        lambda m, value: value + _int(m.group(1)),
    ),
    (
        re.compile(r"\b(?:adds?|gains?|gets?)\s+([\d,]+)\s+(?:boxes?|units?|items?)", re.IGNORECASE),
        2,
        lambda m, value: value + _int(m.group(1)),
    ),
    (
        re.compile(r"\bsubtracts?\s+([\d,]+)\s+(?:boxes?|units?|items?)", re.IGNORECASE),
        2,
        lambda m, value: value - _int(m.group(1)),
    ),
)

_EXPRESSION_AFTER_RE = re.compile(
    r"(?:compute|calculate|evaluate|what\s+is)\s*[:\-]?\s*([0-9][0-9\s+\-*/%().]*[0-9)])",
    re.IGNORECASE,
)
_PURE_EXPRESSION_RE = re.compile(r"^[\d\s+\-*/%().]+$")

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_MAX_EXPRESSION_LEN = 500
_MAX_ABS_CONSTANT = 1e15
_MAX_EXPONENT = 1000


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def format_number(value: int | float) -> str:
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        if value.is_integer() and abs(value) < 1e18:
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


def safe_eval(expression: str) -> int | float | None:
    """Evaluate a restricted arithmetic expression without ``eval``."""

    expr = expression.strip()
    if not expr or len(expr) > _MAX_EXPRESSION_LEN:
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("non-numeric constant")
            if abs(node.value) > _MAX_ABS_CONSTANT:
                raise ValueError("constant too large")
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = evaluate(node.operand)
            return operand if isinstance(node.op, ast.UAdd) else -operand
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > _MAX_EXPONENT or abs(left) > 1e9:
                    raise ValueError("exponent too large")
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
                raise ValueError("division by zero")
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                result = left / right
            elif isinstance(node.op, ast.FloorDiv):
                result = left // right
            elif isinstance(node.op, ast.Mod):
                result = left % right
            else:  # pragma: no cover - exhaustive
                result = left**right
            if isinstance(result, complex):
                raise ValueError("complex result")
            if isinstance(result, float) and not math.isfinite(result):
                raise ValueError("non-finite result")
            if abs(result) > _MAX_ABS_CONSTANT:
                raise ValueError("result too large")
            return result
        raise ValueError(f"unsupported expression node: {type(node).__name__}")

    try:
        return evaluate(tree)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def _extract_ops(instruction: str) -> list[tuple[int, Callable[[int], int]]] | None:
    initial = _INITIAL_RE.search(instruction)
    if initial is None:
        return None

    candidates: list[tuple[int, int, int, Callable[[int], int]]] = []
    for pattern, priority, operation in _OPERATION_PATTERNS:
        for match in pattern.finditer(instruction):
            candidates.append(
                (match.start(), match.end(), priority, lambda value, m=match, op=operation: op(m, value))
            )

    candidates.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))
    selected: list[tuple[int, Callable[[int], int]]] = []
    last_end = -1
    for start, end, _priority, operation in candidates:
        if start < last_end:
            continue
        selected.append((start, operation))
        last_end = end
    return selected


def solve(task: Task, ctx: SolverContext) -> SolveResult:  # noqa: ARG001 - uniform signature
    instruction = task.instruction

    initial = _INITIAL_RE.search(instruction)
    if initial is not None:
        value: int | float = _int(initial.group(1))
        ops = _extract_ops(instruction)
        if ops is None:  # pragma: no cover - initial was just found
            return SolveResult(None, 0.0, "math", "could not parse arithmetic operations")
        for _start, operation in ops:
            value = operation(value)
        return SolveResult(format_number(value), 0.0, "math", f"parsed {len(ops)} operation(s)")

    # Fall back to a single arithmetic expression if the instruction is one.
    candidate: str | None = None
    stripped = instruction.strip()
    if _PURE_EXPRESSION_RE.match(stripped):
        candidate = stripped
    else:
        match = _EXPRESSION_AFTER_RE.search(instruction)
        if match is not None:
            candidate = match.group(1).strip()

    if candidate is not None:
        result = safe_eval(candidate)
        if result is not None:
            return SolveResult(format_number(result), 0.0, "math", "evaluated arithmetic expression")

    return SolveResult(None, 0.0, "math", "no deterministic arithmetic template matched")
