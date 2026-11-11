from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.estimator import CostEstimate
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

    def test_ensure_placeholder_creates_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "ph", workspace=False)
            store = AnswerStore(Path(tmp) / "out")
            paths = store.ensure_placeholder(task)
            self.assertEqual(paths, (task.task_dir / "answer.txt",))
            self.assertEqual(paths[0].read_text(encoding="utf-8"), "")
            store.ensure_placeholder(task)
            self.assertEqual(paths[0].read_text(encoding="utf-8"), "")

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


class PredictionTests(unittest.TestCase):
    def _estimate(self, *, drivers: tuple[str, ...] = ("scope=heuristic",)) -> CostEstimate:
        return CostEstimate(
            expected_cost=1.5,
            upper_cost=3.0,
            expected_input_tokens=10.0,
            expected_output_tokens=0.5,
            confidence="high",
            model="qwen3.6-35b-a3b-noreason",
            solver="math",
            drivers=drivers,
        )

    def test_predictions_path_lives_next_to_solutions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AnswerStore(Path(tmp) / "out")
            self.assertEqual(
                store.predictions_path, Path(tmp) / "out" / "predictions.jsonl"
            )

    def test_record_prediction_appends_one_jsonl_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "t1")
            store = AnswerStore(Path(tmp) / "out")
            estimate = self._estimate()
            store.record_prediction(task, estimate, "qwen3.6-35b-a3b-noreason", "math")
            store.record_prediction(task, estimate, "qwen3.6-35b-a3b-noreason", "math")

            lines = store.predictions_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            record = json.loads(lines[0])
            self.assertEqual(
                set(record),
                {
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
                },
            )
            self.assertEqual(record["task_id"], "t1")
            self.assertEqual(record["task_type"], "math")
            self.assertEqual(record["solver"], "math")
            self.assertEqual(record["model"], "qwen3.6-35b-a3b-noreason")
            self.assertEqual(record["expected_cost"], 1.5)
            self.assertEqual(record["upper_cost"], 3.0)
            self.assertEqual(record["confidence"], "high")
            self.assertEqual(record["drivers"], ["scope=heuristic"])
            self.assertEqual(record["theory"], "heuristic")
            self.assertIn("+", record["ts"])

    def test_theory_names_estimator_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "t1")
            store = AnswerStore(Path(tmp) / "out")
            cases = {
                ("deterministic_solver=true",): "deterministic",
                ("scope=heuristic",): "heuristic",
                ("scope=prior", "online_samples=3"): "calibration+online",
                ("scope=prior",): "calibration",
            }
            for drivers, theory in cases.items():
                with self.subTest(theory=theory):
                    store.record_prediction(task, self._estimate(drivers=drivers), "m", "s")
            theories = [
                json.loads(line)["theory"]
                for line in store.predictions_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(theories, list(cases.values()))

    def test_prediction_does_not_touch_answer_txt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "t1")
            store = AnswerStore(Path(tmp) / "out")
            store.write_answer(task, "42")
            before = (task.task_dir / "answer.txt").read_text(encoding="utf-8")
            store.record_prediction(task, self._estimate(), "m", "math")
            after = (task.task_dir / "answer.txt").read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertFalse((task.task_dir / "predictions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
