from __future__ import annotations

import tempfile
import unittest

from harness.planner import DETERMINISTIC_TYPES, build_plan, estimate_cost
from harness.task import load_tasks

from tests.support import EXAMPLES, make_task


class PlannerTests(unittest.TestCase):
    def test_examples_plan_deterministic_first(self) -> None:
        plan = build_plan(load_tasks(EXAMPLES))
        self.assertEqual(len(plan), 8)
        self.assertEqual(sum(1 for item in plan if item.status == "planned"), 8)
        self.assertEqual([item.deterministic for item in plan], [True] * 6 + [False] * 2)
        self.assertEqual(
            {item.task.task_type for item in plan[:6]},
            set(DETERMINISTIC_TYPES),
        )
        self.assertEqual([item.task.task_id for item in plan[-2:]], ["coding_0003", "coding_0007"])

    def test_infeasible_is_skipped_and_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            feasible = make_task(tmp, "math_ok", task_type="math", instruction="Compute 2 + 2.", reward=1)
            infeasible = make_task(
                tmp,
                "search_bad",
                task_type="search",
                instruction="The kb/ knowledge base contains documents. Who is Alice Smith?",
            )
            assert infeasible.workspace is not None
            (infeasible.workspace / "kb").mkdir()
            coding = make_task(
                tmp,
                "coding_ok",
                task_type="coding",
                instruction="Run python3 test_mylib.py and fix mylib.py.",
                files={
                    "mylib.py": "def f():\n    return 1\n",
                    "test_mylib.py": "import mylib\nassert mylib.f() == 1\n",
                },
            )
            plan = build_plan([feasible, infeasible, coding])
            self.assertEqual(plan[-1].task.task_id, "search_bad")
            self.assertEqual(plan[-1].status, "skipped")
            self.assertIn("impossible", plan[-1].reason)
            self.assertEqual([item.status for item in plan[:2]], ["planned", "planned"])

    def test_cost_estimates_are_bounded(self) -> None:
        tasks = {task.task_id: task for task in load_tasks(EXAMPLES)}
        math_cost = estimate_cost(tasks["math_0000"])
        coding_cost = estimate_cost(tasks["coding_0003"])
        self.assertLess(math_cost, coding_cost)
        self.assertLessEqual(math_cost, 250.0)
        self.assertLessEqual(coding_cost, 3000.0)
        self.assertGreater(coding_cost, 1000.0)

    def test_priority_uses_reward_over_cost(self) -> None:
        tasks = {task.task_id: task for task in load_tasks(EXAMPLES)}
        plan = {item.task.task_id: item for item in build_plan(list(tasks.values()))}
        self.assertGreater(plan["math_0000"].priority, plan["math_0004"].priority)
        self.assertAlmostEqual(
            plan["math_0000"].priority,
            plan["math_0000"].task.reward / plan["math_0000"].estimated_cost,
        )

    def test_plan_items_are_json_ready(self) -> None:
        plan = build_plan(load_tasks(EXAMPLES))
        payload = [item.as_dict() for item in plan]
        self.assertEqual(payload[0]["status"], "planned")
        self.assertIn("triage", payload[0])
        self.assertIn("estimated_cost", payload[0])


if __name__ == "__main__":
    unittest.main()
