"""Tests for eval/fake_100.py report-layer reconciliation. No network, no full execute."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from eval.fake_100 import (
    KNOWN_REPORT_STATUSES,
    generate_tasks,
    reconcile_records,
    summarize_records,
)
from harness.task import load_tasks

EXPECTED_INVALID_IDS = [
    "syn_004",
    "syn_016",
    "syn_028",
    "syn_040",
    "syn_052",
    "syn_064",
    "syn_076",
    "syn_088",
]


def _item(
    task_id: str,
    *,
    bucket: int = 0,
    kind: str = "math",
    expected: str | None = "1",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "bucket": bucket,
        "kind": kind,
        "expected": expected,
    }


def _runner(
    task_id: str,
    status: str,
    *,
    answer: Any = None,
    reward: float = 1.0,
    solver: str = "math",
    notes: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": status,
        "answer": answer,
        "reward": reward,
        "solver": solver,
        "notes": notes,
        "error": error,
    }


def _manual_by_status(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in records:
        status = rec.get("report_status")
        if status is None:
            continue
        key = str(status)
        counts[key] = counts.get(key, 0) + 1
    return counts


class GenerateAndLoadTests(unittest.TestCase):
    def test_seed_2026_count_100_has_eight_invalid_json_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = generate_tasks(root, 100, 2026)
            ids = [row["task_id"] for row in manifest]
            self.assertEqual(len(ids), 100)
            self.assertEqual(len(set(ids)), 100)
            self.assertEqual(ids, [f"syn_{index:03d}" for index in range(100)])
            for row in manifest:
                if row["task_id"] in EXPECTED_INVALID_IDS:
                    self.assertEqual(row["bucket"], 4)
                    self.assertEqual(row["kind"], "invalid_json")

            loaded = load_tasks(root)
            loaded_ids = {task.task_id for task in loaded}
            self.assertEqual(len(loaded), 92)
            self.assertEqual(len(loaded_ids), 92)
            invalid_ids = [
                row["task_id"] for row in manifest if row["task_id"] not in loaded_ids
            ]
            self.assertEqual(invalid_ids, EXPECTED_INVALID_IDS)
            self.assertEqual(set(ids) - loaded_ids, set(EXPECTED_INVALID_IDS))


class ReconcileTests(unittest.TestCase):
    def test_missing_id_marks_incomplete_and_problem(self) -> None:
        manifest = [_item("a", expected="1"), _item("b", expected="2")]
        loaded_ids = {"a", "b"}
        runner_results = [_runner("a", "solved", answer="1")]
        records, problems = reconcile_records(manifest, loaded_ids, runner_results)
        self.assertEqual(len(records), 2)
        self.assertIn("missing_terminal:b", problems)
        summary = summarize_records(records)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["generated"], 2)
        self.assertNotIn("not_started", KNOWN_REPORT_STATUSES)
        self.assertIsNone(records[1]["report_status"])
        self.assertNotIn(records[1]["report_status"], KNOWN_REPORT_STATUSES)

    def test_duplicate_runner_id_keeps_first_and_records_problem(self) -> None:
        manifest = [_item("a", expected="1")]
        loaded_ids = {"a"}
        runner_results = [
            _runner("a", "solved", answer="1", notes="first"),
            _runner("a", "failed", answer=None, notes="second"),
        ]
        records, problems = reconcile_records(manifest, loaded_ids, runner_results)
        self.assertEqual(problems, ["duplicate:a"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["report_status"], "solved")
        self.assertEqual(records[0]["raw_status"], "solved")
        self.assertEqual(records[0]["reason"], "first")
        summary = summarize_records(records)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["solved"], 1)

    def test_unknown_status_is_a_problem(self) -> None:
        manifest = [_item("a", expected="1")]
        loaded_ids = {"a"}
        runner_results = [_runner("a", "timeout", answer=None, notes="late")]
        records, problems = reconcile_records(manifest, loaded_ids, runner_results)
        self.assertEqual(problems, ["unknown_status:a:timeout"])
        self.assertEqual(records[0]["raw_status"], "timeout")
        self.assertEqual(records[0]["report_status"], "timeout")
        self.assertNotIn("timeout", KNOWN_REPORT_STATUSES)
        summary = summarize_records(records)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["solved"], 0)
        self.assertEqual(summary["by_status"].get("timeout"), 1)

    def test_missing_terminal_event_loaded_but_no_result(self) -> None:
        manifest = [
            _item("kept", expected="1"),
            _item("dropped", expected="2"),
        ]
        loaded_ids = {"kept", "dropped"}
        runner_results = [_runner("kept", "failed", notes="nope")]
        records, problems = reconcile_records(manifest, loaded_ids, runner_results)
        self.assertEqual(problems, ["missing_terminal:dropped"])
        dropped = records[1]
        self.assertEqual(dropped["task_id"], "dropped")
        self.assertIsNone(dropped["raw_status"])
        self.assertIsNone(dropped["report_status"])
        self.assertEqual(dropped["reason"], "missing_terminal")
        self.assertNotEqual(dropped["report_status"], "not_started")
        summary = summarize_records(records)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["loaded"], 2)
        self.assertEqual(summary["failed"], 1)

    def test_summarize_records_matches_manual_counts(self) -> None:
        records = [
            {
                "task_id": "s1",
                "report_status": "solved",
                "expected": "ok",
                "answer": "ok",
                "reward": 2.0,
            },
            {
                "task_id": "s2",
                "report_status": "solved",
                "expected": "nope",
                "answer": "other",
                "reward": 9.0,
            },
            {
                "task_id": "f1",
                "report_status": "failed",
                "expected": None,
                "answer": None,
                "reward": 1.0,
            },
            {
                "task_id": "k1",
                "report_status": "skipped",
                "expected": None,
                "answer": None,
                "reward": 1.0,
            },
            {
                "task_id": "e1",
                "report_status": "error",
                "expected": None,
                "answer": None,
                "reward": 1.0,
            },
            {
                "task_id": "i1",
                "report_status": "invalid",
                "expected": None,
                "answer": None,
                "reward": None,
            },
            {
                "task_id": "gap",
                "report_status": None,
                "expected": "x",
                "answer": None,
                "reward": 1.0,
            },
        ]
        summary = summarize_records(records)
        manual = _manual_by_status(records)
        self.assertEqual(summary["by_status"]["solved"], manual["solved"])
        self.assertEqual(summary["by_status"]["failed"], manual["failed"])
        self.assertEqual(summary["by_status"]["skipped"], manual["skipped"])
        self.assertEqual(summary["by_status"]["error"], manual["error"])
        self.assertEqual(summary["by_status"]["invalid"], manual["invalid"])
        self.assertEqual(summary["solved"], manual["solved"])
        self.assertEqual(summary["failed"], manual["failed"])
        self.assertEqual(summary["skipped"], manual["skipped"])
        self.assertEqual(summary["error"], manual["error"])
        self.assertEqual(summary["invalid"], manual["invalid"])
        self.assertEqual(summary["generated"], len(records))
        self.assertEqual(summary["loaded"], 6)
        self.assertEqual(summary["invalid_ids"], ["i1"])
        self.assertAlmostEqual(summary["checked_score"], 2.0)
        self.assertFalse(summary["complete"])
        self.assertEqual(
            summary["solved"]
            + summary["failed"]
            + summary["skipped"]
            + summary["error"]
            + summary["invalid"],
            6,
        )

    def test_complete_happy_path_three_solved_one_failed_one_skipped_one_invalid(
        self,
    ) -> None:
        manifest = [
            _item("s1", expected="1"),
            _item("s2", expected="2"),
            _item("s3", expected="3"),
            _item("f1", kind="fail", expected=None),
            _item("k1", kind="skip", expected=None),
            _item("i1", bucket=4, kind="invalid_json", expected=None),
        ]
        loaded_ids = {"s1", "s2", "s3", "f1", "k1"}
        runner_results = [
            _runner("s1", "solved", answer="1", reward=1.0),
            _runner("s2", "solved", answer="2", reward=1.0),
            _runner("s3", "solved", answer="3", reward=1.0),
            _runner("f1", "failed", notes="nope", reward=2.0),
            _runner("k1", "skipped", notes="reject:time", reward=1.0),
        ]
        records, problems = reconcile_records(manifest, loaded_ids, runner_results)
        self.assertEqual(problems, [])
        self.assertEqual(len(records), 6)
        self.assertEqual(records[-1]["report_status"], "invalid")
        self.assertEqual(records[-1]["reason"], "load_skipped:invalid_json")
        self.assertIsNone(records[-1]["raw_status"])
        summary = summarize_records(records)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["generated"], 6)
        self.assertEqual(summary["loaded"], 5)
        self.assertEqual(summary["invalid_ids"], ["i1"])
        self.assertEqual(summary["solved"], 3)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["error"], 0)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(
            summary["solved"]
            + summary["failed"]
            + summary["skipped"]
            + summary["error"]
            + summary["invalid"],
            6,
        )
        self.assertEqual(summary["by_status"]["solved"], 3)
        self.assertAlmostEqual(summary["checked_score"], 3.0)
        self.assertEqual(
            set(summary["by_status"]) - KNOWN_REPORT_STATUSES,
            set(),
        )


if __name__ == "__main__":
    unittest.main()
