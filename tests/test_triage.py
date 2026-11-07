from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.task import load_tasks
from harness.triage import DEFAULT_THRESHOLD, triage

from tests.support import EXAMPLES, make_task


class TriageTests(unittest.TestCase):
    def test_official_examples_are_feasible(self) -> None:
        for task in load_tasks(EXAMPLES):
            with self.subTest(task=task.task_id):
                result = triage(task)
                self.assertFalse(result.infeasible, result.reasons)
                self.assertEqual(result.score, 0.0)

    def test_empty_knowledge_base_is_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "search_empty",
                task_type="search",
                instruction="The directory kb/ contains a knowledge base. What is Alice Smith's employer?",
            )
            assert task.workspace is not None
            (task.workspace / "kb").mkdir()
            result = triage(task)
            self.assertTrue(result.infeasible)
            self.assertGreaterEqual(result.score, 0.95)
            self.assertTrue(any("no markdown" in reason for reason in result.reasons))

    def test_missing_referenced_csv_is_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "data_missing",
                task_type="data_analysis",
                instruction=(
                    "The file missing.csv has columns id,units. "
                    "How many units were sold in month 1?"
                ),
            )
            result = triage(task)
            self.assertTrue(result.infeasible)
            self.assertTrue(any("missing" in reason for reason in result.reasons))

    def test_coding_without_tests_is_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "coding_no_tests",
                task_type="coding",
                instruction="Fix the bugs in mylib.py. Run python3 test_mylib.py.",
                files={"mylib.py": "def f(x):\n    return x\n"},
            )
            result = triage(task)
            self.assertTrue(result.infeasible)
            self.assertTrue(any("no test_" in reason for reason in result.reasons))

    def test_internet_task_is_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "net",
                task_type="search",
                instruction="Download the latest report from https://example.com/report.csv and sum it.",
                files={"kb/note.md": "# Note\n"},
            )
            result = triage(task)
            self.assertTrue(result.infeasible)
            self.assertTrue(any("internet" in reason for reason in result.reasons))

    def test_contradictory_instruction_is_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "contradiction",
                task_type="math",
                instruction="The answer is 5 and the answer is 7, which is contradictory and impossible.",
            )
            self.assertTrue(triage(task).infeasible)

    def test_math_without_numbers_is_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "math_no_numbers",
                task_type="math",
                instruction="Compute the total number of boxes described in the missing table.",
            )
            result = triage(task)
            self.assertTrue(result.infeasible)
            self.assertGreaterEqual(result.score, 0.7)

    def test_threshold_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "search_empty",
                task_type="search",
                instruction="The directory kb/ contains a knowledge base. Who is Alice Smith?",
            )
            assert task.workspace is not None
            (task.workspace / "kb").mkdir()
            strict = triage(task, threshold=0.99)
            loose = triage(task, threshold=0.6)
            self.assertFalse(strict.infeasible)
            self.assertTrue(loose.infeasible)
            self.assertEqual(DEFAULT_THRESHOLD, 0.6)

    def test_row_count_mismatch_is_low_severity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "row_mismatch",
                task_type="data_analysis",
                instruction=(
                    "The file sales.csv has columns id,units (99 rows). "
                    "How many units are there?"
                ),
                files={"sales.csv": "id,units\n1,2\n2,3\n"},
            )
            result = triage(task)
            self.assertFalse(result.infeasible)
            self.assertTrue(any("claimed 99 rows" in reason for reason in result.reasons))

    def test_invalid_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "m", task_type="math", instruction="Compute 1 + 1.")
            with self.assertRaises(ValueError):
                triage(task, threshold=1.5)


if __name__ == "__main__":
    unittest.main()
