from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import json

from harness.budget import Budget, BudgetExhausted
from harness.llm import LLMConfig, LLMError, LLMResponse, LLMUsage
from harness.runner import RunOptions, run
from harness.solvers.generic import _CONTEXT_CHAR_CAP
from harness.solvers.generic import solve as solve_generic
from harness.task import load_task
from tests.support import make_context, make_task


class FakeLLM:
    """Deterministic LLM double that replays a list of replies."""

    available = True

    def __init__(
        self,
        replies: list[str],
        *,
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
    ) -> None:
        self.replies = list(replies)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []
        self.config = LLMConfig(base_url="http://fake.invalid/v1", model="fake-model")

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        self.messages.append([dict(message) for message in messages])
        index = min(self.calls - 1, len(self.replies) - 1)
        text = self.replies[index] if self.replies else ""
        return LLMResponse(
            text=text,
            model="fake",
            usage=LLMUsage(self.prompt_tokens, self.completion_tokens),
            latency=0.0,
        )


class FailingLLM:
    available = True
    config = LLMConfig(base_url="http://fake.invalid/v1", model="fake-model")

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        raise LLMError("proxy unavailable")


class BudgetFailingLLM:
    """Simulates the gateway ledger refusing a call inside ``chat``."""

    available = True
    config = LLMConfig(base_url="http://fake.invalid/v1", model="fake-model")

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        raise BudgetExhausted("gateway ledger is closed")


def _user_prompt(fake: FakeLLM) -> str:
    return fake.messages[-1][1]["content"]


