from __future__ import annotations

import tempfile
import unittest

from harness.solveability import check_solveability
from harness.task import load_tasks
from tests.support import EXAMPLES, make_task


class SolveabilityTests(unittest.TestCase):
    def test_official_examples_are_solvable(self) -> None:
        for task in load_tasks(EXAMPLES):
            with self.subTest(task=task.task_id):
                result = check_solveability(task)
                self.assertNotEqual(result.verdict, "impossible", result.as_dict())
                self.assertTrue(result.reason_code)

    def test_unknown_type_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "mystery",
                task_type="table_qa",
                instruction="Answer the table question using table.csv.",
                files={"table.csv": "a,b\n1,2\n"},
                check="exact_str",
            )
            result = check_solveability(task)
            self.assertEqual(result.verdict, "uncertain")
            self.assertEqual(result.reason_code, "unknown_type")

    def test_missing_file_is_impossible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "missing",
                task_type="data_analysis",
                instruction="The file missing.csv has columns id,units. How many units?",
                files={"data.csv": "id,units\n1,5\n"},
            )
            result = check_solveability(task)
            self.assertEqual(result.verdict, "impossible")
            self.assertEqual(result.reason_code, "missing_input")

    def test_phrase_impossible_is_not_impossible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "phrase",
                instruction="Compute 2 + 2. It looks impossible but the numbers are here.",
            )
            result = check_solveability(task)
            self.assertNotEqual(result.verdict, "impossible")

    def test_math_without_digits_is_not_impossible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "words",
                instruction="Compute the total number of boxes described in the missing table.",
            )
            result = check_solveability(task)
            self.assertNotEqual(result.verdict, "impossible")

    def test_empty_instruction_is_impossible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "empty", instruction="   ")
            result = check_solveability(task)
            self.assertEqual(result.verdict, "impossible")
            self.assertEqual(result.reason_code, "empty_instruction")

    def test_short_instruction_is_not_impossible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "short", instruction="Sum 2 and 2")
            result = check_solveability(task)
            self.assertNotEqual(result.verdict, "impossible")

    def test_no_llm_import(self) -> None:
        import inspect

        import harness.solveability as module

        source = inspect.getsource(module)
        self.assertNotIn("from .llm", source)
        self.assertNotIn("import llm", source)
        self.assertNotIn("http://", source)


if __name__ == "__main__":
    unittest.main()
