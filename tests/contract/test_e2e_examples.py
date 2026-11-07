"""Contract: end-to-end CLI behaviour on the bundled examples.

The examples are copied to a temp directory before running so this test never
writes answers into the repository.  Everything runs offline with a single
worker, which makes the result fully deterministic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "task-dir"
RUN_AGENT = ROOT / "run_agent.py"

DETERMINISTIC_ANSWERS = {
    "math_0000": "389",
    "math_0004": "1735",
    "data_analysis_0002": "158",
    "data_analysis_0006": "284",
    "search_0001": "1983",
    "search_0005": "Osaka",
}
CODING_TASKS = ("coding_0003", "coding_0007")


def _copy_examples(destination: Path) -> Path:
    shutil.copytree(EXAMPLES, destination)
    for answer in destination.rglob("answer.txt"):
        answer.unlink()
    return destination


def _run_cli(*args: str) -> dict:
    env = os.environ.copy()
    env.pop("LLM_BASE_URL", None)
    env.pop("LLM_API_KEY", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(RUN_AGENT), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"run_agent.py exited with {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    if not proc.stdout.strip():
        raise AssertionError(
            f"run_agent.py produced no JSON summary\nstderr:\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


class ExamplesEndToEndTests(unittest.TestCase):
    def test_offline_run_solves_six_examples_and_fails_two_coding_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = _copy_examples(Path(tmp) / "tasks")
            out = Path(tmp) / "out"

            summary = _run_cli(
                "--tasks-dir",
                str(tasks),
                "--out",
                str(out),
                "--offline",
                "--max-workers",
                "1",
                "--time-limit",
                "120",
                "--budget",
                "100000",
            )

            self.assertTrue(summary["offline"])
            self.assertFalse(summary["dry_run"])
            self.assertEqual(summary["tasks"], 8)
            self.assertEqual(summary["planned"], 8)
            self.assertEqual(summary["infeasible"], 0)
            self.assertEqual(summary["solved"], 6)
            self.assertEqual(summary["failed"], 2)
            self.assertEqual(summary["skipped"], 0)
            self.assertEqual(summary["errors"], 0)
            self.assertEqual(summary["score"], 16.0)
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertEqual(summary["prompt_tokens"], 0)
            self.assertEqual(summary["completion_tokens"], 0)
            self.assertIsNone(summary["stop_reason"])

            for task_id, expected in DETERMINISTIC_ANSWERS.items():
                with self.subTest(task=task_id):
                    root_answer = tasks / task_id / "answer.txt"
                    self.assertTrue(root_answer.is_file())
                    self.assertEqual(
                        root_answer.read_text(encoding="utf-8").strip(), expected
                    )
                    workspace = tasks / task_id / "workspace"
                    if workspace.is_dir():
                        workspace_answer = workspace / "answer.txt"
                        self.assertTrue(workspace_answer.is_file())
                        self.assertEqual(
                            workspace_answer.read_text(encoding="utf-8").strip(),
                            expected,
                        )

            for task_id in CODING_TASKS:
                with self.subTest(task=task_id):
                    self.assertFalse((tasks / task_id / "answer.txt").exists())
                    self.assertFalse(
                        (tasks / task_id / "workspace" / "answer.txt").exists()
                    )

            solutions = json.loads(
                (out / "solutions.json").read_text(encoding="utf-8")
            )
            self.assertEqual(solutions["count"], 8)
            statuses = {
                record["task_id"]: record["status"]
                for record in solutions["solutions"]
            }
            for task_id in DETERMINISTIC_ANSWERS:
                self.assertEqual(statuses[task_id], "solved")
            for task_id in CODING_TASKS:
                self.assertEqual(statuses[task_id], "failed")

            trace_lines = (out / "trace.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(trace_lines), 8)
            per_type = summary["per_type"]
            self.assertEqual(per_type["math"]["solved"], 2)
            self.assertEqual(per_type["data_analysis"]["solved"], 2)
            self.assertEqual(per_type["search"]["solved"], 2)
            self.assertEqual(per_type["coding"]["failed"], 2)

    def test_dry_run_plans_eight_examples_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = _copy_examples(Path(tmp) / "tasks")
            out = Path(tmp) / "out"

            summary = _run_cli(
                "--tasks-dir",
                str(tasks),
                "--out",
                str(out),
                "--offline",
                "--max-workers",
                "1",
                "--dry-run",
            )

            self.assertTrue(summary["dry_run"])
            self.assertTrue(summary["offline"])
            self.assertEqual(summary["tasks"], 8)
            self.assertEqual(summary["planned"], 8)
            self.assertEqual(summary["infeasible"], 0)
            self.assertEqual(summary["solved"], 0)
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertEqual(len(summary["plan"]), 8)
            self.assertTrue(all(item["status"] == "planned" for item in summary["plan"]))
            self.assertEqual(
                [item["deterministic"] for item in summary["plan"]],
                [True] * 6 + [False] * 2,
            )
            self.assertEqual(list(tasks.rglob("answer.txt")), [])
            self.assertFalse((out / "solutions.json").exists())


if __name__ == "__main__":
    unittest.main()
