from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from harness.llm import LLMResponse, LLMUsage
from harness.solvers.coding import (
    _restore_tests,
    _run_tests,
    _snapshot_tests,
    _verify_tests,
    solve as solve_coding,
)
from harness.task import load_tasks

from tests.support import EXAMPLES, make_context


class FakeLLM:
    available = True

    def __init__(self, text: str, *, prompt_tokens: int = 10, completion_tokens: int = 5) -> None:
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0

    def chat(self, messages, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return LLMResponse(
            text=self.text,
            model="fake",
            usage=LLMUsage(self.prompt_tokens, self.completion_tokens),
            latency=0.0,
        )


def _copy_task(task_id: str, destination: Path) -> Path:
    import shutil

    source = EXAMPLES / task_id
    target = destination / task_id
    shutil.copytree(source, target)
    answer = target / "answer.txt"
    if answer.exists():
        answer.unlink()
    return target


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CodingSolverTests(unittest.TestCase):
    def test_offline_coding_fails_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _copy_task("coding_0003", Path(tmp))
            task = load_tasks(Path(tmp))[0]
            test_file = task.workdir / "test_mylib.py"
            before = _hash(test_file)
            result = solve_coding(task, make_context(llm=None))
            self.assertIsNone(result.answer)
            self.assertEqual(result.tokens, 0.0)
            self.assertEqual(_hash(test_file), before)
            self.assertFalse((task.task_dir / "answer.txt").exists())

    def test_mutation_repair_solves_both_examples_with_zero_tokens(self) -> None:
        for task_id in ("coding_0003", "coding_0007"):
            with self.subTest(task=task_id), tempfile.TemporaryDirectory() as tmp:
                _copy_task(task_id, Path(tmp))
                task = load_tasks(Path(tmp))[0]
                test_file = task.workdir / "test_mylib.py"
                before = _hash(test_file)
                context = make_context(llm=None, mutation_repair=True, mutation_time_budget=20)
                result = solve_coding(task, context)
                self.assertEqual(result.answer, "DONE", result.notes)
                self.assertEqual(result.tokens, 0.0)
                self.assertEqual(context.budget.spent, 0.0)
                self.assertEqual(_hash(test_file), before)
                run = _run_tests(task.workdir, 30)
                self.assertTrue(run.passed, run.output)

    def test_llm_full_file_patch_is_applied_and_charged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = _copy_task("coding_0003", Path(tmp))
            task = load_tasks(Path(tmp))[0]
            library = task.workdir / "mylib.py"
            original = library.read_text(encoding="utf-8")
            fixed = original.replace(
                "' '.join(sorted(s.split()))",
                "' '.join(reversed(s.split()))",
            ).replace("s -= x", "s += x")
            self.assertNotEqual(fixed, original)
            test_file = task.workdir / "test_mylib.py"
            before = _hash(test_file)

            fake = FakeLLM(json.dumps({"files": {"mylib.py": fixed}}))
            context = make_context(llm=fake, max_iterations=2)
            result = solve_coding(task, context)

            self.assertEqual(result.answer, "DONE", result.notes)
            self.assertEqual(fake.calls, 1)
            self.assertEqual(result.tokens, 6.0)
            self.assertEqual(context.budget.spent, 6.0)
            self.assertEqual(_hash(test_file), before)
            self.assertTrue(_run_tests(task.workdir, 30).passed)

    def test_llm_cannot_modify_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _copy_task("coding_0003", Path(tmp))
            task = load_tasks(Path(tmp))[0]
            test_file = task.workdir / "test_mylib.py"
            before_bytes = test_file.read_bytes()

            fake = FakeLLM(json.dumps({"files": {"test_mylib.py": "# hacked\n"}}))
            result = solve_coding(task, make_context(llm=fake, max_iterations=1))

            self.assertIsNone(result.answer)
            self.assertEqual(test_file.read_bytes(), before_bytes)
            self.assertFalse((task.task_dir / "answer.txt").exists())

    def test_test_snapshot_detects_and_restores_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _copy_task("coding_0007", Path(tmp))
            task = load_tasks(Path(tmp))[0]
            test_file = task.workdir / "test_mylib.py"

            snapshot = _snapshot_tests(task.workdir)
            self.assertEqual(len(snapshot.files), 1)
            original = test_file.read_bytes()

            test_file.write_text("# hacked\n", encoding="utf-8")
            violations = _verify_tests(snapshot)
            self.assertEqual(len(violations), 1)
            self.assertIn("modified", violations[0])

            _restore_tests(snapshot)
            self.assertEqual(test_file.read_bytes(), original)
            self.assertEqual(_verify_tests(snapshot), [])

    def test_no_tests_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import json as json_module

            from harness.task import load_task

            task_dir = Path(tmp) / "no_tests"
            task_dir.mkdir()
            (task_dir / "task.json").write_text(
                json_module.dumps(
                    {
                        "task_id": "no_tests",
                        "task_type": "coding",
                        "instruction": "Fix mylib.py.",
                        "reward": 1,
                        "check": "unittest",
                    }
                ),
                encoding="utf-8",
            )
            task = load_task(task_dir)
            result = solve_coding(task, make_context(llm=None))
            self.assertIsNone(result.answer)
            self.assertIn("no test files", result.notes)


if __name__ == "__main__":
    unittest.main()
