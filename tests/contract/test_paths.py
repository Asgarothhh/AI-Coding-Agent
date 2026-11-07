"""Contract: where answers and solutions.json are written.

The challenge checker looks for ``answer.txt`` next to ``task.json`` and,
when a ``workspace/`` directory exists, also inside the workspace.  Tasks that
are structurally infeasible must not leave an answer behind, and
``solutions.json`` must be flushed after every single solution so partial
progress survives a killed container.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.budget import Budget
from harness.runner import RunOptions, run
from harness.solvers.base import SolveResult, SolverContext, SolverOptions
from harness.solvers.math import solve as solve_math
from harness.store import AnswerStore
from harness.task import load_tasks

from tests.support import make_task


class AnswerLocationTests(unittest.TestCase):
    def test_solver_then_store_writes_task_root_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "with_ws", instruction="Compute 2 + 2.")
            self.assertTrue(task.has_workspace)
            self.assertIsNotNone(task.workspace)

            result = solve_math(
                task,
                SolverContext(budget=Budget(1_000.0), options=SolverOptions()),
            )
            self.assertEqual(result.answer, "4")

            store = AnswerStore(Path(tmp) / "out")
            written = store.write_answer(task, result.answer or "")

            # Root first (official contract), workspace second (fallback).
            self.assertEqual(written, task.answer_locations())
            self.assertEqual(written[0], task.task_dir / "answer.txt")
            self.assertEqual(
                (task.task_dir / "answer.txt").read_text(encoding="utf-8"), "4\n"
            )
            workspace_answer = task.workspace / "answer.txt"
            self.assertTrue(workspace_answer.is_file())
            self.assertEqual(workspace_answer.read_text(encoding="utf-8"), "4\n")

    def test_task_without_workspace_gets_only_root_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "bare", workspace=False, instruction="Compute 3 + 5.")
            self.assertFalse(task.has_workspace)
            self.assertEqual(
                task.answer_locations(), (task.task_dir / "answer.txt",)
            )

            result = solve_math(
                task,
                SolverContext(budget=Budget(1_000.0), options=SolverOptions()),
            )
            self.assertEqual(result.answer, "8")

            store = AnswerStore(Path(tmp) / "out")
            written = store.write_answer(task, result.answer or "")

            self.assertEqual(written, (task.task_dir / "answer.txt",))
            self.assertEqual(
                (task.task_dir / "answer.txt").read_text(encoding="utf-8"), "8\n"
            )
            self.assertFalse((task.task_dir / "workspace").exists())

    def test_infeasible_tasks_leave_no_answer_in_either_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            missing_file = make_task(
                root,
                "missing_file",
                task_type="data_analysis",
                instruction=(
                    "The file missing.csv has columns id,units. "
                    "How many units are there?"
                ),
                files={"data.csv": "id,units\n1,5\n2,7\n"},
                check="exact_int",
            )
            empty_kb = make_task(
                root,
                "empty_kb",
                task_type="search",
                instruction=(
                    "The directory kb/ contains a small knowledge base. "
                    "Who is Alice Smith?"
                ),
                check="exact_str",
            )
            assert empty_kb.workspace is not None
            (empty_kb.workspace / "kb").mkdir()

            summary = run(
                RunOptions(
                    tasks_dir=root,
                    out=Path(tmp) / "out",
                    offline=True,
                    max_workers=1,
                    time_limit=60.0,
                )
            )

            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], 2)
            for task in (missing_file, empty_kb):
                with self.subTest(task=task.task_id):
                    self.assertFalse((task.task_dir / "answer.txt").exists())
                    if task.workspace is not None:
                        self.assertFalse((task.workspace / "answer.txt").exists())
            self.assertEqual(list(root.rglob("answer.txt")), [])


class SolutionsJsonTests(unittest.TestCase):
    def _record(self, store: AnswerStore, task, answer: str) -> None:
        store.record_solution(
            task,
            SolveResult(answer, 0.0, task.task_type, "contract"),
            "solved",
            0.01,
        )

    def test_out_directory_is_updated_after_each_solution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            first = make_task(root, "math_a", instruction="Compute 1 + 1.")
            second = make_task(root, "math_b", instruction="Compute 2 + 2.")

            out_dir = Path(tmp) / "deep" / "out"
            store = AnswerStore(out_dir)
            self.assertEqual(store.out_dir, out_dir)
            self.assertEqual(store.solutions_path, out_dir / "solutions.json")

            self._record(store, first, "2")
            after_first = json.loads(store.solutions_path.read_text(encoding="utf-8"))
            self.assertEqual(after_first["count"], 1)
            self.assertEqual(
                [record["task_id"] for record in after_first["solutions"]],
                ["math_a"],
            )

            self._record(store, second, "4")
            after_second = json.loads(store.solutions_path.read_text(encoding="utf-8"))
            self.assertEqual(after_second["count"], 2)
            self.assertEqual(
                [record["task_id"] for record in after_second["solutions"]],
                ["math_a", "math_b"],
            )
            self.assertEqual(
                [record["answer"] for record in after_second["solutions"]],
                ["2", "4"],
            )

    def test_out_can_be_a_json_file_path_and_is_updated_after_each_solution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            first = make_task(root, "math_a", instruction="Compute 1 + 1.")
            second = make_task(root, "math_b", instruction="Compute 2 + 2.")

            target = Path(tmp) / "custom" / "solutions.json"
            store = AnswerStore(target)
            self.assertEqual(store.solutions_path, target)
            self.assertEqual(store.out_dir, target.parent)

            self._record(store, first, "2")
            self.assertTrue(target.is_file())
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["count"], 1
            )

            self._record(store, second, "4")
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                [record["task_id"] for record in payload["solutions"]],
                ["math_a", "math_b"],
            )

    def test_out_environment_variable_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "env-out"
            with mock.patch.dict(os.environ, {"OUT": str(out_dir)}):
                store = AnswerStore()
            self.assertEqual(store.out_dir, out_dir)
            self.assertEqual(store.solutions_path, out_dir / "solutions.json")

    def test_runner_accepts_a_solutions_json_file_and_writes_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_a", instruction="Compute 1 + 1.")
            make_task(root, "math_b", instruction="Compute 2 + 2.")
            target = Path(tmp) / "nested" / "solutions.json"

            summary = run(
                RunOptions(
                    tasks_dir=root,
                    out=target,
                    offline=True,
                    max_workers=1,
                    time_limit=60.0,
                )
            )

            self.assertEqual(summary["solved"], 2)
            self.assertEqual(summary["solutions_path"], str(target))
            self.assertEqual(len(load_tasks(root)), 2)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                [record["task_id"] for record in payload["solutions"]],
                ["math_a", "math_b"],
            )
            self.assertEqual(
                (root / "math_a" / "answer.txt").read_text(encoding="utf-8"), "2\n"
            )
            self.assertEqual(
                (root / "math_b" / "answer.txt").read_text(encoding="utf-8"), "4\n"
            )


if __name__ == "__main__":
    unittest.main()
