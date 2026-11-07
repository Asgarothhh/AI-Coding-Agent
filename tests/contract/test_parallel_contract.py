"""Contract: parallel execution safety.

The harness caps parallel sessions at five.  With a deliberately slow solver
we verify that every planned task is attempted exactly once, answers are not
duplicated or mixed up, the shared budget is charged exactly once per call,
and the final report is deterministic regardless of scheduling.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import runner as runner_module
from harness.runner import RunOptions, run
from harness.solvers.base import SolveResult

from tests.support import make_task

TASK_COUNT = 10
PROMPT_TOKENS_PER_CALL = 10  # -> 10 / 10 = 1.0 cost unit per task


class SlowFakeSolver:
    """Sleeps, then charges a fixed amount through the shared budget."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self.charged: list[float] = []
        self.max_spent_seen = 0.0
        self._lock = threading.Lock()

    def __call__(self, task, ctx) -> SolveResult:
        with self._lock:
            self.calls.append(task.task_id)
        time.sleep(self.delay)
        ctx.budget.check(task.task_id, estimated_cost=1.0)
        cost = ctx.budget.charge(
            PROMPT_TOKENS_PER_CALL, 0, task.task_id
        )
        with self._lock:
            self.charged.append(cost)
            self.max_spent_seen = max(self.max_spent_seen, ctx.budget.spent)
        return SolveResult(task.task_id, cost, "fake", "ok")


def _make_tasks(root: Path, count: int = TASK_COUNT) -> list[str]:
    task_ids: list[str] = []
    for index in range(count):
        task_id = f"math_{index:02d}"
        make_task(
            root,
            task_id,
            task_type="math",
            instruction="Compute 1 + 1.",
        )
        task_ids.append(task_id)
    return task_ids


def _projection(summary: dict[str, Any]) -> list[tuple[str, str, str | None, float]]:
    return [
        (
            record["task_id"],
            record["status"],
            record["answer"],
            float(record["tokens"]),
        )
        for record in summary["results"]
    ]


class ParallelContractTests(unittest.TestCase):
    def test_all_tasks_attempted_once_without_duplicate_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            task_ids = _make_tasks(root)
            solver = SlowFakeSolver()

            with mock.patch.dict(runner_module._SOLVERS, {"math": solver}):
                summary = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out",
                        offline=True,
                        max_workers=5,
                        budget=100.0,
                        time_limit=60.0,
                    )
                )

            self.assertEqual(summary["solved"], TASK_COUNT)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["skipped"], 0)
            self.assertIsNone(summary["stop_reason"])

            # Every task was attempted exactly once.
            self.assertEqual(sorted(solver.calls), sorted(task_ids))
            self.assertEqual(len(solver.calls), TASK_COUNT)
            self.assertEqual(len(set(solver.calls)), TASK_COUNT)

            # No duplicate or cross-written answers.
            answers = {}
            for task_id in task_ids:
                answer_path = root / task_id / "answer.txt"
                workspace_answer = (
                    root / task_id / "workspace" / "answer.txt"
                )
                with self.subTest(task=task_id):
                    self.assertTrue(answer_path.is_file())
                    self.assertTrue(workspace_answer.is_file())
                    answers[task_id] = answer_path.read_text(encoding="utf-8").strip()
                    self.assertEqual(
                        workspace_answer.read_text(encoding="utf-8").strip(),
                        task_id,
                    )
            self.assertEqual(len(set(answers.values())), TASK_COUNT)
            for task_id, answer in answers.items():
                self.assertEqual(answer, task_id)

            self.assertEqual(len(summary["results"]), TASK_COUNT)
            self.assertEqual(
                [record["task_id"] for record in summary["results"]],
                sorted(task_ids),
            )

            # Shared budget is charged exactly once per task.
            self.assertEqual(len(solver.charged), TASK_COUNT)
            self.assertEqual(sum(solver.charged), float(TASK_COUNT))
            self.assertEqual(summary["tokens_spent"], float(TASK_COUNT))
            self.assertEqual(summary["budget"]["spent"], float(TASK_COUNT))
            self.assertEqual(summary["budget"]["remaining"], 90.0)
            self.assertLessEqual(solver.max_spent_seen, summary["budget"]["total"])

    def test_results_are_deterministic_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            task_ids = _make_tasks(root)
            solver_one = SlowFakeSolver(delay=0.03)
            solver_two = SlowFakeSolver(delay=0.01)

            with mock.patch.dict(runner_module._SOLVERS, {"math": solver_one}):
                first = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out-one",
                        offline=True,
                        max_workers=5,
                        budget=100.0,
                        time_limit=60.0,
                    )
                )
            with mock.patch.dict(runner_module._SOLVERS, {"math": solver_two}):
                second = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out-two",
                        offline=True,
                        max_workers=5,
                        budget=100.0,
                        time_limit=60.0,
                    )
                )

            self.assertEqual(_projection(first), _projection(second))
            self.assertEqual(first["solved"], second["solved"])
            self.assertEqual(first["tokens_spent"], second["tokens_spent"])
            self.assertEqual(
                sorted(solver_one.calls), sorted(solver_two.calls)
            )
            self.assertEqual(
                [record["task_id"] for record in first["results"]],
                sorted(task_ids),
            )


if __name__ == "__main__":
    unittest.main()
