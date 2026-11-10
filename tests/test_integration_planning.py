"""Integration: the policy/estimator layer drives the production runner.

These tests use a fake estimator and a recording solver so planning order and
skip reasons are observable without relying on the real estimators.  They also
assert that replanning reads the live budget ledger after each completed task
and that rejected tasks never receive an ``answer.txt``.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import runner as runner_module
from harness.budget import BudgetLedger
from harness.estimator import CostEstimate
from harness.llm import LLMConfig, build_llm_client
from harness.policy import replan as real_replan
from harness.runner import RunOptions, run
from harness.solvers.base import SolveResult
from run_agent import build_parser
from tests.support import make_task


class FixedEstimator:
    """Return caller-supplied upper/expected costs for task ids."""

    def __init__(self, costs: dict[str, tuple[float, float]]) -> None:
        self.costs = costs
        self.default = (0.0, 0.0)

    def estimate(self, task: Any, solver: str, model: str) -> CostEstimate:
        expected, upper = self.costs.get(task.task_id, self.default)
        return CostEstimate(
            expected_cost=float(expected),
            upper_cost=float(upper),
            expected_input_tokens=0.0,
            expected_output_tokens=float(expected),
            confidence="high",
            model=model,
            solver=solver,
            drivers=("scope=test",),
        )


class RecordingSolver:
    """Solve every task with the task id and record execution order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, task: Any, ctx: Any) -> SolveResult:
        with self._lock:
            self.calls.append(task.task_id)
        return SolveResult(task.task_id, 0.0, "fake", "ok")


class ChargingSolver(RecordingSolver):
    """Charge one cost unit per task through the shared ledger."""

    def __call__(self, task: Any, ctx: Any) -> SolveResult:
        with self._lock:
            self.calls.append(task.task_id)
        cost = ctx.budget.charge(10, 0, task.task_id)
        return SolveResult(task.task_id, cost, "fake", "ok")


def _answers(root: Path) -> list[Path]:
    return sorted(root.rglob("answer.txt"))


