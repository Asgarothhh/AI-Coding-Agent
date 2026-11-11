from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.task import load_task, load_tasks, resolve_task_path

from tests.support import EXAMPLES


def _write_task(task_dir: Path, payload: dict[str, object]) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(json.dumps(payload), encoding="utf-8")


class TaskFormatAdapterTests(unittest.TestCase):
    def test_official_examples_preserve_raw_including_expected(self) -> None:
        for task in load_tasks(EXAMPLES):
            with self.subTest(task=task.task_id):
                self.assertIn("expected", task.raw)
                self.assertEqual(task.raw["task_id"], task.task_id)
                self.assertEqual(task.raw["reward"], task.reward)
                self.assertFalse(hasattr(task, "expected"))
                self.assertEqual(task.answer_locations()[0], task.task_dir / "answer.txt")

    def test_unknown_fields_round_trip_in_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "extra"
            payload = {
                "task_id": "extra",
                "task_type": "math",
                "instruction": "Compute 1 + 1.",
                "reward": 2,
                "check": "exact_int",
                "expected": "2",
                "foo": {"nested": True},
                "author": "organizers",
            }
            _write_task(task_dir, payload)
            task = load_task(task_dir)
            self.assertEqual(task.raw["foo"], {"nested": True})
            self.assertEqual(task.raw["author"], "organizers")
            self.assertEqual(task.raw["expected"], "2")
            self.assertEqual(task.reward, 2.0)

    def test_unknown_task_type_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "mystery"
            _write_task(
                task_dir,
                {
                    "task_id": "mystery",
                    "task_type": "table_qa",
                    "instruction": "Answer the table question.",
                    "reward": 1,
                    "check": "exact_str",
                    "extra_column": "kept",
                },
            )
            task = load_task(task_dir)
            self.assertEqual(task.task_type, "table_qa")
            self.assertEqual(task.raw["extra_column"], "kept")

    def test_empty_instruction_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "empty"
            _write_task(
                task_dir,
                {
                    "task_id": "empty",
                    "task_type": "math",
                    "instruction": "",
                    "reward": 1,
                    "check": "exact_int",
                },
            )
            task = load_task(task_dir)
            self.assertEqual(task.instruction, "")

    def test_missing_required_field_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_task(
                root / "good",
                {
                    "task_id": "good",
                    "task_type": "math",
                    "instruction": "1+1",
                    "reward": 1,
                    "check": "exact_int",
                },
            )
            _write_task(
                root / "bad",
                {
                    "task_id": "bad",
                    "task_type": "math",
                    "instruction": "1+1",
                    "check": "exact_int",
                },
            )
            tasks = load_tasks(root)
            self.assertEqual([task.task_id for task in tasks], ["good"])

    def test_non_numeric_reward_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, reward in (("str", "high"), ("bool", True), ("numeric_str", "5")):
                _write_task(
                    root / name,
                    {
                        "task_id": name,
                        "task_type": "math",
                        "instruction": "1+1",
                        "reward": reward,
                        "check": "exact_int",
                    },
                )
            self.assertEqual(load_tasks(root), [])

    def test_invalid_json_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken"
            broken.mkdir()
            (broken / "task.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(load_tasks(Path(tmp)), [])

    def test_duplicate_task_id_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {
                "task_id": "dup",
                "task_type": "math",
                "instruction": "1+1",
                "reward": 1,
                "check": "exact_int",
            }
            _write_task(root / "a_dup", payload)
            _write_task(root / "b_dup", payload)
            tasks = load_tasks(root)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].task_id, "dup")

    def test_resolve_rejects_parent_and_absolute_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir()
            (task_dir / "workspace").mkdir()
            (task_dir / "workspace" / "data.csv").write_text("a,b\n", encoding="utf-8")
            inside = resolve_task_path(task_dir, "workspace/data.csv")
            self.assertEqual(inside.name, "data.csv")
            with self.assertRaises(ValueError):
                resolve_task_path(task_dir, "../secret.txt")
            with self.assertRaises(ValueError):
                resolve_task_path(task_dir, "/etc/passwd")

    def test_resolve_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("leak", encoding="utf-8")
            link = task_dir / "escape.txt"
            os.symlink(outside, link)
            with self.assertRaises(ValueError):
                resolve_task_path(task_dir, "escape.txt")

    def test_symlink_workspace_escape_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir()
            outside = Path(tmp) / "outside_ws"
            outside.mkdir()
            os.symlink(outside, task_dir / "workspace")
            _write_task(
                task_dir,
                {
                    "task_id": "sym",
                    "task_type": "math",
                    "instruction": "Compute 1.",
                    "reward": 1,
                    "check": "exact_int",
                },
            )
            task = load_task(task_dir)
            self.assertIsNone(task.workspace)
            self.assertEqual(task.workdir, task.task_dir)


if __name__ == "__main__":
    unittest.main()
