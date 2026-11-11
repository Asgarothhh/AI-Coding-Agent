"""Select the model from measured metrics (R2-07).

The competition only allows four model aliases.  ``harness/model_stats.json``
records the measured live runs (score, solved count, cost) for the aliases we
have actually tested.  This module turns that file into an explicit,
auditable decision:

* ``metrics`` picks the allowlisted model with the highest ``score / cost``
  among entries with ``tasks > 0``; ties prefer the higher solved count and
  then the lower cost;
* ``fixed`` always returns the caller-supplied default;
* anything missing, malformed or empty falls back to the default model with a
  human-readable reason.  Selection never raises and never silently invents a
  model outside :data:`harness.estimator.ALLOWED_MODELS`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .estimator import ALLOWED_MODELS

__all__ = [
    "ALLOWED_MODELS",
    "DEFAULT_STATS_PATH",
    "ModelSelection",
    "select_model",
]

DEFAULT_STATS_PATH = Path(__file__).with_name("model_stats.json")

_POLICIES = ("fixed", "metrics")

#: Metadata keys tolerated in a flat stats document (when there is no
#: explicit ``models`` mapping).
_META_KEYS = frozenset(
    {"source", "date", "measured_at", "measurement", "notes", "version"}
)


@dataclass(frozen=True)
class ModelSelection:
    """One auditable model choice.

    ``stats`` is the winning entry from the stats file plus its ``source`` and
    ``date`` provenance.  It is empty when the policy fell back to the default.
    """

    model: str
    reason: str
    policy: str
    stats: Mapping[str, Any] = field(default_factory=dict)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _load_stats(path: Path) -> tuple[dict[str, dict[str, float]], str | None, str | None]:
    """Return ``(model -> metrics, source, date)`` or raise ``TypeError``/``ValueError``."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise TypeError("stats root is not a JSON object")

    source = data.get("source")
    measured_at = data.get("date", data.get("measured_at"))
    raw_models = data.get("models", data)
    if not isinstance(raw_models, Mapping):
        raise TypeError("stats 'models' is not a JSON object")

    entries: dict[str, dict[str, float]] = {}
    for name, raw in raw_models.items():
        key = str(name).strip()
        if not key or (data.get("models") is None and key in _META_KEYS):
            continue
        if not isinstance(raw, Mapping):
            continue
        tasks = _finite(raw.get("tasks"))
        solved = _finite(raw.get("solved"))
        score = _finite(raw.get("score"))
        cost = _finite(raw.get("cost"))
        if tasks is None or solved is None or score is None or cost is None:
            continue
        entries[key] = {
            "tasks": tasks,
            "solved": solved,
            "score": score,
            "cost": cost,
        }
    return (
        entries,
        str(source) if source is not None else None,
        str(measured_at) if measured_at is not None else None,
    )


def _fallback(default: str, policy: str, reason: str) -> ModelSelection:
    return ModelSelection(model=default, reason=reason, policy=policy, stats={})


def select_model(
    default: str,
    *,
    policy: str = "metrics",
    stats_path: str | Path | None = None,
    allowed: Iterable[str] | None = None,
) -> ModelSelection:
    """Choose a model according to ``policy``.

    ``default`` is always returned verbatim when the policy is ``fixed`` or
    when no measured entry is eligible.  ``allowed`` defaults to the four
    competition aliases.
    """

    default_name = str(default or "").strip()
    policy_name = str(policy or "").strip().lower()
    allowed_set = frozenset(
        str(name).strip() for name in (allowed if allowed is not None else ALLOWED_MODELS)
    )
    path = Path(stats_path) if stats_path is not None else DEFAULT_STATS_PATH

    if policy_name == "fixed":
        return _fallback(
            default_name,
            "fixed",
            f"fixed policy: using default model {default_name!r}",
        )
    if policy_name not in _POLICIES:
        return _fallback(
            default_name,
            policy_name or "unknown",
            f"unknown model policy {policy_name or policy!r}; "
            f"falling back to default model {default_name!r}",
        )

    try:
        entries, source, measured_at = _load_stats(path)
    except FileNotFoundError:
        return _fallback(
            default_name,
            "metrics",
            f"stats file not found: {path}; falling back to default model {default_name!r}",
        )
    except (OSError, TypeError, ValueError) as exc:
        return _fallback(
            default_name,
            "metrics",
            f"invalid stats file {path}: {type(exc).__name__}: {exc}; "
            f"falling back to default model {default_name!r}",
        )

    eligible: dict[str, dict[str, float]] = {}
    for name, entry in entries.items():
        if name not in allowed_set:
            continue
        if entry["tasks"] <= 0 or entry["cost"] <= 0.0:
            continue
        if entry["score"] < 0.0 or entry["solved"] < 0.0:
            continue
        eligible[name] = dict(entry)

    if not eligible:
        return _fallback(
            default_name,
            "metrics",
            f"no eligible measured model in {path} "
            f"(allowed: {', '.join(sorted(allowed_set))}); "
            f"falling back to default model {default_name!r}",
        )

    for entry in eligible.values():
        entry["score_per_cost"] = entry["score"] / entry["cost"]

    # Highest score/cost, then most solved, then cheapest, then a stable name
    # order so the decision is fully deterministic.
    winner = min(
        eligible.items(),
        key=lambda item: (
            -item[1]["score_per_cost"],
            -item[1]["solved"],
            item[1]["cost"],
            item[0],
        ),
    )
    name, metrics = winner
    stats: dict[str, Any] = dict(metrics)
    if source is not None:
        stats["source"] = source
    if measured_at is not None:
        stats["date"] = measured_at
    reason = (
        f"metrics policy: highest measured score/cost "
        f"{metrics['score_per_cost']:.4f} "
        f"(score={metrics['score']:g}, cost={metrics['cost']:g}, "
        f"solved={metrics['solved']:g}/{metrics['tasks']:g})"
    )
    return ModelSelection(model=name, reason=reason, policy="metrics", stats=stats)
