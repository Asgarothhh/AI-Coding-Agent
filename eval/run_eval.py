#!/usr/bin/env python3.11
"""Final evaluation report for the budget-aware harness (D2, R2-08).

The script reads ``trace.jsonl`` and/or ``predictions.jsonl`` (one JSON object
per line), joins predictions to trace by ``task_id`` and reports, in order:

1. the primary outcome — score, solved, budget_used, overruns;
2. forecast diagnostics — MAE / MdAPE / WAPE / bias;
3. interval diagnostics — PICP / MPIW;
4. a per-type table.

The same text is written to markdown.  ``--json`` switches stdout to a single
machine-readable JSON document (markdown is still written).  Stdlib only, no
network.

Usage::

    python3.11 eval/run_eval.py --run /tmp/openjiuwen-out
    python3.11 eval/run_eval.py --trace trace.jsonl --predictions predictions.jsonl
    python3.11 eval/run_eval.py --trace trace.jsonl --holdout 0.3 \
        --out eval/results/final.md --json

Exit codes: ``0`` report produced, ``2`` missing/empty/unreadable input,
``1`` markdown could not be written.  No metric is reported without data: a
metric whose inputs are absent is either ``null`` in JSON or ``n/a`` in the
markdown, with a warning.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from eval.metrics import (
    DEFAULT_HOLDOUT_FRACTION,
    DEFAULT_SPLIT_SEED,
    forecast_metrics,
    interval_metrics,
    split_by_task_id,
    summarize_outcomes,
)

DEFAULT_OUT = REPO_ROOT / "eval" / "results" / "final.md"
PREDICTION_JOIN_FIELDS = (
    "expected_cost",
    "upper_cost",
    "confidence",
    "drivers",
    "theory",
    "model",
    "solver",
)


class InputError(Exception):
    """Raised when an input file is missing, unreadable or empty."""


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #
def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read JSON objects from ``path``; return rows and human-readable problems."""

    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for lineno, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    problems.append(f"{path}:{lineno}: invalid JSON ({exc.msg})")
                    continue
                if not isinstance(payload, dict):
                    problems.append(f"{path}:{lineno}: expected a JSON object, skipped")
                    continue
                rows.append(payload)
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    return rows, problems


def load_input(
    path: Path,
    *,
    kind: str,
    required: bool,
    warnings: list[str],
) -> list[dict[str, Any]] | None:
    """Load one JSONL input following the missing/empty input policy."""

    if not path.exists():
        if required:
            raise InputError(f"{kind} file not found: {path}")
        warnings.append(f"{kind} file not found: {path}; dependent metrics skipped")
        return None
    rows, problems = read_jsonl(path)
    warnings.extend(problems)
    if not rows:
        if required:
            raise InputError(f"{kind} file has no JSON rows: {path}")
        warnings.append(
            f"{kind} file has no JSON rows: {path}; dependent metrics skipped"
        )
        return None
    return rows


def index_predictions(
    rows: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, dict[str, Any]]:
    """Index predictions by ``task_id``; later/newer rows win deterministically."""

    index: dict[str, dict[str, Any]] = {}
    duplicates = 0
    skipped = 0
    for row in rows:
        raw_id = row.get("task_id")
        task_id = str(raw_id).strip() if raw_id is not None else ""
        if not task_id:
            skipped += 1
            continue
        previous = index.get(task_id)
        if previous is None:
            index[task_id] = row
            continue
        duplicates += 1
        if str(row.get("ts") or "") >= str(previous.get("ts") or ""):
            index[task_id] = row
    if skipped:
        warnings.append(f"predictions: {skipped} row(s) without task_id skipped")
    if duplicates:
        warnings.append(
            f"predictions: {duplicates} duplicate task_id row(s); kept the newest per task"
        )
    return index


