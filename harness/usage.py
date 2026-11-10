"""Normalisation of LLM ``usage`` payloads into one accounting record.

The challenge proxy is OpenAI-compatible but the exact field names and the
meaning of *reasoning* tokens are not guaranteed.  This module is the single
place that translates a raw ``usage`` mapping into :class:`TokenUsage` so the
budget ledger never has to guess:

* input  — ``input_tokens`` or ``prompt_tokens``;
* output — ``output_tokens`` or ``completion_tokens``;
* reasoning — ``reasoning_tokens`` or a nested
  ``completion_tokens_details.reasoning_tokens`` /
  ``output_tokens_details.reasoning_tokens``.

Cost follows the competition formula ``input / 10 + output``.  Reasoning is a
diagnostic field and is never added to output twice.  If a proxy reports only
reasoning tokens and a zero output, reasoning is promoted to output exactly
once, because the proxy would still charge it as generated tokens.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "COST_INPUT_DIVISOR",
    "TokenUsage",
    "normalize_usage",
]

COST_INPUT_DIVISOR = 10.0

_INPUT_FIELDS = ("input_tokens", "prompt_tokens")
_OUTPUT_FIELDS = ("output_tokens", "completion_tokens")
_REASONING_FIELDS = ("reasoning_tokens",)
_REASONING_DETAIL_FIELDS = (
    "completion_tokens_details",
    "output_tokens_details",
)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Normalised usage for one paid LLM response.

    This is the single runtime record for the whole harness.  The constructor
    enforces the invariants the budget ledger relies on: token counts are
    non-negative integers and ``total_cost`` is a finite non-negative number.
    Callers with an untrusted proxy payload should use :func:`normalize_usage`,
    which tolerates malformed fields instead of raising.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_cost: float = 0.0
    source_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "reasoning_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, got {value!r}")
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value!r}")
        cost = self.total_cost
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise TypeError(f"total_cost must be numeric, got {cost!r}")
        number = float(cost)
        if not math.isfinite(number) or number < 0:
            raise ValueError(
                f"total_cost must be finite and >= 0, got {self.total_cost!r}"
            )
        object.__setattr__(self, "total_cost", number)

    @property
    def cost(self) -> float:
        """Cost in challenge units, computed from the token counts."""

        return self.input_tokens / COST_INPUT_DIVISOR + self.output_tokens


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _object_usage(value: Any) -> dict[str, Any] | None:
    """Best-effort extraction from a duck-typed usage object.

    ``harness.llm.LLMUsage`` and SDK usage objects expose the same fields as
    attributes.  They are converted without importing those modules, so this
    normalizer stays the single translation point.
    """

    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return None
    fields: dict[str, Any] = {}
    for name in (
        *_INPUT_FIELDS,
        *_OUTPUT_FIELDS,
        *_REASONING_FIELDS,
        "total_cost",
        "source_fields",
    ):
        item = getattr(value, name, None)
        if item is not None:
            fields[name] = item
    return fields or None


def _as_int(value: Any) -> int | None:
    """Best-effort non-negative integer coercion for proxy payloads."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number >= 0 else None


def _pick(
    raw: Mapping[str, Any], fields: tuple[str, ...]
) -> tuple[str | None, int | None]:
    for name in fields:
        if name not in raw:
            continue
        value = _as_int(raw.get(name))
        if value is not None:
            return name, value
    return None, None


def _reasoning(raw: Mapping[str, Any]) -> tuple[str | None, int | None]:
    field, value = _pick(raw, _REASONING_FIELDS)
    if value is not None:
        return field, value
    for parent in _REASONING_DETAIL_FIELDS:
        details = _as_mapping(raw.get(parent))
        if details is None:
            continue
        nested = _as_int(details.get("reasoning_tokens"))
        if nested is not None:
            return f"{parent}.reasoning_tokens", nested
    return None, None


def normalize_usage(raw: Mapping[str, Any] | None) -> TokenUsage:
    """Translate one proxy ``usage`` mapping into :class:`TokenUsage`.

    A missing, empty or malformed payload normalises to a zero-cost record so
    callers never crash on a proxy that omits usage.  ``total_cost`` is always
    the formula-derived cost; ``source_fields`` names the fields that were
    actually read, which makes proxy-shape regressions visible in traces.
    """

    if isinstance(raw, TokenUsage):
        return raw

    payload = _as_mapping(raw)
    if payload is None:
        payload = _object_usage(raw)
    if payload is None:
        return TokenUsage(source_fields=())

    # Some responses wrap usage under a ``usage`` key (or the caller passes the
    # whole response).  Only descend when no direct token field is present.
    direct_input, _ = _pick(payload, _INPUT_FIELDS)
    direct_output, _ = _pick(payload, _OUTPUT_FIELDS)
    if direct_input is None and direct_output is None:
        nested = _as_mapping(payload.get("usage"))
        if nested is not None:
            payload = nested

    source_fields: list[str] = []

    input_field, input_tokens = _pick(payload, _INPUT_FIELDS)
    input_tokens = input_tokens or 0
    if input_field is not None:
        source_fields.append(input_field)

    output_field, output_tokens = _pick(payload, _OUTPUT_FIELDS)
    output_tokens = output_tokens or 0
    if output_field is not None:
        source_fields.append(output_field)

    reasoning_field, reasoning_tokens = _reasoning(payload)
    reasoning_tokens = reasoning_tokens or 0
    if reasoning_field is not None:
        source_fields.append(reasoning_field)

    # Reasoning is charged as generated output.  If the proxy reported it only
    # in a diagnostic field, promote it once; never add it on top of an
    # output count that may already include it.
    if output_tokens == 0 and reasoning_tokens > 0:
        output_tokens = reasoning_tokens

    total_cost = input_tokens / COST_INPUT_DIVISOR + output_tokens

    # Preserve order while removing duplicates (e.g. a promoted field).
    seen: set[str] = set()
    ordered_sources: list[str] = []
    for name in source_fields:
        if name in seen:
            continue
        seen.add(name)
        ordered_sources.append(name)

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_cost=total_cost,
        source_fields=tuple(ordered_sources),
    )
