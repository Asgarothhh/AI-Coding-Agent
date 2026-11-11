from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from harness.llm import LLMResponse, LLMUsage
from harness.solvers.coding import (
    _fallback_patch,
    _restore_tests,
    _run_tests,
    _snapshot_tests,
    _verify_tests,
    solve as solve_coding,
)
from harness.task import load_tasks

from tests.support import EXAMPLES, make_context, make_task


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


_BUGGY_SOURCE = "def add(a, b):\n    return a - b\n"
_FIXED_SOURCE = "def add(a, b):\n    return a + b\n"
_TEST_SOURCE = (
    "import unittest\n\n"
    "from mylib import add\n\n\n"
    "class TestAdd(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n\n\n"
    'if __name__ == "__main__":\n'
    "    unittest.main()\n"
)


def _single_source_task(root: Path, task_id: str = "fallback_one"):
    return make_task(
        root,
        task_id,
        task_type="coding",
        instruction="Fix mylib.py so the tests pass.",
        files={"mylib.py": _BUGGY_SOURCE, "test_mylib.py": _TEST_SOURCE},
        check="unittest",
    )


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
            source_before = (task.workdir / "mylib.py").read_bytes()

            fake = FakeLLM(json.dumps({"files": {"test_mylib.py": "# hacked\n"}}))
            result = solve_coding(task, make_context(llm=fake, max_iterations=1))

            self.assertIsNone(result.answer)
            self.assertEqual(test_file.read_bytes(), before_bytes)
            self.assertEqual((task.workdir / "mylib.py").read_bytes(), source_before)
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

    def test_fallback_applies_single_fenced_block_for_one_source_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = _single_source_task(Path(tmp))
            test_file = task.workdir / "test_mylib.py"
            before = _hash(test_file)
            reply = "Here is the corrected file:\n\n```python\n" + _FIXED_SOURCE + "```\n"

            fake = FakeLLM(reply)
            result = solve_coding(task, make_context(llm=fake, max_iterations=2))

            self.assertEqual(result.answer, "DONE", result.notes)
            self.assertEqual(
                (task.workdir / "mylib.py").read_text(encoding="utf-8"),
                _FIXED_SOURCE,
            )
            self.assertEqual(_hash(test_file), before)
            self.assertEqual(fake.calls, 1)
            self.assertEqual(result.tokens, 6.0)

    def test_fallback_applies_bare_body_for_one_source_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = _single_source_task(Path(tmp))
            fake = FakeLLM(_FIXED_SOURCE)

            result = solve_coding(task, make_context(llm=fake, max_iterations=1))

            self.assertEqual(result.answer, "DONE", result.notes)
            self.assertEqual(
                (task.workdir / "mylib.py").read_text(encoding="utf-8"),
                _FIXED_SOURCE,
            )

    def test_fallback_refuses_multiple_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                Path(tmp),
                "fallback_multi",
                task_type="coding",
                instruction="Fix alpha.py and beta.py.",
                files={
                    "alpha.py": _BUGGY_SOURCE,
                    "beta.py": "def mul(a, b):\n    return a / b\n",
                    "test_both.py": (
                        "import unittest\n\n"
                        "from alpha import add\n"
                        "from beta import mul\n\n\n"
                        "class TestBoth(unittest.TestCase):\n"
                        "    def test_add(self):\n"
                        "        self.assertEqual(add(2, 3), 5)\n\n"
                        "    def test_mul(self):\n"
                        "        self.assertEqual(mul(2, 3), 6)\n\n\n"
                        'if __name__ == "__main__":\n'
                        "    unittest.main()\n"
                    ),
                },
                check="unittest",
            )
            alpha_before = (task.workdir / "alpha.py").read_bytes()
            beta_before = (task.workdir / "beta.py").read_bytes()
            test_before = (task.workdir / "test_both.py").read_bytes()
            reply = "```python\n" + _FIXED_SOURCE + "```\n"

            result = solve_coding(
                task,
                make_context(llm=FakeLLM(reply), max_iterations=1),
            )

            self.assertIsNone(result.answer)
            self.assertEqual(result.notes, "LLM did not return a usable full-file patch")
            self.assertEqual((task.workdir / "alpha.py").read_bytes(), alpha_before)
            self.assertEqual((task.workdir / "beta.py").read_bytes(), beta_before)
            self.assertEqual((task.workdir / "test_both.py").read_bytes(), test_before)

    def test_fallback_rejects_test_file_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = _single_source_task(Path(tmp))
            source_before = (task.workdir / "mylib.py").read_bytes()
            test_before = (task.workdir / "test_mylib.py").read_bytes()
            reply = "### FILE: test_mylib.py\n```python\n# hacked\n```\n"

            self.assertIsNone(_fallback_patch(reply, task.workdir))
            result = solve_coding(
                task,
                make_context(llm=FakeLLM(reply), max_iterations=1),
            )

            self.assertIsNone(result.answer)
            self.assertEqual(result.notes, "LLM did not return a usable full-file patch")
            self.assertEqual((task.workdir / "mylib.py").read_bytes(), source_before)
            self.assertEqual((task.workdir / "test_mylib.py").read_bytes(), test_before)

            test_only = Path(tmp) / "test_only"
            test_only.mkdir()
            (test_only / "test_solo.py").write_text(_TEST_SOURCE, encoding="utf-8")
            self.assertIsNone(
                _fallback_patch("```python\nx = 1\n```\n", test_only)
            )

    def test_fallback_applies_one_block_despite_unrelated_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = _single_source_task(Path(tmp))
            reply = (
                "I could not produce JSON, so here is the whole file.\n\n"
                "```\n" + _FIXED_SOURCE + "```\n"
            )

            result = solve_coding(
                task,
                make_context(llm=FakeLLM(reply), max_iterations=1),
            )

            self.assertEqual(result.answer, "DONE", result.notes)
            self.assertEqual(
                (task.workdir / "mylib.py").read_text(encoding="utf-8"),
                _FIXED_SOURCE,
            )


if __name__ == "__main__":
    unittest.main()
