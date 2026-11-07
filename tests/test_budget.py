from __future__ import annotations

import concurrent.futures
import unittest

from harness.budget import Budget, BudgetExhausted, cost_of


class BudgetTests(unittest.TestCase):
    def test_cost_formula(self) -> None:
        self.assertEqual(cost_of(100, 50), 10.0 + 50.0)
        self.assertEqual(cost_of(0, 0), 0.0)
        self.assertAlmostEqual(cost_of(1, 1), 1.1)
        with self.assertRaises(ValueError):
            cost_of(-1, 0)

    def test_charge_updates_counters_and_remaining(self) -> None:
        budget = Budget(1000.0)
        cost = budget.charge(100, 50, "t1")
        self.assertEqual(cost, 60.0)
        self.assertEqual(budget.spent, 60.0)
        self.assertEqual(budget.remaining, 940.0)
        self.assertEqual(budget.prompt_tokens, 100)
        self.assertEqual(budget.completion_tokens, 50)
        self.assertEqual(budget.task_spent("t1"), 60.0)
        self.assertFalse(budget.exhausted)

    def test_reserve_blocks_new_calls(self) -> None:
        budget = Budget(100.0, reserve=20.0)
        budget.check("t", estimated_cost=1.0)
        budget.charge(0, 80, "t")
        self.assertEqual(budget.remaining, 20.0)
        self.assertFalse(budget.can_start_task())
        with self.assertRaises(BudgetExhausted) as ctx:
            budget.check("t", estimated_cost=1.0)
        self.assertIn("reserve", str(ctx.exception))

    def test_per_task_cap(self) -> None:
        budget = Budget(100_000.0, per_task_cap=100.0)
        budget.check("t", estimated_cost=100.0)
        budget.charge(0, 100, "t")
        with self.assertRaises(BudgetExhausted) as ctx:
            budget.check("t", estimated_cost=1.0)
        self.assertIn("per-task cap", str(ctx.exception))
        # A different task is unaffected.
        budget.check("other", estimated_cost=100.0)

    def test_global_exhaustion_hard_stop(self) -> None:
        budget = Budget(10.0)
        budget.charge(0, 10, "t")
        self.assertEqual(budget.remaining, 0.0)
        self.assertFalse(budget.can_start_task())
        self.assertTrue(budget.exhausted)
        with self.assertRaises(BudgetExhausted):
            budget.check("next")

    def test_overspend_does_not_raise_but_stops(self) -> None:
        budget = Budget(5.0)
        budget.charge(0, 100, "t")
        self.assertLess(budget.remaining, 0)
        self.assertTrue(budget.exhausted)
        self.assertFalse(budget.can_start_task())

    def test_stop_is_sticky(self) -> None:
        budget = Budget(1000.0)
        budget.stop()
        self.assertFalse(budget.can_start_task())
        with self.assertRaises(BudgetExhausted):
            budget.check()

    def test_thread_safety(self) -> None:
        budget = Budget(100_000.0, per_task_cap=100_000.0)

        def worker() -> None:
            for _ in range(100):
                budget.charge(0, 1, "shared")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: worker(), range(8)))

        self.assertEqual(budget.spent, 800.0)
        self.assertEqual(budget.completion_tokens, 800)
        self.assertEqual(budget.task_spent("shared"), 800.0)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot.total, 100_000.0)
        self.assertFalse(snapshot.stopped)

    def test_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            Budget(-1)
        with self.assertRaises(ValueError):
            Budget(10, reserve=-1)
        with self.assertRaises(ValueError):
            Budget(10, per_task_cap=0)


if __name__ == "__main__":
    unittest.main()
