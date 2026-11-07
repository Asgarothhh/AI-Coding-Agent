from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.task import load_task, load_tasks

from tests.support import EXAMPLES


class TaskLoadingTests(unittest.TestCase):
    def test_loads_official_examples(self) -> None:
        tasks = load_tasks(EXAMPLES)
        self.assertEqual(len(tasks), 8)
        self.assertEqual(
            {task.task_id for task in tasks},
            {
                "math_0000",
                "math_0004",
                "search_0001",
                "search_0005",
                "data_analysis_0002",
                "data_analysis_0006",
                "coding_0003",
                "coding_0007",
            },
        )
        for task in tasks:
            self.assertFalse(hasattr(task, "expected"))
            self.assertEqual(task.task_dir.name, task.task_id)
            self.assertTrue(task.instruction)
            self.assertGreater(task.reward, 0)

    def test_workspace_missing_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "bare"
            task_dir.mkdir()
            (task_dir / "task.json").write_text(
                json.dumps(
                    {
                        "task_id": "bare",
                        "task_type": "math",
                        "instruction": "Compute 1 + 1.",
                        "reward": 1,
                        "check": "exact_int",
                    }
                ),
                encoding="utf-8",
            )
            task = load_task(task_dir)
            self.assertIsNone(task.workspace)
            self.assertEqual(task.workdir, task_dir)
            self.assertEqual(task.answer_locations(), (task_dir / "answer.txt",))

    def test_utf8_bom_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "bom"
            task_dir.mkdir()
            payload = {
                "task_id": "bom",
                "task_type": "search",
                "instruction": "Find the value in kb/.",
                "reward": 2,
                "check": "exact_str",
            }
            (task_dir / "task.json").write_bytes(
                b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8")
            )
            task = load_task(task_dir)
            self.assertEqual(task.task_id, "bom")
            self.assertEqual(task.reward, 2.0)

    def test_direct_task_directory_is_supported(self) -> None:
        task = load_task(EXAMPLES / "math_0000")
        self.assertEqual(task.task_id, "math_0000")
        self.assertEqual(len(load_tasks(EXAMPLES / "math_0000")), 1)

    def test_invalid_task_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good"
            good.mkdir()
            (good / "task.json").write_text(
                json.dumps(
                    {
                        "task_id": "good",
                        "task_type": "math",
                        "instruction": "Compute 1 + 1.",
                        "reward": 1,
                        "check": "exact_int",
                    }
                ),
                encoding="utf-8",
            )
            (root / "not_a_task").mkdir()
            broken = root / "broken"
            broken.mkdir()
            (broken / "task.json").write_text("{not json", encoding="utf-8")
            tasks = load_tasks(root)
            self.assertEqual([task.task_id for task in tasks], ["good"])

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_tasks("/definitely/not/a/real/path/openjiuwen")


if __name__ == "__main__":
    unittest.main()
