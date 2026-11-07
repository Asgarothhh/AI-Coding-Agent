from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.solvers.base import SolveResult
from harness.store import AnswerStore, atomic_write_text, normalize_answer
from harness.trace import TraceWriter

from tests.support import make_task


class StoreTests(unittest.TestCase):
    def test_write_answer_to_task_root_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "t1", files={"note.txt": "hi"})
            store = AnswerStore(Path(tmp) / "out")
            paths = store.write_answer(task, "42")
            self.assertEqual(len(paths), 2)
            self.assertEqual((task.task_dir / "answer.txt").read_text(encoding="utf-8"), "42\n")
            assert task.workspace is not None
            self.assertEqual((task.workspace / "answer.txt").read_text(encoding="utf-8"), "42\n")
            leftovers = [p.name for p in task.task_dir.iterdir() if p.name.startswith(".answer")]
            self.assertEqual(leftovers, [])

    def test_write_answer_without_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "bare", workspace=False)
            store = AnswerStore(Path(tmp) / "out")
            paths = store.write_answer(task, "7")
            self.assertEqual(paths, (task.task_dir / "answer.txt",))
            self.assertEqual(paths[0].read_text(encoding="utf-8"), "7\n")

    def test_normalize_answer_is_single_line(self) -> None:
        self.assertEqual(normalize_answer("  Osaka  "), "Osaka")
        self.assertEqual(normalize_answer("\n\n42\nignored\n"), "42")
        with self.assertRaises(ValueError):
            normalize_answer("   ")
        with self.assertRaises(ValueError):
            normalize_answer(None)

    def test_solutions_json_is_flushed_per_solution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "t1")
            store = AnswerStore(Path(tmp) / "out")
            result = SolveResult("42", 0.0, "math", "ok")
            store.record_solution(task, result, "solved", 0.05)
            payload = json.loads(store.solutions_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["count"], 1)
            record = payload["solutions"][0]
            self.assertEqual(record["task_id"], "t1")
            self.assertEqual(record["status"], "solved")
            self.assertEqual(record["answer"], "42")
            self.assertEqual(record["solver"], "math")

            store.record_solution(task, result, "solved", 0.06)
            payload = json.loads(store.solutions_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["count"], 2)

    def test_out_can_be_a_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "custom" / "solutions.json"
            store = AnswerStore(target)
            self.assertEqual(store.solutions_path, target)
            task = make_task(tmp, "t1")
            store.record_solution(task, SolveResult("1", 0.0, "math", ""), "solved", 0.0)
            self.assertTrue(target.is_file())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["count"], 1)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            atomic_write_text(target, "hello")
            atomic_write_text(target, "world")
            self.assertEqual(target.read_text(encoding="utf-8"), "world")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["file.txt"])

    def test_trace_writer_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            writer = TraceWriter(path)
            writer.write(
                task_id="t1",
                task_type="math",
                reward=5,
                status="solved",
                solver="math",
                tokens=0.0,
                duration=0.01,
                error=None,
                notes="ok",
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            for key in ("task_id", "type", "reward", "status", "solver", "tokens", "duration", "error"):
                self.assertIn(key, record)
            self.assertEqual(record["task_id"], "t1")


if __name__ == "__main__":
    unittest.main()