class PlanningIntegrationTests(unittest.TestCase):
    def test_policy_order_and_cost_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_cheap", instruction="Compute 2 + 2.", reward=1.0)
            make_task(root, "math_pricey", instruction="Compute 3 + 3.", reward=10.0)
            make_task(root, "math_second", instruction="Compute 4 + 4.", reward=5.0)
            estimator = FixedEstimator(
                {
                    "math_cheap": (0.0, 0.0),
                    "math_pricey": (500.0, 600.0),
                    "math_second": (500.0, 600.0),
                }
            )
            solver = RecordingSolver()
            out = Path(tmp) / "out"

            with mock.patch.dict(runner_module._SOLVERS, {"math": solver}):
                summary = run(
                    RunOptions(
                        tasks_dir=root,
                        out=out,
                        offline=True,
                        budget=1000.0,
                        planner_reserve=0.0,
                        max_workers=1,
                        time_limit=60.0,
                        estimator=estimator,
                    )
                )

            # The zero-cost task sorts first, then the higher-value task; the
            # lower-value task does not fit the remaining capacity.
            self.assertEqual(solver.calls, ["math_cheap", "math_pricey"])
            self.assertEqual(summary["solved"], 2)
            self.assertEqual(summary["skipped"], 1)

            planner = summary["planner"]
            self.assertEqual(planner["accepted"], 2)
            self.assertEqual(planner["rejected"], 1)
            self.assertEqual(planner["reasons"]["reject:cost"], 1)

            self.assertEqual(summary["policy"], "value_cost_greedy")
            self.assertIn("variant", summary["estimator"])
            self.assertIn("confidence", summary["estimator"])
            self.assertEqual(summary["estimator"]["total"], 3)
            self.assertIn("budget_source", summary)
            self.assertIn("total", summary["budget"])

            # Rejected tasks are skipped in both artefacts and never get an
            # answer file.
            solutions = json.loads((out / "solutions.json").read_text(encoding="utf-8"))
            records = {record["task_id"]: record for record in solutions["solutions"]}
            self.assertEqual(records["math_second"]["status"], "skipped")
            self.assertEqual(records["math_second"]["notes"], "reject:cost")
            self.assertFalse((root / "math_second" / "answer.txt").exists())
            self.assertFalse(
                (root / "math_second" / "workspace" / "answer.txt").exists()
            )

            trace = {
                json.loads(line)["task_id"]: json.loads(line)
                for line in (out / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            }
            self.assertEqual(trace["math_second"]["status"], "skipped")
            self.assertEqual(trace["math_second"]["notes"], "reject:cost")

    def test_impossible_rejection_has_no_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_ok", instruction="Compute 2 + 2.")
            infeasible = make_task(
                root,
                "search_bad",
                task_type="search",
                instruction=(
                    "The directory kb/ contains a small knowledge base. "
                    "Who is Alice Smith?"
                ),
                check="exact_str",
                files={"kb/readme.txt": "plain text, not markdown\n"},
            )
            self.assertIsNotNone(infeasible.workspace)
            solver = RecordingSolver()

            with mock.patch.dict(runner_module._SOLVERS, {"math": solver}):
                summary = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out",
                        offline=True,
                        budget=1000.0,
                        max_workers=1,
                        time_limit=60.0,
                        estimator=FixedEstimator({}),
                    )
                )

            self.assertEqual(summary["skipped"], 1)
            self.assertEqual(summary["solved"], 1)
            self.assertEqual(summary["planner"]["reasons"]["reject:impossible"], 1)
            self.assertEqual(solver.calls, ["math_ok"])
            self.assertFalse((root / "search_bad" / "answer.txt").exists())
            self.assertFalse(
                (root / "search_bad" / "workspace" / "answer.txt").exists()
            )

    def test_zero_time_limit_rejects_every_task_as_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_a", instruction="Compute 1 + 1.")
            make_task(root, "math_b", instruction="Compute 2 + 2.")
            solver = RecordingSolver()

            with mock.patch.dict(runner_module._SOLVERS, {"math": solver}):
                summary = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out",
                        offline=True,
                        budget=1000.0,
                        max_workers=1,
                        time_limit=0.0,
                        estimator=FixedEstimator({}),
                    )
                )

            self.assertEqual(solver.calls, [])
            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], 2)
            self.assertEqual(summary["stop_reason"], "time_limit")
            self.assertEqual(summary["planner"]["reasons"]["reject:time"], 2)
            self.assertEqual(_answers(root), [])

    def test_replan_uses_live_ledger_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            for index in range(3):
                make_task(root, f"math_{index}", instruction="Compute 1 + 1.")
            solver = ChargingSolver()
            captured: list[Any] = []

            def recording_replan(tasks, estimates, snapshot, time_left, **kwargs):
                captured.append(snapshot.remaining)
                return real_replan(tasks, estimates, snapshot, time_left, **kwargs)

            with (
                mock.patch.dict(runner_module._SOLVERS, {"math": solver}),
                mock.patch.object(runner_module, "replan", side_effect=recording_replan),
            ):
                summary = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out",
                        offline=True,
                        budget=100.0,
                        max_workers=1,
                        time_limit=60.0,
                        estimator=FixedEstimator({}),
                    )
                )

            self.assertEqual(summary["solved"], 3)
            self.assertEqual(summary["replans"], 2)
            self.assertEqual(captured, [99.0, 98.0])
            self.assertEqual(summary["budget"]["spent"], 3.0)

    def test_gateway_backend_receives_the_shared_ledger(self) -> None:
        ledger = BudgetLedger(100.0)
        client = build_llm_client(
            LLMConfig(base_url="http://proxy.invalid", api_key="test-key"),
            backend="gateway",
            ledger=ledger,
        )
        try:
            self.assertEqual(client.backend_name, "openjiuwen-gateway")
            self.assertIs(client.gateway._ledger, ledger)
        finally:
            client.close()

    def test_gateway_accounting_view_does_not_double_charge(self) -> None:
        ledger = BudgetLedger(100.0)
        ledger.charge(10, 0)  # what the gateway records after a response
        spent = ledger.spent

        view = runner_module._GatewaySolverLedger(ledger)
        self.assertEqual(view.charge(10, 0), 1.0)
        self.assertEqual(ledger.spent, spent)
        self.assertEqual(view.remaining, ledger.remaining)

    def test_cli_defaults_to_value_cost_greedy_and_accepts_gateway(self) -> None:
        args = build_parser().parse_args(["--tasks-dir", "tasks"])
        self.assertEqual(args.policy, "value_cost_greedy")
        self.assertIsNone(args.planner_reserve)
        self.assertEqual(args.llm_backend, "auto")

        gateway = build_parser().parse_args(
            [
                "--tasks-dir",
                "tasks",
                "--policy",
                "reward_sort",
                "--planner-reserve",
                "0.07",
                "--llm-backend",
                "gateway",
            ]
        )
        self.assertEqual(gateway.policy, "reward_sort")
        self.assertEqual(gateway.planner_reserve, 0.07)
        self.assertEqual(gateway.llm_backend, "gateway")


if __name__ == "__main__":
    unittest.main()
