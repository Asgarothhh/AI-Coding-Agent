"""Pure evaluation metrics for the final contour (D2, R2-08).

Rows are mappings shaped like one line of ``trace.jsonl`` joined with the
matching ``predictions.jsonl`` record (join by ``task_id``).  The functions in
this module are pure: no I/O, no global state, no clock and no RNG.  Values
that a metric needs but a row does not provide are excluded from that metric
and counted in the result, never silently replaced with zero.

Modules are stdlib-only (spec 12.5: eval must stay offline).
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "DEFAULT_HOLDOUT_FRACTION",
    "DEFAULT_SPLIT_SEED",
    "forecast_metrics",
    "interval_metrics",
    "split_by_task_id",
    "summarize_outcomes",
]

DEFAULT_HOLDOUT_FRACTION = 0.3
DEFAULT_SPLIT_SEED = "v5"

SOLVED = "solved"
FAILED = "failed"
SKIPPED = "skipped"
ERROR = "error"
OTHER = "other"


# --------------------------------------------------------------------------- #
# Value helpers
# --------------------------------------------------------------------------- #
def _as_float(value: Any) -> float | None:
    """Return a finite float or ``None`` for missing/malformed values."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_rows(rows: Iterable[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    """Materialise rows and reject non-mapping entries with a clear error."""

    materialised: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError(f"{name}() expects mapping rows, got {type(row).__name__}")
        materialised.append(row)
    return materialised


def _status(row: Mapping[str, Any]) -> str:
    raw = row.get("status")
    return str(raw).strip().lower() if raw is not None else ""


def _task_id(row: Mapping[str, Any]) -> str | None:
    raw = row.get("task_id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _reward(row: Mapping[str, Any]) -> float:
    return _as_float(row.get("reward")) or 0.0


def _actual_cost(row: Mapping[str, Any]) -> float | None:
    """Actual cost of a task: ``y_actual`` when recorded, else ``tokens``."""

    actual = _as_float(row.get("y_actual"))
    if actual is not None:
        return actual
    return _as_float(row.get("tokens"))


# --------------------------------------------------------------------------- #
# Outcome metrics (the primary criterion)
# --------------------------------------------------------------------------- #
def summarize_outcomes(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise solved/failed/skipped/errors, score and budget usage.

    ``score`` is the sum of ``reward`` over confirmed ``solved`` rows only, as
    required by the evaluation spec.  ``budget_used`` sums the actual cost
    (``y_actual``, falling back to ``tokens``) of every row that recorded one.
    ``overruns`` counts rows where ``y_actual > y_hat``; ``overrun_rate`` uses
    only rows where both values are present (``overrun_n``).  A non-zero
    overrun count means the run is not accepted as successful (spec 12.1).
    """

    materialised = _as_rows(rows, "summarize_outcomes")
    counts = {SOLVED: 0, FAILED: 0, SKIPPED: 0, ERROR: 0, OTHER: 0}
    score = 0.0
    budget_used = 0.0
    overruns = 0
    overrun_n = 0
    rows_without_cost = 0
    task_ids: set[str] = set()

    for row in materialised:
        status = _status(row)
        if status == SOLVED:
            counts[SOLVED] += 1
            reward = _reward(row)
            if reward > 0:
                score += reward
        elif status == FAILED:
            counts[FAILED] += 1
        elif status == SKIPPED:
            counts[SKIPPED] += 1
        elif status.startswith(ERROR):
            counts[ERROR] += 1
        else:
            counts[OTHER] += 1

        task_id = _task_id(row)
        if task_id is not None:
            task_ids.add(task_id)

        cost = _actual_cost(row)
        if cost is None:
            rows_without_cost += 1
        else:
            budget_used += cost

        actual = _as_float(row.get("y_actual"))
        forecast = _as_float(row.get("y_hat"))
        if actual is not None and forecast is not None:
            overrun_n += 1
            if actual > forecast:
                overruns += 1

    row_count = len(materialised)
    solved = counts[SOLVED]
    return {
        "rows": row_count,
        "tasks": len(task_ids),
        "solved": solved,
        "failed": counts[FAILED],
        "skipped": counts[SKIPPED],
        "errors": counts[ERROR],
        "other": counts[OTHER],
        "score": score,
        "budget_used": budget_used,
        "rows_without_cost": rows_without_cost,
        "overruns": overruns,
        "overrun_n": overrun_n,
        "overrun_rate": (overruns / overrun_n) if overrun_n else None,
        "solve_rate": (solved / row_count) if row_count else None,
    }


