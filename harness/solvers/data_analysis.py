"""Deterministic CSV aggregation for data-analysis tasks.

Supports the official shape (``sales.csv`` with
``order_id,region,product,month,units,price``) plus common variants:
filtering by product, month range and region, and sum/count/average/min/max
or revenue aggregation.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from .base import SolveResult, SolverContext
from .math import format_number
from ..task import Task

__all__ = ["solve"]

_CSV_RE = re.compile(r"([\w./-]+\.csv)", re.IGNORECASE)
_PRODUCT_RE = re.compile(r"product\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_REGION_RE = re.compile(r"region\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_MONTH_RANGE_RE = re.compile(
    r"months?\s+(\d+)\s+(?:through|to|until|and)\s+(\d+)(?:\s+inclusive)?",
    re.IGNORECASE,
)
_MONTH_BETWEEN_RE = re.compile(
    r"between\s+months?\s+(\d+)\s+and\s+(\d+)",
    re.IGNORECASE,
)
_MONTH_FROM_RE = re.compile(
    r"from\s+month\s+(\d+)\s+to\s+(\d+)",
    re.IGNORECASE,
)


def _find_column(fieldnames: list[str], name: str) -> str | None:
    target = name.strip().lower()
    for field in fieldnames:
        if field.strip().lower() == target:
            return field
    return None


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number


def _parse_filters(instruction: str) -> dict[str, Any]:
    filters: dict[str, Any] = {}

    product_match = _PRODUCT_RE.search(instruction)
    if product_match:
        filters["product"] = product_match.group(1).strip()

    region_match = _REGION_RE.search(instruction)
    if region_match:
        filters["region"] = region_match.group(1).strip()

    month_match = (
        _MONTH_RANGE_RE.search(instruction)
        or _MONTH_BETWEEN_RE.search(instruction)
        or _MONTH_FROM_RE.search(instruction)
    )
    if month_match:
        low, high = int(month_match.group(1)), int(month_match.group(2))
        filters["months"] = (min(low, high), max(low, high))

    return filters


def _question_text(instruction: str) -> str:
    """Return the interrogative sentence, ignoring boilerplate/column lists."""

    matches = re.findall(r"(?:^|[.!?]\s+)([^.!?]*\?)", instruction, flags=re.DOTALL)
    if matches:
        return matches[-1].strip()
    return instruction


def _aggregation(instruction: str) -> tuple[str, str]:
    """Return ``(aggregation, metric_column)``."""

    text = instruction.lower()

    if "revenue" in text or "total value" in text or "sales amount" in text:
        return "revenue", "units"

    if "how many orders" in text or "number of orders" in text or "count of orders" in text:
        return "count", "order_id"

    if "price" in text:
        metric = "price"
    elif "unit" in text:
        metric = "units"
    else:
        metric = "units"

    if "average" in text or "mean" in text or "avg" in text:
        return "avg", metric
    if "maximum" in text or " max " in text or "highest" in text or "largest" in text:
        return "max", metric
    if "minimum" in text or " min " in text or "lowest" in text or "smallest" in text:
        return "min", metric
    return "sum", metric


def _pick_csv(instruction: str, workdir: Path) -> Path | None:
    match = _CSV_RE.search(instruction)
    if match:
        candidate = workdir / match.group(1)
        return candidate if candidate.is_file() else None
    files = sorted(workdir.glob("*.csv")) + sorted(workdir.glob("*/*.csv"))
    return files[0] if files else None


def solve(task: Task, ctx: SolverContext) -> SolveResult:  # noqa: ARG001 - uniform signature
    workdir = task.workdir
    csv_path = _pick_csv(task.instruction, workdir)
    if csv_path is None:
        return SolveResult(None, 0.0, "data_analysis", "no readable CSV file found")

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = [str(name) for name in (reader.fieldnames or []) if name is not None]
            rows = list(reader)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        return SolveResult(None, 0.0, "data_analysis", f"CSV read failed: {exc}")

    if not fieldnames:
        return SolveResult(None, 0.0, "data_analysis", "CSV has no header")

    filters = _parse_filters(task.instruction)
    question = _question_text(task.instruction)
    aggregation, metric_name = _aggregation(question)

    product_col = _find_column(fieldnames, "product")
    month_col = _find_column(fieldnames, "month")
    region_col = _find_column(fieldnames, "region")
    metric_col = _find_column(fieldnames, metric_name)
    units_col = _find_column(fieldnames, "units")
    price_col = _find_column(fieldnames, "price")

    if "product" in filters and product_col is None:
        return SolveResult(None, 0.0, "data_analysis", "CSV has no product column")
    if "months" in filters and month_col is None:
        return SolveResult(None, 0.0, "data_analysis", "CSV has no month column")
    if "region" in filters and region_col is None:
        return SolveResult(None, 0.0, "data_analysis", "CSV has no region column")
    if aggregation == "revenue":
        if units_col is None or price_col is None:
            return SolveResult(None, 0.0, "data_analysis", "revenue requires units and price columns")
    elif aggregation != "count" and metric_col is None:
        return SolveResult(None, 0.0, "data_analysis", f"CSV has no {metric_name} column")

    selected: list[dict[str, str]] = []
    for row in rows:
        if "product" in filters and product_col is not None:
            if str(row.get(product_col, "")).strip().lower() != str(filters["product"]).lower():
                continue
        if "region" in filters and region_col is not None:
            if str(row.get(region_col, "")).strip().lower() != str(filters["region"]).lower():
                continue
        if "months" in filters and month_col is not None:
            month_value = _to_number(row.get(month_col))
            if month_value is None:
                continue
            low, high = filters["months"]
            if not (low <= int(month_value) <= high):
                continue
        selected.append(row)

    if aggregation == "count":
        answer: int | float = len(selected)
    elif aggregation == "revenue":
        total = 0.0
        for row in selected:
            units = _to_number(row.get(units_col))  # type: ignore[arg-type]
            price = _to_number(row.get(price_col))  # type: ignore[arg-type]
            if units is not None and price is not None:
                total += units * price
        answer = total
    else:
        values: list[float] = []
        for row in selected:
            number = _to_number(row.get(metric_col))  # type: ignore[arg-type]
            if number is not None:
                values.append(number)
        if not values:
            if aggregation in {"sum", "count"}:
                answer = 0
            else:
                return SolveResult(
                    None,
                    0.0,
                    "data_analysis",
                    f"no numeric values for {metric_name} after filters",
                )
        elif aggregation == "sum":
            answer = sum(values)
        elif aggregation == "avg":
            answer = sum(values) / len(values)
        elif aggregation == "max":
            answer = max(values)
        elif aggregation == "min":
            answer = min(values)
        else:  # pragma: no cover - exhaustive
            return SolveResult(None, 0.0, "data_analysis", f"unsupported aggregation {aggregation}")

    filter_bits = []
    if "product" in filters:
        filter_bits.append(f"product={filters['product']!r}")
    if "region" in filters:
        filter_bits.append(f"region={filters['region']!r}")
    if "months" in filters:
        filter_bits.append(f"months={filters['months'][0]}..{filters['months'][1]}")
    note = (
        f"{aggregation} {metric_name} over {len(selected)}/{len(rows)} rows in {csv_path.name}"
        + (f" ({', '.join(filter_bits)})" if filter_bits else "")
    )
    return SolveResult(format_number(answer), 0.0, "data_analysis", note)
