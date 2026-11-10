"""Unit tests for ``harness.usage.normalize_usage``.

The proxy's exact usage schema is not guaranteed, so these cases pin down every
shape observed in OpenAI-compatible responses.  Reasoning must never be added
to output twice.
"""

from __future__ import annotations

import unittest

from harness.usage import TokenUsage, normalize_usage


class OpenAIShapeTests(unittest.TestCase):
    def test_prompt_and_completion_tokens(self) -> None:
        usage = normalize_usage({"prompt_tokens": 100, "completion_tokens": 50})
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 50)
        self.assertEqual(usage.reasoning_tokens, 0)
        self.assertEqual(usage.total_cost, 60.0)
        self.assertAlmostEqual(usage.cost, 60.0)
        self.assertEqual(usage.source_fields, ("prompt_tokens", "completion_tokens"))

    def test_input_and_output_alias(self) -> None:
        usage = normalize_usage({"input_tokens": 40, "output_tokens": 10})
        self.assertEqual(usage.input_tokens, 40)
        self.assertEqual(usage.output_tokens, 10)
        self.assertEqual(usage.total_cost, 14.0)
        self.assertEqual(usage.source_fields, ("input_tokens", "output_tokens"))

    def test_input_alias_is_preferred_independently_of_output_alias(self) -> None:
        usage = normalize_usage(
            {"input_tokens": 10, "prompt_tokens": 999, "completion_tokens": 5}
        )
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.output_tokens, 5)
        self.assertEqual(usage.source_fields, ("input_tokens", "completion_tokens"))


class ReasoningTests(unittest.TestCase):
    def test_nested_completion_details_reasoning_is_not_added_twice(self) -> None:
        # 100 completion tokens already include the 40 reasoning tokens.
        usage = normalize_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 100,
                "completion_tokens_details": {"reasoning_tokens": 40},
            }
        )
        self.assertEqual(usage.output_tokens, 100)
        self.assertEqual(usage.reasoning_tokens, 40)
        self.assertEqual(usage.total_cost, 101.0)
        self.assertIn(
            "completion_tokens_details.reasoning_tokens", usage.source_fields
        )

    def test_nested_output_details_reasoning(self) -> None:
        usage = normalize_usage(
            {
                "input_tokens": 10,
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 7},
            }
        )
        self.assertEqual(usage.output_tokens, 30)
        self.assertEqual(usage.reasoning_tokens, 7)
        self.assertEqual(usage.total_cost, 31.0)
        self.assertIn("output_tokens_details.reasoning_tokens", usage.source_fields)

    def test_top_level_reasoning_field(self) -> None:
        usage = normalize_usage(
            {"prompt_tokens": 0, "completion_tokens": 20, "reasoning_tokens": 9}
        )
        self.assertEqual(usage.output_tokens, 20)
        self.assertEqual(usage.reasoning_tokens, 9)
        self.assertEqual(usage.total_cost, 20.0)
        self.assertEqual(usage.source_fields, ("prompt_tokens", "completion_tokens", "reasoning_tokens"))

    def test_zero_output_promotes_reasoning_once(self) -> None:
        usage = normalize_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 0,
                "completion_tokens_details": {"reasoning_tokens": 25},
            }
        )
        self.assertEqual(usage.output_tokens, 25)
        self.assertEqual(usage.reasoning_tokens, 25)
        self.assertEqual(usage.total_cost, 26.0)

    def test_reasoning_without_any_output_field(self) -> None:
        usage = normalize_usage(
            {"prompt_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 5}}
        )
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.output_tokens, 5)
        self.assertEqual(usage.reasoning_tokens, 5)
        self.assertEqual(usage.total_cost, 6.0)

    def test_reasoning_never_added_when_output_is_smaller(self) -> None:
        # A proxy that reports both counts inconsistently: output stays
        # authoritative, reasoning is diagnostic only.
        usage = normalize_usage(
            {"prompt_tokens": 0, "completion_tokens": 4, "reasoning_tokens": 10}
        )
        self.assertEqual(usage.output_tokens, 4)
        self.assertEqual(usage.reasoning_tokens, 10)
        self.assertEqual(usage.total_cost, 4.0)


class MissingAndMalformedTests(unittest.TestCase):
    def test_missing_usage_normalizes_to_zero(self) -> None:
        for raw in (None, {}, {"usage": None}):
            with self.subTest(raw=raw):
                usage = normalize_usage(raw)  # type: ignore[arg-type]
                self.assertEqual(usage, TokenUsage())
                self.assertEqual(usage.total_cost, 0.0)
                self.assertEqual(usage.source_fields, ())

    def test_wrapped_usage_mapping(self) -> None:
        usage = normalize_usage(
            {"id": "x", "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        )
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.output_tokens, 2)
        self.assertEqual(usage.total_cost, 3.0)

    def test_invalid_values_are_ignored(self) -> None:
        usage = normalize_usage(
            {
                "prompt_tokens": "not-a-number",
                "completion_tokens": -5,
                "reasoning_tokens": None,
            }
        )
        self.assertEqual(usage.input_tokens, 0)
        self.assertEqual(usage.output_tokens, 0)
        self.assertEqual(usage.reasoning_tokens, 0)
        self.assertEqual(usage.total_cost, 0.0)

    def test_numeric_strings_are_accepted(self) -> None:
        usage = normalize_usage(
            {"prompt_tokens": "20", "completion_tokens": "3"}
        )
        self.assertEqual(usage.input_tokens, 20)
        self.assertEqual(usage.output_tokens, 3)
        self.assertEqual(usage.total_cost, 5.0)

    def test_zero_usage_deterministic_solver(self) -> None:
        usage = normalize_usage({"prompt_tokens": 0, "completion_tokens": 0})
        self.assertEqual(usage.total_cost, 0.0)
        self.assertEqual(usage.source_fields, ("prompt_tokens", "completion_tokens"))


if __name__ == "__main__":
    unittest.main()