def join_rows(
    trace_rows: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Attach prediction fields to trace rows by ``task_id`` (trace wins)."""

    joined: list[dict[str, Any]] = []
    used: set[str] = set()
    unmatched = 0
    for trace_row in trace_rows:
        record = dict(trace_row)
        raw_id = trace_row.get("task_id")
        task_id = str(raw_id).strip() if raw_id is not None else ""
        prediction = predictions.get(task_id) if task_id else None
        if prediction is None:
            unmatched += 1
        else:
            used.add(task_id)
            for field in PREDICTION_JOIN_FIELDS:
                if record.get(field) is None and prediction.get(field) is not None:
                    record[field] = prediction[field]
        joined.append(record)
    leftover = len(set(predictions) - used)
    if unmatched:
        warnings.append(f"join: {unmatched} trace row(s) without matching prediction")
    if leftover:
        warnings.append(f"join: {leftover} prediction(s) without matching trace row")
    return joined


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def predictions_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Inventory of the predictions file (no accuracy claims)."""

    task_ids: set[str] = set()
    expected: list[float] = []
    upper: list[float] = []
    theories: dict[str, int] = {}
    models: dict[str, int] = {}
    for row in rows:
        raw_id = row.get("task_id")
        if raw_id is not None and str(raw_id).strip():
            task_ids.add(str(raw_id).strip())
        for field, bucket in (("expected_cost", expected), ("upper_cost", upper)):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            bucket.append(float(value))
        theory = row.get("theory")
        if theory is not None:
            theories[str(theory)] = theories.get(str(theory), 0) + 1
        model = row.get("model")
        if model is not None:
            models[str(model)] = models.get(str(model), 0) + 1
    return {
        "rows": len(rows),
        "tasks": len(task_ids),
        "mean_expected_cost": (sum(expected) / len(expected)) if expected else None,
        "mean_upper_cost": (sum(upper) / len(upper)) if upper else None,
        "theories": theories,
        "models": models,
    }


def per_type_table(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Outcome metrics grouped by ``type`` (falling back to ``task_type``)."""

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        raw = row.get("type")
        label = (
            str(raw)
            if raw not in (None, "")
            else str(row.get("task_type") or "unknown")
        )
        groups.setdefault(label, []).append(row)
    table: list[dict[str, Any]] = []
    for label, group_rows in groups.items():
        table.append({"type": label, **summarize_outcomes(group_rows)})
    table.sort(key=lambda item: (-item["score"], item["type"]))
    return table


def _split_task_ids(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        str(row.get("task_id")).strip()
        for row in rows
        if row.get("task_id") is not None and str(row.get("task_id")).strip()
    }


def build_report(
    *,
    trace_path: Path | None,
    predictions_path: Path | None,
    trace_rows: list[dict[str, Any]] | None,
    prediction_rows: list[dict[str, Any]] | None,
    holdout: float,
    seed: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Build the machine-readable report; never invents a number."""

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "trace": str(trace_path) if trace_path is not None else None,
            "predictions": str(predictions_path)
            if predictions_path is not None
            else None,
        },
        "config": {
            "holdout_fraction": holdout,
            "seed": seed,
            "trace_rows": len(trace_rows or []),
            "prediction_rows": len(prediction_rows or []),
        },
        "split": None,
        "primary": None,
        "forecast": None,
        "interval": None,
        "per_type": [],
        "reference": {},
        "predictions": (
            predictions_summary(prediction_rows) if prediction_rows else None
        ),
        "warnings": warnings,
    }

    predictions = (
        index_predictions(prediction_rows, warnings) if prediction_rows else {}
    )
    if prediction_rows and not trace_rows:
        warnings.append(
            "trace not provided: outcome, forecast and interval metrics are unavailable"
        )

    if not trace_rows:
        return report

    if predictions:
        joined = join_rows(trace_rows, predictions, warnings)
    else:
        joined = [dict(row) for row in trace_rows]

    train_rows, test_rows = split_by_task_id(joined, holdout, seed)
    primary_rows: Sequence[Mapping[str, Any]]
    if test_rows:
        primary_rows, primary_split = test_rows, "test"
    else:
        primary_rows, primary_split = joined, "all"
        warnings.append(
            "holdout split is empty for this task set; primary metrics use all rows"
        )

    train_ids = _split_task_ids(train_rows)
    test_ids = _split_task_ids(test_rows)
    overlap = train_ids & test_ids
    if overlap:  # pragma: no cover - hash split cannot produce it; reported for honesty
        warnings.append(
            f"split leakage detected for {len(overlap)} task id(s): {sorted(overlap)[:5]}"
        )

    report["split"] = {
        "primary": primary_split,
        "train_rows": len(train_rows),
        "test_rows": len(test_rows),
        "train_tasks": len(train_ids),
        "test_tasks": len(test_ids),
        "overlap_tasks": len(overlap),
    }
    report["primary"] = {"split": primary_split, **summarize_outcomes(primary_rows)}
    report["reference"] = {
        "all": summarize_outcomes(joined),
        "train": summarize_outcomes(train_rows),
    }
    report["per_type"] = per_type_table(primary_rows)

    if predictions:
        report["forecast"] = forecast_metrics(primary_rows)
        report["interval"] = interval_metrics(primary_rows)
    else:
        warnings.append(
            "predictions not available: forecast and interval metrics skipped"
        )

    return report


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #
def _num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def _share(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100.0:.{digits}f}%"


