"""Contract: wall-clock limiting.

``time_limit`` must stop *new* tasks once the limit is reached and still
return a complete summary with every task accounted for.  A zero limit is the
degenerate case of the same rule: no task may be started at all.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import runner as runner_module
from harness.runner import RunOptions, run
from harness.solvers.base import SolveResult

from tests.support import make_task

TASK_COUNT = 6


class SlowFakeSolver:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, task, ctx) -> SolveResult:  # noqa: ARG002 - contract signature
        with self._lock:
            self.calls.append(task.task_id)
        time.sleep(self.delay)
        return SolveResult(task.task_id, 0.0, "fake", "ok")


def _make_tasks(root: Path, count: int) -> list[str]:
    task_ids: list[str] = []
    for index in range(count):
        task_id = f"math_{index:02d}"
        make_task(root, task_id, instruction="Compute 1 + 1.")
        task_ids.append(task_id)
    return task_ids


class TimeLimitContractTests(unittest.TestCase):
    def test_zero_time_limit_attempts_no_tasks(self) -> None:
        """A zero wall-clock budget must not start any task.

        The current runner treats ``time_limit == 0`` as "unlimited" (its
        guards are ``if options.time_limit > 0``), which contradicts the
        budget-style zero semantics.  See the report for the exact failure.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            task_ids = _make_tasks(root, 3)
            solver = SlowFakeSolver(delay=0.0)

            with mock.patch.dict(
                runner_module._SOLVERS, {"math": solver}
            ):
                summary = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out",
                        offline=True,
                        max_workers=2,
                        time_limit=0.0,
                    )
                )

            self.assertEqual(
                solver.calls,
                [],
                "time_limit=0 must not attempt any task",
            )
            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], len(task_ids))
            self.assertEqual(summary["stop_reason"], "time_limit")
            for path in root.rglob("answer.txt"):
                self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertTrue(list(root.rglob("answer.txt")))

    def test_small_positive_limit_stops_new_tasks_and_returns_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            task_ids = _make_tasks(root, TASK_COUNT)
            solver = SlowFakeSolver(delay=0.2)

            with mock.patch.dict(
                runner_module._SOLVERS, {"math": solver}
            ):
                summary = run(
                    RunOptions(
                        tasks_dir=root,
                        out=Path(tmp) / "out",
                        offline=True,
                        max_workers=1,
                        time_limit=0.05,
                    )
                )

            # The first task already runs past the limit; no new task starts.
            self.assertEqual(summary["stop_reason"], "time_limit")
            self.assertEqual(solver.calls, [task_ids[0]])
            self.assertEqual(summary["solved"], 1)
            self.assertEqual(summary["skipped"], TASK_COUNT - 1)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(len(summary["results"]), TASK_COUNT)
            self.assertEqual(
                [record["task_id"] for record in summary["results"]],
                sorted(task_ids),
            )
            for key in (
                "tasks",
                "planned",
                "infeasible",
                "solved",
                "skipped",
                "failed",
                "tokens_spent",
                "stop_reason",
                "budget",
                "per_type",
            ):
                self.assertIn(key, summary)

            # Placeholders exist for every loaded task; only the run task is non-empty.
            answers = sorted(root.rglob("answer.txt"))
            self.assertGreaterEqual(len(answers), TASK_COUNT)
            self.assertEqual(
                (root / task_ids[0] / "answer.txt").read_text(encoding="utf-8"),
                task_ids[0] + "\n",
            )
            for task_id in task_ids[1:]:
                self.assertEqual(
                    (root / task_id / "answer.txt").read_text(encoding="utf-8"),
                    "",
                )


if __name__ == "__main__":
    unittest.main()