# --------------------------------------------------------------------------- #
# Forecast diagnostics (MAPE-family, diagnostic only)
# --------------------------------------------------------------------------- #
def forecast_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """MAE, MdAPE, WAPE and bias for ``y_hat`` against ``y_actual``.

    Only rows with ``y_actual > 0`` are used, as required by the evaluation
    spec; ``zero_cost_excluded`` reports how many rows were dropped for that
    reason.  Rows without a forecast or without an actual cost are excluded
    and counted separately.  ``mdape`` and ``wape`` are percentages (100 means
    100%); ``bias`` is ``mean(y_hat / y_actual)`` and is a ratio (``1.0`` is
    unbiased).
    """

    materialised = _as_rows(rows, "forecast_metrics")
    absolute_errors: list[float] = []
    percentage_errors: list[float] = []
    ratios: list[float] = []
    sum_actual = 0.0
    sum_absolute = 0.0
    zero_cost_excluded = 0
    missing_forecast = 0
    missing_actual = 0

    for row in materialised:
        actual = _as_float(row.get("y_actual"))
        if actual is None:
            missing_actual += 1
            continue
        if actual <= 0:
            zero_cost_excluded += 1
            continue
        forecast = _as_float(row.get("y_hat"))
        if forecast is None:
            missing_forecast += 1
            continue

        error = abs(forecast - actual)
        absolute_errors.append(error)
        percentage_errors.append(error / actual * 100.0)
        ratios.append(forecast / actual)
        sum_actual += actual
        sum_absolute += error

    n = len(absolute_errors)
    return {
        "rows": len(materialised),
        "n": n,
        "mae": (sum_absolute / n) if n else None,
        "mdape": (statistics.median(percentage_errors) if n else None),
        "wape": ((sum_absolute / sum_actual) * 100.0 if sum_actual > 0 else None),
        "bias": (statistics.fmean(ratios) if n else None),
        "zero_cost_excluded": zero_cost_excluded,
        "missing_forecast": missing_forecast,
        "missing_actual": missing_actual,
    }


# --------------------------------------------------------------------------- #
# Prediction-interval diagnostics
# --------------------------------------------------------------------------- #
def interval_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """PICP and MPIW for prediction intervals ``[expected_cost, upper_cost]``.

    A row enters the metric only when both bounds and ``y_actual`` are present
    and the interval is not inverted.  ``picp`` is the share of actual costs
    inside the interval (target: the configured coverage), ``mpiw`` is the mean
    interval width.  Both are ``None`` when no row qualifies.
    """

    materialised = _as_rows(rows, "interval_metrics")
    covered = 0
    widths: list[float] = []
    missing_bounds = 0
    missing_actual = 0
    invalid_bounds = 0

    for row in materialised:
        actual = _as_float(row.get("y_actual"))
        lower = _as_float(row.get("expected_cost"))
        upper = _as_float(row.get("upper_cost"))
        if actual is None:
            missing_actual += 1
            continue
        if lower is None or upper is None:
            missing_bounds += 1
            continue
        if upper < lower:
            invalid_bounds += 1
            continue
        widths.append(upper - lower)
        if lower <= actual <= upper:
            covered += 1

    n = len(widths)
    return {
        "rows": len(materialised),
        "n": n,
        "covered": covered,
        "picp": (covered / n) if n else None,
        "mpiw": (statistics.fmean(widths) if n else None),
        "missing_bounds": missing_bounds,
        "missing_actual": missing_actual,
        "invalid_bounds": invalid_bounds,
    }


# --------------------------------------------------------------------------- #
# Deterministic task-level split
# --------------------------------------------------------------------------- #
def _bucket(task_id: str, seed: str) -> float:
    """Map a task id to a stable uniform value in ``[0, 1)``."""

    digest = hashlib.sha256(f"{seed}:{task_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def split_by_task_id(
    rows: Iterable[Mapping[str, Any]],
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    seed: str = DEFAULT_SPLIT_SEED,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split rows into ``(train, test)`` by hashing ``task_id``.

    The split is deterministic for a given ``(seed, holdout_fraction)`` and is
    independent of row order.  Every row of one task always lands on the same
    side, so repeated calls/records of a task cannot leak into both parts.
    """

    fraction = float(holdout_fraction)
    if not 0.0 <= fraction <= 1.0:
        raise ValueError(
            f"holdout_fraction must be within [0, 1], got {holdout_fraction!r}"
        )
    materialised = _as_rows(rows, "split_by_task_id")

    train: list[Mapping[str, Any]] = []
    test: list[Mapping[str, Any]] = []
    for row in materialised:
        task_id = _task_id(row)
        if task_id is None:
            raise ValueError(
                "split_by_task_id() requires a non-empty task_id on every row"
            )
        target = test if _bucket(task_id, str(seed)) < fraction else train
        target.append(row)
    return train, test
