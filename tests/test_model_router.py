"""Unit tests for measured model selection (R2-07)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.llm import DEFAULT_MODEL
from harness.model_router import (
    ALLOWED_MODELS,
    DEFAULT_STATS_PATH,
    ModelSelection,
    select_model,
)
from run_agent import _effective_model_policy, build_parser

# Measured winner in the shipped stats file.  It is intentionally not the
# module default: the official CLI defaults to the base alias and fixed policy.
DEFAULT = "qwen3.6-35b-a3b-noreason"


def _write_stats(path: Path, models: dict[str, dict[str, float]], **meta: str) -> Path:
    payload = {
        "source": "unit-test synthetic stats",
        "date": "2026-11-11",
        "notes": "synthetic",
        "models": models,
        **meta,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ModelRouterTests(unittest.TestCase):
    def test_default_wins_with_supplied_stats(self) -> None:
        selection = select_model(DEFAULT, stats_path=DEFAULT_STATS_PATH)
        self.assertIsInstance(selection, ModelSelection)
        self.assertEqual(selection.model, DEFAULT)
        self.assertEqual(selection.policy, "metrics")
        self.assertIn("score/cost", selection.reason)
        self.assertEqual(selection.stats["tasks"], 16)
        self.assertEqual(selection.stats["solved"], 16)
        self.assertEqual(selection.stats["score"], 54)
        self.assertEqual(selection.stats["cost"], 3421.8)
        self.assertIn("README", selection.stats["source"])

    def test_metrics_can_pick_another_allowlisted_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_stats(
                Path(tmp) / "stats.json",
                {
                    DEFAULT: {"tasks": 16, "solved": 16, "score": 54, "cost": 4000},
                    "qwen3.8-27b": {"tasks": 16, "solved": 15, "score": 50, "cost": 100},
                },
            )
            selection = select_model(DEFAULT, stats_path=path)
            self.assertEqual(selection.model, "qwen3.8-27b")
            self.assertEqual(selection.policy, "metrics")
            self.assertIn("qwen3.8-27b", selection.reason + selection.model)
            self.assertEqual(selection.stats["source"], "unit-test synthetic stats")

    def test_unknown_model_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_stats(
                Path(tmp) / "stats.json",
                {
                    "not-an-allowed-model": {
                        "tasks": 99,
                        "solved": 99,
                        "score": 10_000,
                        "cost": 1,
                    },
                    "qwen3.8-27b": {"tasks": 16, "solved": 15, "score": 50, "cost": 100},
                },
            )
            selection = select_model(DEFAULT, stats_path=path)
            self.assertEqual(selection.model, "qwen3.8-27b")
            self.assertIn(selection.model, ALLOWED_MODELS)

    def test_only_four_allowed_models(self) -> None:
        self.assertEqual(
            set(ALLOWED_MODELS),
            {
                "qwen3.8-27b",
                "qwen3.8-27b-noreason",
                "qwen3.6-35b-a3b",
                "qwen3.6-35b-a3b-noreason",
            },
        )
        self.assertEqual(len(ALLOWED_MODELS), 4)
        selection = select_model(DEFAULT)
        self.assertIn(selection.model, ALLOWED_MODELS)

    def test_missing_file_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selection = select_model(DEFAULT, stats_path=Path(tmp) / "absent.json")
            self.assertEqual(selection.model, DEFAULT)
            self.assertEqual(selection.policy, "metrics")
            self.assertEqual(dict(selection.stats), {})
            self.assertIn("not found", selection.reason)

    def test_invalid_file_falls_back_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            path.write_text("{not json", encoding="utf-8")
            selection = select_model(DEFAULT, stats_path=path)
            self.assertEqual(selection.model, DEFAULT)
            self.assertIn("invalid stats file", selection.reason)

    def test_no_eligible_entries_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_stats(
                Path(tmp) / "stats.json",
                {
                    "unknown": {"tasks": 16, "solved": 1, "score": 1, "cost": 1},
                    "qwen3.8-27b": {"tasks": 0, "solved": 0, "score": 0, "cost": 1},
                },
            )
            selection = select_model(DEFAULT, stats_path=path)
            self.assertEqual(selection.model, DEFAULT)
            self.assertIn("no eligible measured model", selection.reason)

    def test_fixed_policy_always_returns_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_stats(
                Path(tmp) / "stats.json",
                {
                    "qwen3.8-27b": {"tasks": 16, "solved": 16, "score": 999, "cost": 1},
                },
            )
            selection = select_model(
                "qwen3.8-27b-noreason", policy="fixed", stats_path=path
            )
            self.assertEqual(selection.model, "qwen3.8-27b-noreason")
            self.assertEqual(selection.policy, "fixed")
            self.assertEqual(dict(selection.stats), {})
            self.assertIn("fixed policy", selection.reason)

    def test_unknown_policy_falls_back(self) -> None:
        selection = select_model(DEFAULT, policy="bandit")
        self.assertEqual(selection.model, DEFAULT)
        self.assertIn("unknown model policy", selection.reason)

    def test_ties_prefer_more_solved_then_lower_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_stats(
                Path(tmp) / "stats.json",
                {
                    "qwen3.8-27b": {"tasks": 16, "solved": 16, "score": 100, "cost": 100},
                    "qwen3.6-35b-a3b": {
                        "tasks": 16,
                        "solved": 15,
                        "score": 100,
                        "cost": 100,
                    },
                    "qwen3.6-35b-a3b-noreason": {
                        "tasks": 16,
                        "solved": 16,
                        "score": 100,
                        "cost": 90,
                    },
                },
            )
            selection = select_model(DEFAULT, stats_path=path)
            self.assertEqual(selection.model, "qwen3.6-35b-a3b-noreason")
            same_ratio = select_model(
                DEFAULT,
                stats_path=_write_stats(
                    Path(tmp) / "tie.json",
                    {
                        "qwen3.8-27b": {
                            "tasks": 16,
                            "solved": 16,
                            "score": 100,
                            "cost": 100,
                        },
                        "qwen3.6-35b-a3b-noreason": {
                            "tasks": 16,
                            "solved": 16,
                            "score": 90,
                            "cost": 90,
                        },
                    },
                ),
            )
            self.assertEqual(same_ratio.model, DEFAULT)


class DefaultModelCliTests(unittest.TestCase):
    def test_module_default_is_safe_base_alias(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "qwen3.6-35b-a3b")
        selection = select_model(
            DEFAULT_MODEL, policy="fixed", stats_path=DEFAULT_STATS_PATH
        )
        self.assertEqual(selection.model, "qwen3.6-35b-a3b")
        self.assertEqual(selection.policy, "fixed")
        self.assertEqual(dict(selection.stats), {})

    def test_metrics_policy_is_unchanged_for_internal_experiments(self) -> None:
        selection = select_model(
            DEFAULT_MODEL, policy="metrics", stats_path=DEFAULT_STATS_PATH
        )
        self.assertEqual(selection.model, DEFAULT)
        self.assertEqual(selection.policy, "metrics")

    def test_parser_defaults_to_gateway_and_fixed(self) -> None:
        args = build_parser().parse_args(["--tasks-dir", "tasks"])
        self.assertEqual(args.llm_backend, "gateway")
        self.assertEqual(args.model_policy, "fixed")

    def test_explicit_model_or_env_forces_fixed_policy(self) -> None:
        self.assertEqual(
            "fixed", _effective_model_policy(None, "qwen3.6-35b-a3b", "metrics")
        )
        self.assertEqual(
            "fixed", _effective_model_policy("qwen3.8-27b", None, "metrics")
        )
        self.assertEqual(
            "fixed", _effective_model_policy("qwen3.8-27b", "env", "metrics")
        )
        self.assertEqual("metrics", _effective_model_policy(None, None, "metrics"))
        self.assertEqual("fixed", _effective_model_policy(None, None, "fixed"))
        self.assertEqual("metrics", _effective_model_policy(None, "", "metrics"))


if __name__ == "__main__":
    unittest.main()
