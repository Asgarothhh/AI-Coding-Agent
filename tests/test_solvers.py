from __future__ import annotations

import tempfile
import unittest

from harness.solvers.base import SolveResult
from harness.solvers.data_analysis import solve as solve_data_analysis
from harness.solvers.math import format_number, safe_eval, solve as solve_math
from harness.solvers.search import solve as solve_search
from harness.task import load_task, load_tasks

from tests.support import EXAMPLES, ROOT, expected_for, make_context, make_task

EXTENDED = ROOT / "examples" / "task-dir-extended"


def _search_kb() -> dict[str, str]:
    """Small knowledge base with repeated mentor chains and a founded filter."""

    return {
        "kb/person_maya_sato.md": (
            "# Maya Sato\n\nEmployer: Iris Optics\nMentor: Dmitri Wang\n"
        ),
        "kb/person_dmitri_wang.md": (
            "# Dmitri Wang\n\nEmployer: Cobalt Works\nMentor: Alice Smith\n"
        ),
        "kb/person_alice_smith.md": (
            "# Alice Smith\n\nEmployer: Helix Motors\nMentor: None\n"
        ),
        "kb/person_boris_ivanova.md": (
            "# Boris Ivanova\n\nEmployer: Bluefin Systems\nMentor: Chen Petrov\n"
        ),
        "kb/person_chen_petrov.md": (
            "# Chen Petrov\n\nEmployer: Ember Bio\nMentor: Gina Muller\n"
        ),
        "kb/person_gina_muller.md": (
            "# Gina Muller\n\nEmployer: Granite AI\nMentor: None\n"
        ),
        "kb/company_iris_optics.md": (
            "# Iris Optics\n\nHeadquarters: Nairobi\nFounded: 1999\n"
        ),
        "kb/company_cobalt_works.md": (
            "# Cobalt Works\n\nHeadquarters: Austin\nFounded: 1971\n"
        ),
        "kb/company_helix_motors.md": (
            "# Helix Motors\n\nHeadquarters: Lisbon\nFounded: 1962\n"
        ),
        "kb/company_granite_ai.md": (
            "# Granite AI\n\nHeadquarters: Toronto\nFounded: 1978\nCEO: Dana Reed\n"
        ),
        "kb/company_bluefin_systems.md": (
            "# Bluefin Systems\n\nHeadquarters: Tallinn\nFounded: 2003\n"
        ),
        "kb/company_ember_bio.md": (
            "# Ember Bio\n\nHeadquarters: Bogota\nFounded: 2010\n"
        ),
    }


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

    def test_search_0006_repeated_mentors_then_employer_headquarters(self) -> None:
        task = load_task(EXTENDED / "search_0006")
        self.assertIn(
            "In which city is the headquarters of the employer of the mentor "
            "of the mentor of Maya Sato?",
            task.instruction,
        )
        result = solve_search(task, make_context())
        self.assertEqual(result.answer, "Lisbon")
        self.assertEqual(result.notes.count("mentor="), 2)
        self.assertIn("employer='Helix Motors'", result.notes)

    def test_search_0007_repeated_mentors_with_founded_filter(self) -> None:
        task = load_task(EXTENDED / "search_0007")
        self.assertIn(
            "Which company founded in 1978 employs the mentor of the mentor "
            "of Boris Ivanova?",
            task.instruction,
        )
        result = solve_search(task, make_context())
        self.assertEqual(result.answer, "Granite AI")
        self.assertEqual(result.notes.count("mentor="), 2)
        self.assertIn("employer='Granite AI'", result.notes)

    def test_search_repeated_relations_apply_right_to_left(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "repeated",
                task_type="search",
                instruction=(
                    "In which city is the headquarters of the employer of the "
                    "mentor of the mentor of Maya Sato?"
                ),
                files=_search_kb(),
                check="exact_str",
            )
            result = solve_search(task, make_context())
            self.assertEqual(result.answer, "Lisbon")
            self.assertIn("mentor='Dmitri Wang'", result.notes)
            self.assertIn("mentor='Alice Smith'", result.notes)
            self.assertIn("employer='Helix Motors'", result.notes)

            mentor_result = solve_search(
                make_task(
                    tmp,
                    "mentor-chain",
                    task_type="search",
                    instruction="Who is the mentor of the mentor of Maya Sato?",
                    files=_search_kb(),
                    check="exact_str",
                ),
                make_context(),
            )
            self.assertEqual(mentor_result.answer, "Alice Smith")

            outer_result = solve_search(
                make_task(
                    tmp,
                    "outer-employer",
                    task_type="search",
                    instruction="Who is the employer of the mentor of Boris Ivanova?",
                    files=_search_kb(),
                    check="exact_str",
                ),
                make_context(),
            )
            self.assertEqual(outer_result.answer, "Ember Bio")

    def test_search_company_filter_after_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            matched = solve_search(
                make_task(
                    tmp,
                    "filter-match",
                    task_type="search",
                    instruction=(
                        "Which company founded in 1978 employs the mentor of "
                        "the mentor of Boris Ivanova?"
                    ),
                    files=_search_kb(),
                    check="exact_str",
                ),
                make_context(),
            )
            self.assertEqual(matched.answer, "Granite AI")

            mismatched = solve_search(
                make_task(
                    tmp,
                    "filter-mismatch",
                    task_type="search",
                    instruction=(
                        "Which company founded in 1978 employs the mentor of "
                        "the mentor of Maya Sato?"
                    ),
                    files=_search_kb(),
                    check="exact_str",
                ),
                make_context(),
            )
            self.assertIsNone(mismatched.answer)
            self.assertIn("does not match requested '1978'", mismatched.notes)

    def test_search_ceo_attribute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = solve_search(
                make_task(
                    tmp,
                    "ceo",
                    task_type="search",
                    instruction="Who is the CEO of Granite AI?",
                    files=_search_kb(),
                    check="exact_str",
                ),
                make_context(),
            )
            self.assertEqual(result.answer, "Dana Reed")

    def test_search_employer_verb_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for index, question in enumerate(
                (
                    "Which company does Maya Sato work for?",
                    "Which company is Maya Sato employed by?",
                    "Which company employs Maya Sato?",
                )
            ):
                with self.subTest(instruction=question):
                    result = solve_search(
                        make_task(
                            tmp,
                            f"verb-{index}",
                            task_type="search",
                            instruction=question,
                            files=_search_kb(),
                            check="exact_str",
                        ),
                        make_context(),
                    )
                    self.assertEqual(result.answer, "Iris Optics")


if __name__ == "__main__":
    unittest.main()