def _overruns_line(outcomes: dict[str, Any]) -> str:
    if not outcomes.get("overrun_n"):
        return f"{_num(outcomes['overruns'], 0)} (n/a: no comparable rows)"
    rate = outcomes.get("overrun_rate")
    return (
        f"{_num(outcomes['overruns'], 0)} of {_num(outcomes['overrun_n'], 0)} "
        f"comparable ({_share(rate)})"
    )


def _solve_rate_line(outcomes: dict[str, Any]) -> str:
    return f"{_num(outcomes['solved'], 0)} / {_num(outcomes['rows'], 0)} ({_share(outcomes['solve_rate'])})"


def _primary_section(report: dict[str, Any]) -> list[str]:
    primary = report.get("primary")
    lines = ["## Primary outcome"]
    if primary is None:
        lines.append("")
        lines.append(
            "_Not available: no trace rows were supplied, so no score, solved or "
            "budget_used is reported._"
        )
        return lines
    lines.append("")
    lines.append(
        f"Split: **{primary['split']}**, {_num(primary['rows'], 0)} row(s), "
        f"{_num(primary['tasks'], 0)} task(s)."
    )
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| score | {_num(primary['score'])} |")
    lines.append(f"| solved | {_num(primary['solved'], 0)} |")
    lines.append(f"| budget_used | {_num(primary['budget_used'])} |")
    lines.append(f"| overruns | {_overruns_line(primary)} |")
    lines.append(f"| solve_rate | {_solve_rate_line(primary)} |")
    lines.append(f"| failed | {_num(primary['failed'], 0)} |")
    lines.append(f"| skipped | {_num(primary['skipped'], 0)} |")
    lines.append(f"| errors | {_num(primary['errors'], 0)} |")
    lines.append(f"| other statuses | {_num(primary['other'], 0)} |")
    lines.append(
        f"| rows without recorded cost | {_num(primary['rows_without_cost'], 0)} |"
    )
    return lines


def _forecast_section(report: dict[str, Any]) -> list[str]:
    lines = ["## Forecast diagnostics"]
    forecast = report.get("forecast")
    if forecast is None:
        lines.append("")
        lines.append("_Skipped: predictions/trace rows are not both available._")
        return lines
    lines.append("")
    lines.append("Diagnostic only: the estimator is a tool, not the primary criterion.")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| n | {_num(forecast['n'], 0)} |")
    lines.append(f"| MAE | {_num(forecast['mae'])} |")
    lines.append(f"| MdAPE | {_num(forecast['mdape'])}% |")
    lines.append(f"| WAPE | {_num(forecast['wape'])}% |")
    lines.append(f"| bias (mean y_hat/y_actual) | {_num(forecast['bias'], 3)} |")
    lines.append(
        f"| zero-cost rows excluded | {_num(forecast['zero_cost_excluded'], 0)} |"
    )
    lines.append(f"| rows without y_hat | {_num(forecast['missing_forecast'], 0)} |")
    lines.append(f"| rows without y_actual | {_num(forecast['missing_actual'], 0)} |")
    return lines


