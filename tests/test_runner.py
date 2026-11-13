from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.llm import DEFAULT_MODEL, LLMClient, LLMConfig
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
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


class RunnerTests(unittest.TestCase):
    def test_dry_run_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            out = Path(tmp) / "out"
            summary = run(
                RunOptions(
                    tasks_dir=tasks,
                    out=out,
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
            self.assertEqual(summary["policy"], "value_cost_greedy")
            self.assertEqual(summary["planner"]["accepted"], 8)
            self.assertEqual(summary["planner"]["rejected"], 0)
            self.assertEqual(summary["reconsidered"], 0)
            self.assertEqual(summary["final_accepted"], 8)
            self.assertEqual(summary["estimator"]["total"], 8)
            self.assertEqual(summary["estimator"]["confidence"]["high"], 6)
            self.assertEqual(summary["estimator"]["confidence"]["low"], 2)
            self.assertIn("budget_source", summary)
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
                    self.assertTrue(workspace_answer.is_file())
                    self.assertEqual(workspace_answer.read_text(encoding="utf-8"), "")

            per_type = summary["per_type"]
            self.assertEqual(per_type["math"]["solved"], 2)
            self.assertEqual(per_type["data_analysis"]["solved"], 2)
            self.assertEqual(per_type["search"]["solved"], 2)
            self.assertEqual(per_type["coding"]["failed"], 2)
            self.assertEqual(summary["budget"]["spent"], 0.0)
            self.assertEqual(summary["policy"], "value_cost_greedy")
            self.assertEqual(summary["planner"]["accepted"], 8)
            self.assertEqual(summary["planner"]["rejected"], 0)
            self.assertEqual(summary["reconsidered"], 0)
            self.assertEqual(summary["final_accepted"], 8)
            self.assertEqual(summary["estimator"]["variant"]["deterministic"], 6)
            self.assertEqual(summary["budget_source"], "runner-default")
            self.assertIn("remaining", summary["budget"])

            solutions = json.loads((out / "solutions.json").read_text(encoding="utf-8"))
            self.assertEqual(solutions["count"], 8)
            trace_lines = (out / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(trace_lines), 8)
            statuses = {json.loads(line)["task_id"]: json.loads(line)["status"] for line in trace_lines}
            for task_id in DETERMINISTIC:
                self.assertEqual(statuses[task_id], "solved")
            for task_id in CODING:
                self.assertEqual(statuses[task_id], "failed")

    def test_offline_mutation_repair_solves_all_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            summary = run(
                RunOptions(
                    tasks_dir=tasks,
                    out=Path(tmp) / "out",
                    offline=True,
                    mutation_repair=True,
                    max_workers=1,
                    time_limit=60,
                )
            )
            self.assertEqual(summary["solved"], 8)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["skipped"], 0)
            self.assertEqual(summary["score"], 20.0)
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertTrue(list(tasks.rglob("answer.txt")))
            self.assertEqual(summary["planner"]["accepted"], 8)

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
            answers = list(tasks.rglob("answer.txt"))
            self.assertGreaterEqual(len(answers), 8)
            self.assertTrue(all(path.read_text(encoding="utf-8") == "" for path in answers))

    def test_dry_run_writes_plan_predictions_when_out_is_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = copy_examples(Path(tmp) / "tasks")
            out = Path(tmp) / "out"
            summary = run(
                RunOptions(
                    tasks_dir=tasks,
                    out=out,
                    dry_run=True,
                    offline=True,
                )
            )
            self.assertEqual(summary["predictions_path"], str(out / "predictions.jsonl"))
            self.assertFalse((out / "solutions.json").exists())
            self.assertFalse((out / "trace.jsonl").exists())
            self.assertEqual(list(tasks.rglob("answer.txt")), [])

            lines = (out / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), summary["tasks"])
            task_ids = set()
            for line in lines:
                record = json.loads(line)
                for key in (
                    "task_id",
                    "task_type",
                    "solver",
                    "model",
                    "expected_cost",
                    "upper_cost",
                    "confidence",
                    "drivers",
                    "theory",
                    "ts",
                ):
                    self.assertIn(key, record)
                task_ids.add(record["task_id"])
                self.assertEqual(record["model"], DEFAULT_MODEL)
            self.assertEqual(task_ids, {item["task_id"] for item in summary["plan"]})

    def test_predictions_are_written_once_per_task_and_trace_has_audit_fields(self) -> None:
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
            self.assertEqual(summary["predictions_path"], str(out / "predictions.jsonl"))

            # One prediction per task even though the runner re-estimates and
            # replans after each completed task.
            predictions = [
                json.loads(line)
                for line in (out / "predictions.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(predictions), 8)
            self.assertEqual(len({row["task_id"] for row in predictions}), 8)
            self.assertEqual(
                {row["task_id"] for row in predictions},
                {record["task_id"] for record in summary["results"]},
            )
            for row in predictions:
                self.assertGreaterEqual(row["upper_cost"], row["expected_cost"])

            trace = {
                record["task_id"]: record
                for record in (
                    json.loads(line)
                    for line in (out / "trace.jsonl").read_text(encoding="utf-8").splitlines()
                )
            }
            self.assertEqual(len(trace), 8)
            for task_id, record in trace.items():
                with self.subTest(task=task_id):
                    self.assertIn("y_hat", record)
                    self.assertIn("y_actual", record)
                    self.assertIn("theory", record)
                    self.assertIn("estimator_confidence", record)
                    self.assertEqual(record["model"], DEFAULT_MODEL)
                    if task_id in DETERMINISTIC:
                        self.assertEqual(record["theory"], "deterministic")
                        self.assertEqual(record["estimator_confidence"], "high")
                        self.assertEqual(record["y_hat"], 0.0)
                        self.assertEqual(record["y_actual"], 0.0)
                    else:
                        self.assertNotEqual(record["theory"], "deterministic")
                        self.assertEqual(record["estimator_confidence"], "low")

            for task_id in DETERMINISTIC:
                with self.subTest(answer=task_id):
                    self.assertEqual(_answer(tasks, task_id), expected_for(task_id))

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
