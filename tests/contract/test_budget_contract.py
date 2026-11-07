"""Contract: cost accounting, hard stops and reserve/per-task caps.

The single shared budget must always follow::

    cost = prompt_tokens / 10 + completion_tokens

and it must stop *new* work without ever crashing a response that was already
paid for.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.budget import Budget, BudgetExhausted, cost_of
from harness.llm import ChatMessage, LLMConfig, LLMResponse, LLMUsage
from harness.runner import RunOptions, run

from tests.support import make_task

BROKEN_LIB = "def add(a, b):\n    return a - b\n"

FAILING_TEST = (
    "import unittest\n\n"
    "import mylib\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(mylib.add(2, 3), 5)\n\n\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)

CODING_FILES = {"mylib.py": BROKEN_LIB, "test_mylib.py": FAILING_TEST}
CODING_INSTRUCTION = "Fix mylib.py so that test_mylib.py passes."


class CountingLLM:
    """Minimal ``LLMClient`` stand-in with deterministic usage reporting."""

    available = True

    def __init__(
        self,
        text: str = "MOCK",
        *,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
    ) -> None:
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.config = LLMConfig(base_url="http://fake.invalid/v1", model="fake-model")
        self.calls = 0
        self._lock = threading.Lock()

    def chat(
        self,
        messages: Sequence[Mapping[str, Any] | ChatMessage],
        **kwargs: Any,
    ) -> LLMResponse:
        with self._lock:
            self.calls += 1
        return LLMResponse(
            text=self.text,
            model="fake",
            usage=LLMUsage(self.prompt_tokens, self.completion_tokens),
            latency=0.0,
        )


def _coding_task(root: Path, task_id: str):
    return make_task(
        root,
        task_id,
        task_type="coding",
        instruction=CODING_INSTRUCTION,
        files=CODING_FILES,
        check="unittest",
    )


def _run(root: Path, out: Path, **overrides: Any) -> dict[str, Any]:
    options = dict(
        tasks_dir=root,
        out=out,
        offline=True,
        max_workers=1,
        time_limit=60.0,
    )
    options.update(overrides)
    return run(RunOptions(**options))


class CostFormulaTests(unittest.TestCase):
    def test_exact_formula(self) -> None:
        # cost = prompt_tokens / 10 + completion_tokens
        self.assertEqual(cost_of(50, 10), 15.0)
        self.assertEqual(cost_of(100, 5), 15.0)
        self.assertEqual(cost_of(100, 50), 60.0)
        self.assertEqual(cost_of(0, 0), 0.0)
        self.assertEqual(cost_of(1000, 0), 100.0)
        self.assertEqual(cost_of(0, 50), 50.0)
        self.assertAlmostEqual(cost_of(33, 7), 10.3)
        with self.assertRaises(ValueError):
            cost_of(-1, 0)
        with self.assertRaises(ValueError):
            cost_of(0, -1)

    def test_charge_returns_the_same_formula(self) -> None:
        budget = Budget(1_000.0)
        returned = budget.charge(50, 10, "t")
        self.assertEqual(returned, 15.0)
        self.assertEqual(budget.spent, 15.0)
        self.assertEqual(budget.prompt_tokens, 50)
        self.assertEqual(budget.completion_tokens, 10)
        self.assertEqual(budget.task_spent("t"), 15.0)


class BudgetZeroTests(unittest.TestCase):
    def test_zero_budget_makes_no_llm_calls_and_no_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_a", instruction="Compute 1 + 1.")
            _coding_task(root, "coding_a")
            fake = CountingLLM()

            summary = _run(
                root,
                Path(tmp) / "out",
                offline=False,
                llm=fake,
                budget=0.0,
                max_workers=2,
            )

            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], 2)
            self.assertEqual(summary["stop_reason"], "budget")
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertEqual(summary["prompt_tokens"], 0)
            self.assertEqual(summary["completion_tokens"], 0)
            self.assertEqual(fake.calls, 0)
            self.assertEqual(list(root.rglob("answer.txt")), [])


class ReserveTests(unittest.TestCase):
    def test_reserve_blocks_new_calls(self) -> None:
        budget = Budget(100.0, reserve=20.0)
        budget.check("t", estimated_cost=1.0)
        budget.charge(0, 80, "t")
        self.assertEqual(budget.remaining, 20.0)
        self.assertFalse(budget.can_start_task())
        with self.assertRaises(BudgetExhausted) as caught:
            budget.check("t", estimated_cost=1.0)
        self.assertIn("reserve", str(caught.exception))

    def test_reserve_boundary_still_allows_calls_above_it(self) -> None:
        budget = Budget(100.0, reserve=20.0)
        budget.charge(0, 79, "t")
        self.assertEqual(budget.remaining, 21.0)
        self.assertTrue(budget.can_start_task())
        budget.check("t", estimated_cost=1.0)

    def test_runner_with_reserve_equal_to_budget_starts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            for index in range(3):
                make_task(root, f"math_{index}", instruction="Compute 1 + 1.")

            summary = _run(
                root,
                Path(tmp) / "out",
                budget=10.0,
                reserve=10.0,
                max_workers=2,
            )

            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], 3)
            self.assertEqual(summary["stop_reason"], "budget")
            self.assertEqual(list(root.rglob("answer.txt")), [])


class PerTaskCapTests(unittest.TestCase):
    def test_cap_blocks_one_task_but_not_another(self) -> None:
        budget = Budget(100_000.0, per_task_cap=100.0)
        budget.charge(0, 100, "a")
        with self.assertRaises(BudgetExhausted) as caught:
            budget.check("a", estimated_cost=1.0)
        self.assertIn("per-task cap", str(caught.exception))
        # A different task has a clean per-task ledger.
        budget.check("b", estimated_cost=100.0)

    def test_solver_stops_before_llm_call_when_cap_would_be_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            _coding_task(root, "coding_a")
            fake = CountingLLM()

            summary = _run(
                root,
                Path(tmp) / "out",
                offline=False,
                llm=fake,
                budget=1_000.0,
                per_task_cap=1.0,
                max_iterations=2,
                max_tokens=64,
            )

            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(fake.calls, 0)
            self.assertEqual(summary["tokens_spent"], 0.0)
            result = summary["results"][0]
            self.assertTrue(result["notes"].startswith("budget stop:"))
            self.assertEqual(list(root.rglob("answer.txt")), [])


class GlobalExhaustionTests(unittest.TestCase):
    def test_budget_hard_stop_is_sticky(self) -> None:
        budget = Budget(10.0)
        budget.charge(0, 10, "t")
        self.assertEqual(budget.remaining, 0.0)
        self.assertTrue(budget.exhausted)
        self.assertFalse(budget.can_start_task())
        with self.assertRaises(BudgetExhausted):
            budget.check("next")

    def test_global_exhaustion_stops_remaining_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            for task_id in ("coding_a", "coding_b", "coding_c"):
                _coding_task(root, task_id)
            # One response already costs more than the whole budget.
            fake = CountingLLM(text="MOCK", prompt_tokens=0, completion_tokens=100)

            summary = _run(
                root,
                Path(tmp) / "out",
                offline=False,
                llm=fake,
                budget=50.0,
                per_task_cap=10_000.0,
                max_iterations=1,
                max_workers=1,
            )

            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["skipped"], 2)
            self.assertEqual(summary["stop_reason"], "budget")
            self.assertEqual(summary["tokens_spent"], 100.0)
            self.assertEqual(fake.calls, 1)
            self.assertEqual(list(root.rglob("answer.txt")), [])


class FakeLLMUsageTests(unittest.TestCase):
    def test_fake_llm_usage_produces_exact_tokens_spent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            _coding_task(root, "coding_a")
            fake = CountingLLM(text="MOCK", prompt_tokens=50, completion_tokens=10)

            summary = _run(
                root,
                Path(tmp) / "out",
                offline=False,
                llm=fake,
                budget=1_000.0,
                max_iterations=2,
                max_tokens=64,
                max_workers=1,
            )

            # Two unusable patches, each response costs 50/10 + 10 = 15.
            self.assertEqual(fake.calls, 2)
            self.assertEqual(summary["tokens_spent"], 30.0)
            self.assertEqual(summary["prompt_tokens"], 100)
            self.assertEqual(summary["completion_tokens"], 20)
            self.assertEqual(summary["budget"]["spent"], 30.0)
            self.assertEqual(summary["budget"]["remaining"], 970.0)
            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["failed"], 1)
            record = summary["results"][0]
            self.assertEqual(record["tokens"], 30.0)
            self.assertEqual(list(root.rglob("answer.txt")), [])


if __name__ == "__main__":
    unittest.main()
