from __future__ import annotations

import tempfile
import unittest

from harness.solvers.base import SolveResult
from harness.solvers.data_analysis import solve as solve_data_analysis
from harness.solvers.math import format_number, safe_eval, solve as solve_math
from harness.solvers.search import solve as solve_search
from harness.task import load_tasks

from tests.support import EXAMPLES, expected_for, make_context, make_task


class DeterministicSolverTests(unittest.TestCase):
    def test_six_examples_are_solved_with_zero_tokens(self) -> None:
        functions = {
            "math": solve_math,
            "data_analysis": solve_data_analysis,
            "search": solve_search,
        }
        solved: list[str] = []
        for task in load_tasks(EXAMPLES):
            if task.task_type not in functions:
                continue
            with self.subTest(task=task.task_id):
                context = make_context()
                result = functions[task.task_type](task, context)
                self.assertIsInstance(result, SolveResult)
                self.assertIsNotNone(result.answer, result.notes)
                self.assertEqual(str(result.answer), expected_for(task.task_id))
                self.assertEqual(result.tokens, 0.0)
                self.assertEqual(context.budget.spent, 0.0)
                solved.append(task.task_id)
        self.assertEqual(len(solved), 6)

    def test_math_expression_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "expr", instruction="What is 2 + 3 * 4?")
            result = solve_math(task, make_context())
            self.assertEqual(result.answer, "14")

    def test_math_returns_none_when_unparseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "unknown", instruction="Write a poem about boxes.")
            result = solve_math(task, make_context())
            self.assertIsNone(result.answer)

    def test_safe_eval(self) -> None:
        self.assertEqual(safe_eval("2 + 3 * 4"), 14)
        self.assertEqual(safe_eval("(10 // 3) + 1"), 4)
        self.assertEqual(safe_eval("2 ** 10"), 1024)
        self.assertIsNone(safe_eval("__import__('os').system('echo pwned')"))
        self.assertIsNone(safe_eval("open('/etc/passwd').read()"))
        self.assertIsNone(safe_eval("1 / 0"))
        self.assertIsNone(safe_eval("2 ** 100000"))
        self.assertIsNone(safe_eval(""))

    def test_format_number(self) -> None:
        self.assertEqual(format_number(389), "389")
        self.assertEqual(format_number(389.0), "389")
        self.assertEqual(format_number(4.5), "4.5")

    def test_data_analysis_average_and_max(self) -> None:
        csv_text = (
            "order_id,region,product,month,units,price\n"
            "1,west,widget,5,2,10\n"
            "2,west,widget,6,4,20\n"
            "3,east,widget,6,10,30\n"
            "4,west,sprocket,6,1,99\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "avg",
                task_type="data_analysis",
                instruction=(
                    "The file sales.csv has columns order_id,region,product,month,units,price. "
                    "What is the average price of product 'widget' in months 5 through 6 inclusive?"
                ),
                files={"sales.csv": csv_text},
            )
            result = solve_data_analysis(task, make_context())
            self.assertEqual(result.answer, "20")

            task_max = make_task(
                tmp,
                "max",
                task_type="data_analysis",
                instruction=(
                    "The file sales.csv has columns order_id,region,product,month,units,price. "
                    "What is the maximum units of product 'widget' in region 'west'?"
                ),
                files={"sales.csv": csv_text},
            )
            result_max = solve_data_analysis(task_max, make_context())
            self.assertEqual(result_max.answer, "4")

            task_count = make_task(
                tmp,
                "count",
                task_type="data_analysis",
                instruction=(
                    "The file sales.csv has columns order_id,region,product,month,units,price. "
                    "How many orders are in region 'west'?"
                ),
                files={"sales.csv": csv_text},
            )
            self.assertEqual(solve_data_analysis(task_count, make_context()).answer, "3")

    def test_data_analysis_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "missing",
                task_type="data_analysis",
                instruction="The file sales.csv has columns units. How many units are there?",
            )
            self.assertIsNone(solve_data_analysis(task, make_context()).answer)

    def test_search_chain_example(self) -> None:
        task = next(task for task in load_tasks(EXAMPLES) if task.task_id == "search_0005")
        result = solve_search(task, make_context())
        self.assertEqual(result.answer, "Osaka")
        self.assertIn("employer='Fjord Data'", result.notes)


if __name__ == "__main__":
    unittest.main()
