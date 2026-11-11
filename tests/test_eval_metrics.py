"""Tests for the final D2/R2-08 evaluation script (stdlib only)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from eval.metrics import (
    forecast_metrics,
    interval_metrics,
    split_by_task_id,
    summarize_outcomes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_EVAL = REPO_ROOT / "eval" / "run_eval.py"


def _row(task_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": task_id,
        "type": "math",
        "status": "solved",
        "reward": 1.0,
        "tokens": 0.0,
    }
    payload.update(overrides)
    return payload


class OutcomeMetricsTests(unittest.TestCase):
    def test_summarize_outcomes_counts_statuses_score_and_budget(self) -> None:
        rows = [
            _row("a", status="solved", reward=5.0, y_actual=100.0, y_hat=90.0),
            _row("b", status="solved", reward=3.0, tokens=50.0, y_hat=60.0),
            _row("c", status="failed", reward=7.0, y_actual=30.0, y_hat=20.0),
            _row("d", status="skipped", reward=2.0, y_actual=0.0, y_hat=10.0),
            _row("e", status="error", reward=1.0, tokens=None),
            _row("f", status="timeout", reward=4.0, tokens=None),
        ]

        summary = summarize_outcomes(rows)

        self.assertEqual(summary["rows"], 6)
        self.assertEqual(summary["tasks"], 6)
        self.assertEqual(summary["solved"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["other"], 1)
        # Only confirmed solved rows contribute reward.
        self.assertAlmostEqual(summary["score"], 8.0)
        # y_actual wins over tokens; rows without either cost are counted.
        self.assertAlmostEqual(summary["budget_used"], 180.0)
        self.assertEqual(summary["rows_without_cost"], 2)
        self.assertEqual(summary["overrun_n"], 3)
        self.assertEqual(summary["overruns"], 2)
        self.assertAlmostEqual(summary["overrun_rate"], 2 / 3)
        self.assertAlmostEqual(summary["solve_rate"], 2 / 6)

    def test_summarize_outcomes_empty_rows_has_no_rates(self) -> None:
        summary = summarize_outcomes([])

        self.assertEqual(summary["rows"], 0)
        self.assertEqual(summary["score"], 0.0)
        self.assertIsNone(summary["overrun_rate"])
        self.assertIsNone(summary["solve_rate"])


class ForecastMetricsTests(unittest.TestCase):
    def test_forecast_metrics_exclude_zero_cost_and_missing_values(self) -> None:
        rows = [
            _row("a", y_actual=100.0, y_hat=110.0),
            _row("b", y_actual=50.0, y_hat=40.0),
            _row("c", y_actual=0.0, y_hat=5.0),
            _row("d", y_actual=0.0),
            _row("e", y_actual=200.0),
            _row("f", y_hat=10.0),
        ]

        metrics = forecast_metrics(rows)

        self.assertEqual(metrics["n"], 2)
        self.assertEqual(metrics["zero_cost_excluded"], 2)
        self.assertEqual(metrics["missing_forecast"], 1)
        self.assertEqual(metrics["missing_actual"], 1)
        self.assertAlmostEqual(metrics["mae"], 10.0)
        self.assertAlmostEqual(metrics["mdape"], 15.0)  # median of 10% and 20%
        self.assertAlmostEqual(metrics["wape"], 20.0 / 150.0 * 100.0)
        self.assertAlmostEqual(metrics["bias"], 0.95)  # mean(1.1, 0.8)

    def test_forecast_metrics_without_positive_rows_reports_none(self) -> None:
        metrics = forecast_metrics([_row("a", y_actual=0.0, y_hat=3.0)])

        self.assertEqual(metrics["n"], 0)
        self.assertIsNone(metrics["mae"])
        self.assertIsNone(metrics["mdape"])
        self.assertIsNone(metrics["wape"])
        self.assertIsNone(metrics["bias"])
        self.assertEqual(metrics["zero_cost_excluded"], 1)


class IntervalMetricsTests(unittest.TestCase):
    def test_interval_metrics_picp_and_mpiw(self) -> None:
        rows = [
            _row("a", y_actual=100.0, expected_cost=80.0, upper_cost=120.0),
            _row("b", y_actual=100.0, expected_cost=80.0, upper_cost=95.0),
            _row("c", y_actual=50.0, expected_cost=40.0, upper_cost=60.0),
            _row("d", y_actual=50.0, expected_cost=40.0),
            _row("e", expected_cost=1.0, upper_cost=2.0),
            _row("f", y_actual=10.0, expected_cost=20.0, upper_cost=15.0),
        ]

        metrics = interval_metrics(rows)

        self.assertEqual(metrics["n"], 3)
        self.assertEqual(metrics["covered"], 2)
        self.assertAlmostEqual(metrics["picp"], 2 / 3)
        self.assertAlmostEqual(metrics["mpiw"], 25.0)
        self.assertEqual(metrics["missing_bounds"], 1)
        self.assertEqual(metrics["missing_actual"], 1)
        self.assertEqual(metrics["invalid_bounds"], 1)


class SplitTests(unittest.TestCase):
    def test_split_by_task_id_is_deterministic_and_has_no_leakage(self) -> None:
        rows = [
            _row(f"task-{index:03d}", tokens=float(index))
            for index in range(50)
            for _ in range(3)
        ]

        train, test = split_by_task_id(rows, holdout_fraction=0.3, seed="v5")
        train_again, test_again = split_by_task_id(
            list(reversed(rows)), holdout_fraction=0.3, seed="v5"
        )

        # Order-independent determinism of the membership (list order follows input).
        self.assertEqual(
            sorted(row["task_id"] for row in train),
            sorted(row["task_id"] for row in train_again),
        )
        self.assertEqual(
            sorted(row["task_id"] for row in test),
            sorted(row["task_id"] for row in test_again),
        )

        # No row is lost, no task id appears on both sides.
        self.assertEqual(len(train) + len(test), len(rows))
        train_ids = {row["task_id"] for row in train}
        test_ids = {row["task_id"] for row in test}
        self.assertFalse(train_ids & test_ids)
        self.assertEqual(train_ids | test_ids, {row["task_id"] for row in rows})

        # All records of one task stay together.
        self.assertTrue(all(row["task_id"] in test_ids for row in test))
        self.assertTrue(all(row["task_id"] in train_ids for row in train))

        self.assertTrue(test)
        self.assertTrue(train)
        self.assertLess(len(test_ids), len(train_ids))

    def test_split_extremes_and_validation(self) -> None:
        rows = [_row(f"task-{index}") for index in range(10)]

        train, test = split_by_task_id(rows, holdout_fraction=0.0)
        self.assertEqual(len(test), 0)
        self.assertEqual(len(train), len(rows))

        train, test = split_by_task_id(rows, holdout_fraction=1.0)
        self.assertEqual(len(train), 0)
        self.assertEqual(len(test), len(rows))

        with self.assertRaises(ValueError):
            split_by_task_id(rows, holdout_fraction=1.5)
        with self.assertRaises(ValueError):
            split_by_task_id([{"type": "math"}], holdout_fraction=0.3)


class CliTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict[str, object]]) -> Path:
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def _fixtures(self, directory: Path) -> tuple[Path, Path]:
        trace_rows = [
            _row(
                "t01",
                type="math",
                status="solved",
                reward=1.0,
                y_actual=100.0,
                y_hat=90.0,
                theory="median",
                estimator_confidence="high",
                model="qwen",
            ),
            _row(
                "t02",
                type="math",
                status="solved",
                reward=2.0,
                y_actual=200.0,
                y_hat=250.0,
            ),
            _row(
                "t03",
                type="coding",
                status="solved",
                reward=3.0,
                y_actual=300.0,
                y_hat=300.0,
            ),
            _row(
                "t04",
                type="coding",
                status="solved",
                reward=4.0,
                y_actual=400.0,
                y_hat=450.0,
            ),
            _row(
                "t05",
                type="coding",
                status="solved",
                reward=5.0,
                y_actual=500.0,
                y_hat=480.0,
            ),
            _row(
                "t06",
                type="search",
                status="failed",
                reward=6.0,
                y_actual=50.0,
                y_hat=60.0,
            ),
            _row(
                "t07",
                type="search",
                status="skipped",
                reward=7.0,
                y_actual=0.0,
                y_hat=10.0,
            ),
            _row(
                "t08", type="math", status="error", reward=8.0, tokens=25.0, y_hat=20.0
            ),
        ]
        prediction_rows = []
        for index, row in enumerate(trace_rows, 1):
            task_id = str(row["task_id"])
            prediction_rows.append(
                {
                    "task_id": task_id,
                    "task_type": row["type"],
                    "solver": "math" if row["type"] == "math" else "generic",
                    "model": "qwen",
                    "expected_cost": 100.0 * index,
                    "upper_cost": 150.0 * index,
                    "confidence": "high",
                    "drivers": {"n": index},
                    "theory": "median",
                    "ts": f"2026-11-11T10:00:{index:02d}+00:00",
                }
            )
        trace = self._write_jsonl(directory / "trace.jsonl", trace_rows)
        predictions = self._write_jsonl(
            directory / "predictions.jsonl", prediction_rows
        )
        return trace, predictions

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUN_EVAL), *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )

    def test_cli_writes_markdown_in_required_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            trace, predictions = self._fixtures(directory)
            out = directory / "final.md"

            result = self._run(
                "--trace",
                str(trace),
                "--predictions",
                str(predictions),
                "--out",
                str(out),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertIn("score", text)
            self.assertIn("budget_used", text)
            self.assertIn("overruns", text)
            self.assertLess(
                text.index("## Primary outcome"), text.index("## Forecast diagnostics")
            )
            self.assertLess(
                text.index("## Forecast diagnostics"),
                text.index("## Interval diagnostics"),
            )
            self.assertLess(
                text.index("## Interval diagnostics"), text.index("## Per-type")
            )
            self.assertIn("MAE", text)
            self.assertIn("PICP", text)
            self.assertIn("Task overlap: 0", text)

    def test_cli_json_reports_primary_outcome_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._fixtures(directory)
            out = directory / "final.md"

            result = self._run(
                "--run",
                str(directory),
                "--out",
                str(out),
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out.exists())
            report = json.loads(result.stdout)
            self.assertEqual(report["config"]["trace_rows"], 8)
            self.assertEqual(report["config"]["prediction_rows"], 8)
            self.assertIn(report["primary"]["split"], {"test", "all"})
            self.assertGreater(report["primary"]["rows"], 0)
            self.assertEqual(report["reference"]["all"]["score"], 15.0)
            self.assertEqual(report["reference"]["all"]["overruns"], 2)
            self.assertEqual(report["reference"]["all"]["budget_used"], 1575.0)
            self.assertIsNotNone(report["forecast"])
            self.assertIsNotNone(report["interval"])
            self.assertEqual(report["split"]["overlap_tasks"], 0)

    def test_cli_skips_forecast_when_predictions_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            trace, _ = self._fixtures(directory)
            out = directory / "final.md"

            result = self._run(
                "--trace",
                str(trace),
                "--predictions",
                str(directory / "absent.jsonl"),
                "--out",
                str(out),
            )

            # An explicitly requested predictions file must exist.
            self.assertEqual(result.returncode, 2)
            self.assertIn("not found", result.stderr)

            # A --run directory that only holds a trace skips forecast metrics.
            trace_only = directory / "trace-only"
            trace_only.mkdir()
            (trace_only / "trace.jsonl").write_text(
                trace.read_text(encoding="utf-8"), encoding="utf-8"
            )
            result = self._run(
                "--run",
                str(trace_only),
                "--out",
                str(out),
                "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIsNone(report["forecast"])
            self.assertIsNone(report["interval"])
            self.assertTrue(any("predictions" in item for item in report["warnings"]))

    def test_cli_empty_input_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            empty = directory / "trace.jsonl"
            empty.write_text("", encoding="utf-8")
            out = directory / "final.md"

            result = self._run("--trace", str(empty), "--out", str(out))

            self.assertEqual(result.returncode, 2)
            self.assertIn("no JSON rows", result.stderr)
            self.assertFalse(out.exists())

    def test_cli_missing_input_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = self._run("--trace", str(directory / "absent.jsonl"))
            self.assertEqual(result.returncode, 2)
            self.assertIn("not found", result.stderr)

            result = self._run()
            self.assertEqual(result.returncode, 2)
            self.assertIn("provide --trace", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
