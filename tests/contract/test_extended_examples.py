"""Contract: the extended 16-task example set is planned and solved offline.

``examples/task-dir-extended`` is additive on top of the frozen 8-task
``examples/task-dir`` baseline.  After the triage and search fixes every one of
its 16 tasks must be planned (nothing skipped as "no internet"), and the
deterministic offline run with mutation repair must solve at least 14 of them
for a score of 46 or more without spending a single token.

All runs happen on a temporary copy, and the repository tree is fingerprinted
before and after the test, so a stray ``answer.txt`` can never leak into the
checkout.
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

from harness.task import load_tasks

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "task-dir-extended"
RUN_AGENT = ROOT / "run_agent.py"

TASK_COUNT = 16
TASK_TYPES = frozenset({"math", "search", "data_analysis", "coding"})
REQUIRED_FIELDS = (
    "task_id",
    "task_type",
    "instruction",
    "reward",
    "expected",
    "check",
)
EXPECTED_SOLVED = 14
EXPECTED_SCORE = 46.0
# These two need more than the deterministic mutation repair can do offline;
# they must be reported as ``failed`` solutions, not as runner ``errors``.
KNOWN_FAILURES = ("coding_0008", "coding_0009")


def _copy_tasks(destination: Path) -> Path:
    """Copy the extended task set and scrub any pre-existing answers."""

    shutil.copytree(EXAMPLES, destination)
    for answer in destination.rglob("answer.txt"):
        answer.unlink()
    return destination


def _snapshot(root: Path) -> dict[str, int]:
    """Fingerprint every file below ``root`` by relative path and size."""

    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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
        timeout=180,
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


class ExtendedExamplesContractTests(unittest.TestCase):
    def test_extended_set_loads_plans_and_solves_offline(self) -> None:
        repository_before = _snapshot(EXAMPLES)

        # 1. Every task is discoverable and carries the full task contract.
        tasks = load_tasks(EXAMPLES)
        self.assertEqual(
            len(tasks), TASK_COUNT, f"expected {TASK_COUNT} tasks, got {len(tasks)}"
        )
        task_ids = [task.task_id for task in tasks]
        self.assertEqual(
            len(set(task_ids)), TASK_COUNT, f"duplicate task ids: {task_ids}"
        )
        for task in tasks:
            with self.subTest(phase="load", task=task.task_id):
                payload = json.loads(
                    (task.task_dir / "task.json").read_text(encoding="utf-8-sig")
                )
                for field in REQUIRED_FIELDS:
                    self.assertIn(field, payload, f"{task.task_id} lacks {field}")
                self.assertEqual(payload["task_id"], task.task_id)
                self.assertIn(task.task_type, TASK_TYPES)
                self.assertEqual(payload["task_type"], task.task_type)
                self.assertTrue(task.instruction.strip())
                self.assertIsInstance(payload["reward"], (int, float))
                self.assertGreater(task.reward, 0)
                self.assertIsInstance(payload["expected"], str)
                self.assertTrue(payload["expected"].strip())
                self.assertTrue(task.check)
                self.assertEqual(payload["check"], task.check)

        expected_ids = set(task_ids)

        # 2. Triage must keep every task feasible: none skipped for "no internet".
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _copy_tasks(Path(tmp) / "tasks")
            dry_run = _run_cli(
                "--tasks-dir",
                str(tasks_dir),
                "--out",
                str(Path(tmp) / "dry-out"),
                "--dry-run",
                "--offline",
                "--max-workers",
                "1",
            )

            self.assertTrue(dry_run["dry_run"])
            self.assertTrue(dry_run["offline"])
            self.assertEqual(dry_run["tasks"], TASK_COUNT)
            self.assertEqual(dry_run["planned"], TASK_COUNT)
            self.assertEqual(dry_run["infeasible"], 0)
            self.assertEqual(dry_run["skipped"], 0)
            self.assertEqual(dry_run["errors"], 0)
            self.assertEqual(dry_run["solved"], 0)
            self.assertEqual(dry_run["score"], 0.0)
            self.assertEqual(len(dry_run["plan"]), TASK_COUNT)
            self.assertEqual(
                {item["task_id"] for item in dry_run["plan"]}, expected_ids
            )
            for item in dry_run["plan"]:
                with self.subTest(phase="dry-run", task=item["task_id"]):
                    self.assertEqual(item["status"], "planned")
                    self.assertFalse(item["triage"]["infeasible"])
            self.assertEqual(list(tasks_dir.rglob("answer.txt")), [])

        # 3. The offline solve with mutation repair reproduces the measured run.
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = _copy_tasks(Path(tmp) / "tasks")
            out = Path(tmp) / "out"
            summary = _run_cli(
                "--tasks-dir",
                str(tasks_dir),
                "--out",
                str(out),
                "--offline",
                "--mutation-repair",
                "--max-workers",
                "1",
            )

            self.assertTrue(summary["offline"])
            self.assertFalse(summary["dry_run"])
            self.assertEqual(summary["tasks"], TASK_COUNT)
            self.assertEqual(summary["planned"], TASK_COUNT)
            self.assertEqual(summary["infeasible"], 0)
            self.assertEqual(summary["skipped"], 0)
            self.assertEqual(summary["errors"], 0)
            self.assertGreaterEqual(summary["solved"], EXPECTED_SOLVED)
            self.assertGreaterEqual(summary["score"], EXPECTED_SCORE)
            self.assertEqual(summary["tokens_spent"], 0.0)
            self.assertEqual(summary["prompt_tokens"], 0)
            self.assertEqual(summary["completion_tokens"], 0)
            self.assertIsNone(summary["stop_reason"])

            solutions = json.loads(
                (out / "solutions.json").read_text(encoding="utf-8")
            )
            self.assertEqual(solutions["count"], TASK_COUNT)
            statuses = {
                record["task_id"]: record["status"]
                for record in solutions["solutions"]
            }
            self.assertEqual(set(statuses), expected_ids)
            for task_id in KNOWN_FAILURES:
                with self.subTest(phase="solve-failed", task=task_id):
                    self.assertEqual(statuses[task_id], "failed")
            for task_id in sorted(expected_ids - set(KNOWN_FAILURES)):
                with self.subTest(phase="solve-solved", task=task_id):
                    self.assertEqual(statuses[task_id], "solved")

            # Solving wrote answers into the temp copy, never into the checkout.
            self.assertTrue(list(tasks_dir.rglob("answer.txt")))

        # 4. The repository tree is untouched by the whole contract test.
        self.assertEqual(_snapshot(EXAMPLES), repository_before)
        self.assertEqual(list(EXAMPLES.rglob("answer.txt")), [])


if __name__ == "__main__":
    unittest.main()