class GenericSolverTests(unittest.TestCase):
    def test_unknown_type_returns_clean_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "mystery_1",
                task_type="mystery",
                instruction="Read notes.txt and report the codeword.",
                files={"notes.txt": "The codeword is ORCHID.\n"},
                check="exact_str",
                reward=3.0,
            )
            fake = FakeLLM(["Answer: ORCHID"])
            context = make_context(llm=fake)

            result = solve_generic(task, context)

            self.assertEqual(result.answer, "ORCHID")
            self.assertEqual(result.solver, "generic")
            self.assertEqual(result.tokens, 6.0)
            self.assertEqual(context.budget.spent, 6.0)
            self.assertIn("attempts=1", result.notes)
            self.assertIn("notes.txt", _user_prompt(fake))

    def test_runner_dispatches_unknown_type_to_generic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(
                root,
                "mystery_1",
                task_type="mystery",
                instruction="Read notes.txt and report the codeword.",
                files={"notes.txt": "The codeword is ORCHID.\n"},
                check="exact_str",
                reward=3.0,
            )
            fake = FakeLLM(["ORCHID"])
            summary = run(
                RunOptions(
                    tasks_dir=root,
                    out=Path(tmp) / "out",
                    llm=fake,
                    max_workers=1,
                    time_limit=60,
                    budget=100_000,
                )
            )

            self.assertEqual(summary["solved"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["score"], 3.0)
            self.assertEqual(fake.calls, 1)
            self.assertEqual(
                (root / "mystery_1" / "answer.txt").read_text(encoding="utf-8").strip(),
                "ORCHID",
            )
            record = summary["results"][0]
            self.assertEqual(record["solver"], "generic")
            self.assertNotIn("no solver registered", record["notes"])

    def test_answer_extraction_variants(self) -> None:
        cases = {
            "json": '{"answer": "42", "reasoning": "6 * 7 = 42"}',
            "fenced": "```text\n42\n```",
            "inline_fence": "```42```",
            "last_line": "Let me compute: 6 * 7.\nAnswer: 42",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, reply in cases.items():
                with self.subTest(case=name):
                    task = make_task(
                        tmp,
                        f"extract_{name}",
                        task_type="mystery",
                        instruction="Compute 6 * 7 and reply with the integer.",
                        check="exact_int",
                    )
                    result = solve_generic(task, make_context(llm=FakeLLM([reply])))
                    self.assertEqual(result.answer, "42", result.notes)

    def test_empty_answer_triggers_exactly_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "retry",
                task_type="mystery",
                instruction="Compute 6 * 7 and reply with the integer.",
                check="exact_int",
            )
            fake = FakeLLM(["", "42"])
            context = make_context(llm=fake)

            result = solve_generic(task, context)

            self.assertEqual(fake.calls, 2)
            self.assertEqual(result.answer, "42")
            self.assertEqual(result.tokens, 12.0)
            self.assertEqual(context.budget.spent, 12.0)
            self.assertIn("attempts=2", result.notes)

    def test_malformed_int_triggers_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "malformed",
                task_type="mystery",
                instruction="Compute 6 * 7 and reply with the integer.",
                check="exact_int",
            )
            fake = FakeLLM(["about forty-two", "42"])

            result = solve_generic(task, make_context(llm=fake))

            self.assertEqual(fake.calls, 2)
            self.assertEqual(result.answer, "42")

    def test_budget_exhaustion_returns_no_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "budget",
                task_type="mystery",
                instruction="Compute 6 * 7 and reply with the integer.",
                check="exact_int",
            )
            fake = FakeLLM(["42"])
            context = make_context(budget=Budget(0.0), llm=fake)

            result = solve_generic(task, context)

            self.assertIsNone(result.answer)
            self.assertEqual(fake.calls, 0)
            self.assertEqual(result.tokens, 0.0)
            self.assertTrue(result.notes.startswith("budget stop:"), result.notes)

    def test_llm_error_returns_clean_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "llm_error",
                task_type="mystery",
                instruction="Compute 6 * 7 and reply with the integer.",
                check="exact_int",
            )
            fake = FailingLLM()

            result = solve_generic(task, make_context(llm=fake))

            self.assertIsNone(result.answer)
            self.assertEqual(fake.calls, 1)
            self.assertEqual(result.solver, "generic")
            self.assertTrue(result.notes.startswith("LLM error:"), result.notes)

    def test_budget_exhausted_inside_chat_returns_no_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "budget_in_call",
                task_type="mystery",
                instruction="Compute 6 * 7 and reply with the integer.",
                check="exact_int",
            )
            fake = BudgetFailingLLM()

            result = solve_generic(task, make_context(llm=fake))

            self.assertIsNone(result.answer)
            self.assertEqual(fake.calls, 1)
            self.assertEqual(result.solver, "generic")
            self.assertTrue(result.notes.startswith("budget stop:"), result.notes)

    def test_prompt_includes_raw_task_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "rawkeys",
                task_type="mystery",
                instruction="Return the extra field value.",
                check="exact_str",
            )
            payload = json.loads((task.task_dir / "task.json").read_text(encoding="utf-8"))
            payload["author"] = "organizers"
            payload["expected"] = "ORACLE-GOLD-VALUE"
            (task.task_dir / "task.json").write_text(json.dumps(payload), encoding="utf-8")
            task = load_task(task.task_dir)
            fake = FakeLLM(["organizers"])
            result = solve_generic(task, make_context(llm=fake))
            self.assertEqual(result.answer, "organizers")
            prompt = _user_prompt(fake)
            self.assertIn("TASK.JSON KEYS:", prompt)
            self.assertIn("author", prompt)
            self.assertNotIn("expected", prompt.lower())
            self.assertNotIn("ORACLE-GOLD-VALUE", prompt)

    def test_json_object_in_fence_is_not_truncated_to_brace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "json_obj",
                task_type="mystery",
                instruction="Return the JSON object with keys foo and bar.",
                check="exact_str",
            )
            reply = '```json\n{"foo": 1, "bar": 2}\n```'
            result = solve_generic(task, make_context(llm=FakeLLM([reply])))
            self.assertEqual(result.answer, '{"foo":1,"bar":2}', result.notes)

    def test_empty_reply_after_retry_is_not_solved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "empty",
                task_type="mystery",
                instruction="Say the codeword.",
                check="exact_str",
            )
            fake = FakeLLM(["", "   "])
            result = solve_generic(task, make_context(llm=fake))
            self.assertIsNone(result.answer)
            self.assertEqual(fake.calls, 2)

    def test_prompt_has_instruction_inventory_and_stays_under_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                f"a{index:02d}.txt": "filler " * 220 for index in range(20)
            }
            files["z_important.txt"] = "IMPORTANT-CONTENT-TOKEN\n"
            task = make_task(
                tmp,
                "prompt",
                task_type="mystery",
                instruction=(
                    "Read z_important.txt and return the token. "
                    "UNIQUE-INSTRUCTION-PHRASE."
                ),
                files=files,
                check="exact_str",
            )
            fake = FakeLLM(["IMPORTANT-CONTENT-TOKEN"])

            result = solve_generic(task, make_context(llm=fake))

            self.assertEqual(result.answer, "IMPORTANT-CONTENT-TOKEN", result.notes)
            prompt = _user_prompt(fake)
            self.assertIn("UNIQUE-INSTRUCTION-PHRASE", prompt)
            self.assertIn("WORKSPACE INVENTORY:", prompt)
            self.assertIn("z_important.txt", prompt)
            self.assertIn("IMPORTANT-CONTENT-TOKEN", prompt)
            self.assertLessEqual(len(prompt), _CONTEXT_CHAR_CAP)


if __name__ == "__main__":
    unittest.main()