def _interval_section(report: dict[str, Any]) -> list[str]:
    lines = ["## Interval diagnostics"]
    interval = report.get("interval")
    if interval is None:
        lines.append("")
        lines.append("_Skipped: predictions/trace rows are not both available._")
        return lines
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| n | {_num(interval['n'], 0)} |")
    lines.append(f"| PICP | {_share(interval['picp'])} |")
    lines.append(f"| MPIW | {_num(interval['mpiw'])} |")
    lines.append(f"| covered | {_num(interval['covered'], 0)} |")
    lines.append(
        f"| rows without both bounds | {_num(interval['missing_bounds'], 0)} |"
    )
    lines.append(f"| rows without y_actual | {_num(interval['missing_actual'], 0)} |")
    lines.append(
        f"| inverted bounds excluded | {_num(interval['invalid_bounds'], 0)} |"
    )
    return lines


def _per_type_section(report: dict[str, Any]) -> list[str]:
    lines = ["## Per-type"]
    table = report.get("per_type") or []
    if not table:
        lines.append("")
        lines.append("_No rows for the reported split._")
        return lines
    lines.append("")
    lines.append(
        "| type | rows | solved | score | budget_used | overruns | solve_rate |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for item in table:
        lines.append(
            f"| {item['type']} | {_num(item['rows'], 0)} | {_num(item['solved'], 0)} | "
            f"{_num(item['score'])} | {_num(item['budget_used'])} | "
            f"{_num(item['overruns'], 0)} | {_share(item['solve_rate'])} |"
        )
    return lines


def _split_section(report: dict[str, Any]) -> list[str]:
    lines = ["## Split"]
    split = report.get("split")
    if split is None:
        lines.append("")
        lines.append("_No split: trace rows are not available._")
        return lines
    lines.append("")
    lines.append("Split by `task_id` hash; train and test never share a task.")
    lines.append("")
    lines.append("| part | rows | tasks |")
    lines.append("|---|---|---|")
    lines.append(
        f"| train | {_num(split['train_rows'], 0)} | {_num(split['train_tasks'], 0)} |"
    )
    lines.append(
        f"| test | {_num(split['test_rows'], 0)} | {_num(split['test_tasks'], 0)} |"
    )
    lines.append("")
    lines.append(f"Task overlap: {_num(split['overlap_tasks'], 0)}.")
    return lines


def _reference_section(report: dict[str, Any]) -> list[str]:
    reference = report.get("reference") or {}
    lines = ["## Reference (same run)"]
    if not reference:
        lines.append("")
        lines.append("_No reference rows._")
        return lines
    lines.append("")
    lines.append("| part | score | solved | budget_used | overruns | solve_rate |")
    lines.append("|---|---|---|---|---|---|")
    for label in ("all", "train"):
        outcomes = reference.get(label)
        if not outcomes:
            continue
        lines.append(
            f"| {label} | {_num(outcomes['score'])} | {_num(outcomes['solved'], 0)} | "
            f"{_num(outcomes['budget_used'])} | {_num(outcomes['overruns'], 0)} | "
            f"{_share(outcomes['solve_rate'])} |"
        )
    return lines


def _predictions_section(report: dict[str, Any]) -> list[str]:
    summary = report.get("predictions")
    lines = ["## Predictions inventory"]
    if summary is None:
        lines.append("")
        lines.append("_No predictions file._")
        return lines
    lines.append("")
    lines.append("| field | value |")
    lines.append("|---|---|")
    lines.append(f"| rows | {_num(summary['rows'], 0)} |")
    lines.append(f"| tasks | {_num(summary['tasks'], 0)} |")
    lines.append(f"| mean expected_cost | {_num(summary['mean_expected_cost'])} |")
    lines.append(f"| mean upper_cost | {_num(summary['mean_upper_cost'])} |")
    if summary["theories"]:
        theories = ", ".join(
            f"{k}: {v}" for k, v in sorted(summary["theories"].items())
        )
        lines.append(f"| theories | {theories} |")
    if summary["models"]:
        models = ", ".join(f"{k}: {v}" for k, v in sorted(summary["models"].items()))
        lines.append(f"| models | {models} |")
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report; primary outcome always comes first."""

    inputs = report["inputs"]
    config = report["config"]
    warnings = report["warnings"]
    lines = [
        "# Final evaluation (D2 / R2-08)",
        "",
        f"- Generated: {report['generated_at']}",
        (
            f"- Trace: `{inputs['trace'] or 'not provided'}`"
            f" ({_num(config['trace_rows'], 0)} rows)"
        ),
        (
            f"- Predictions: `{inputs['predictions'] or 'not provided'}`"
            f" ({_num(config['prediction_rows'], 0)} rows)"
        ),
        (
            f"- Holdout: {_share(config['holdout_fraction'], 1)} of task ids,"
            f" seed `{config['seed']}`"
        ),
        "",
    ]
    lines.extend(_primary_section(report))
    lines.append("")
    lines.extend(_forecast_section(report))
    lines.append("")
    lines.extend(_interval_section(report))
    lines.append("")
    lines.extend(_per_type_section(report))
    lines.append("")
    lines.extend(_split_section(report))
    lines.append("")
    lines.extend(_reference_section(report))
    lines.append("")
    if report.get("predictions") is not None:
        lines.extend(_predictions_section(report))
        lines.append("")
    lines.append("## Warnings")
    lines.append("")
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description="Build the final D2/R2-08 evaluation report from JSONL traces.",
    )
    parser.add_argument("--trace", type=Path, help="path to trace.jsonl")
    parser.add_argument("--predictions", type=Path, help="path to predictions.jsonl")
    parser.add_argument(
        "--run",
        type=Path,
        help="directory containing trace.jsonl and/or predictions.jsonl",
    )
    parser.add_argument(
        "--holdout",
        type=float,
        default=DEFAULT_HOLDOUT_FRACTION,
        help=f"held-out fraction of task ids (default: {DEFAULT_HOLDOUT_FRACTION})",
    )
    parser.add_argument(
        "--seed",
        default=DEFAULT_SPLIT_SEED,
        help=f"deterministic split seed (default: {DEFAULT_SPLIT_SEED})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"markdown output path (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a single JSON document to stdout instead of the markdown text",
    )
    return parser


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _resolve_paths(args: argparse.Namespace) -> tuple[Path | None, Path | None, bool]:
    trace_path: Path | None = args.trace
    predictions_path: Path | None = args.predictions
    predictions_explicit = predictions_path is not None
    if args.run is not None:
        if not args.run.is_dir():
            raise InputError(f"run directory not found: {args.run}")
        if trace_path is None:
            trace_path = args.run / "trace.jsonl"
        if predictions_path is None:
            predictions_path = args.run / "predictions.jsonl"
    if trace_path is None and predictions_path is None:
        raise InputError("provide --trace, --predictions or --run")
    return trace_path, predictions_path, predictions_explicit


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0.0 <= args.holdout <= 1.0:
        build_parser().error(f"--holdout must be within [0, 1], got {args.holdout!r}")

    warnings: list[str] = []
    try:
        trace_path, predictions_path, predictions_explicit = _resolve_paths(args)
        trace_rows = (
            load_input(trace_path, kind="trace", required=True, warnings=warnings)
            if trace_path is not None
            else None
        )
        prediction_rows = (
            load_input(
                predictions_path,
                kind="predictions",
                required=predictions_explicit,
                warnings=warnings,
            )
            if predictions_path is not None
            else None
        )
        if trace_rows is None and prediction_rows is None:
            raise InputError(
                "no usable input rows: supply a non-empty trace and/or predictions"
            )
    except InputError as exc:
        print(f"run_eval: error: {exc}", file=sys.stderr)
        return 2

    report = build_report(
        trace_path=trace_path,
        predictions_path=predictions_path,
        trace_rows=trace_rows,
        prediction_rows=prediction_rows,
        holdout=args.holdout,
        seed=args.seed,
        warnings=warnings,
    )
    markdown = render_markdown(report)
    try:
        write_markdown(args.out, markdown)
    except OSError as exc:
        print(f"run_eval: error: cannot write {args.out}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(markdown)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
