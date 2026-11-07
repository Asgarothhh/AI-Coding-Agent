from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.llm import LLMClient, LLMConfig
from harness.mock_proxy import MockProxyServer
from harness.runner import RunOptions, run

from tests.support import copy_examples, expected_for

DETERMINISTIC = (
    "math_0000",
    "math_0004",
    "data_analysis_0002",
    "data_analysis_0006",
    "search_0001",
    "search_0005",
)
CODING = ("coding_0003", "coding_0007")


def _answer(root: Path, task_id: str) -> str | None:
    path = root / task_id / "answer.txt"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


class RunnerTests(unittest.TestCase):
    def test_dry_run_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            summary = run(
                RunOptions(
                    tasks_dir=tasks,
                    out=Path(tmp) / "out",
                    dry_run=True,
                    offline=True,
                )
            )
            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["tasks"], 8)
            self.assertEqual(summary["planned"], 8)
            self.assertEqual(summary["infeasible"], 0)
            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertEqual(len(summary["plan"]), 8)
            self.assertEqual(
                [item["deterministic"] for item in summary["plan"]],
                [True] * 6 + [False] * 2,
            )
            self.assertEqual(list(tasks.rglob("answer.txt")), [])

    def test_offline_run_solves_six_examples_with_zero_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            out = Path(tmp) / "out"
            summary = run(
                RunOptions(
                    tasks_dir=tasks,
                    out=out,
                    offline=True,
                    max_workers=1,
                    time_limit=60,
                )
            )
            self.assertEqual(summary["solved"], 6)
            self.assertEqual(summary["failed"], 2)
            self.assertEqual(summary["skipped"], 0)
            self.assertEqual(summary["errors"], 0)
            self.assertEqual(summary["score"], 16.0)
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertEqual(summary["prompt_tokens"], 0)
            self.assertEqual(summary["completion_tokens"], 0)
            self.assertIsNone(summary["stop_reason"])

            for task_id in DETERMINISTIC:
                with self.subTest(task=task_id):
                    expected = expected_for(task_id)
                    self.assertEqual(_answer(tasks, task_id), expected)
                    workspace = tasks / task_id / "workspace"
                    if workspace.is_dir():
                        self.assertEqual(
                            (workspace / "answer.txt").read_text(encoding="utf-8").strip(),
                            expected,
                        )

            for task_id in CODING:
                with self.subTest(task=task_id):
                    self.assertIsNone(_answer(tasks, task_id))
                    workspace_answer = tasks / task_id / "workspace" / "answer.txt"
                    self.assertFalse(workspace_answer.exists())

            per_type = summary["per_type"]
            self.assertEqual(per_type["math"]["solved"], 2)
            self.assertEqual(per_type["data_analysis"]["solved"], 2)
            self.assertEqual(per_type["search"]["solved"], 2)
            self.assertEqual(per_type["coding"]["failed"], 2)
            self.assertEqual(summary["budget"]["spent"], 0.0)

            solutions = json.loads((out / "solutions.json").read_text(encoding="utf-8"))
            self.assertEqual(solutions["count"], 8)
            trace_lines = (out / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(trace_lines), 8)
            statuses = {json.loads(line)["task_id"]: json.loads(line)["status"] for line in trace_lines}
            for task_id in DETERMINISTIC:
                self.assertEqual(statuses[task_id], "solved")
            for task_id in CODING:
                self.assertEqual(statuses[task_id], "failed")

    def test_mock_llm_run_charges_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            with MockProxyServer(response_text="MOCK") as proxy:
                client = LLMClient(
                    LLMConfig(
                        base_url=proxy.base_url,
                        api_key="mock-key",
                        max_retries=1,
                        backoff_base=0.01,
                        timeout=10.0,
                        max_tokens=64,
                    )
                )
                summary = run(
                    RunOptions(
                        tasks_dir=tasks,
                        out=Path(tmp) / "out",
                        offline=False,
                        llm=client,
                        max_workers=1,
                        max_iterations=2,
                        max_tokens=64,
                        time_limit=60,
                    )
                )

            self.assertEqual(summary["solved"], 6)
            self.assertEqual(summary["failed"], 2)
            self.assertGreater(summary["tokens_spent"], 0.0)
            self.assertGreater(summary["prompt_tokens"], 0)
            self.assertGreater(summary["completion_tokens"], 0)
            expected_cost = summary["prompt_tokens"] / 10.0 + summary["completion_tokens"]
            self.assertAlmostEqual(summary["tokens_spent"], expected_cost, places=6)
            self.assertGreaterEqual(proxy.request_count, 4)
            for task_id in CODING:
                self.assertIsNone(_answer(tasks, task_id))
                coding_result = next(
                    record for record in summary["results"] if record["task_id"] == task_id
                )
                self.assertGreater(coding_result["tokens"], 0.0)

    def test_zero_budget_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            summary = run(
                RunOptions(
                    tasks_dir=tasks,
                    out=Path(tmp) / "out",
                    offline=True,
                    budget=0.0,
                    max_workers=2,
                    time_limit=60,
                )
            )
            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], 8)
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertEqual(summary["stop_reason"], "budget")
            self.assertEqual(list(tasks.rglob("answer.txt")), [])

    def test_time_limit_stops_new_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            summary = run(
                RunOptions(
                    tasks_dir=tasks,
                    out=Path(tmp) / "out",
                    offline=True,
                    time_limit=0.001,
                    max_workers=1,
                )
            )
            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["skipped"], 8)
            self.assertIn(summary["stop_reason"], {"time_limit", "budget"})


if __name__ == "__main__":
    unittest.main()
