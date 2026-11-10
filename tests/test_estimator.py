"""Unit tests for the offline cost estimator (D2, spec 001 v5.0)."""

from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness.estimator import (
    CostEstimator,
    TokenUsage,
    normalize_usage,
)
from harness.features import extract_features
from harness.task import load_tasks
from tests.support import EXAMPLES, make_task


def _prior(
    *,
    solver: str = "coding",
    task_type: str = "coding",
    model: str = "m",
    n: int = 20,
    input_tokens: float = 1000.0,
    output_tokens: float = 100.0,
    p90_cost: float | None = None,
    weak_transfer: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "solver": solver,
        "task_type": task_type,
        "model": model,
        "n": n,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "source": "test",
        "split": "train",
        "confidence": "high",
        "weak_transfer": weak_transfer,
    }
    if p90_cost is not None:
        payload["p90_cost"] = p90_cost
    return payload


def _calibration(*priors: dict[str, object], heuristic: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "version": 1,
        "heuristic": heuristic or {},
        "priors": list(priors),
    }


class FallbackAndConfidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task = make_task(
            self._tmp.name,
            "coding_0001",
            task_type="coding",
            files={"main.py": "def f():\n    return 1\n"},
        )

    def test_level1_confidence_thresholds(self) -> None:
        high = CostEstimator(_calibration(_prior(n=20))).estimate(self.task, "coding", "m")
        self.assertEqual(high.confidence, "high")
        self.assertEqual(high.drivers[0], "scope=solver+type+model")
        self.assertAlmostEqual(high.expected_cost, 1000 / 10 + 100)

        medium = CostEstimator(_calibration(_prior(n=10))).estimate(self.task, "coding", "m")
        self.assertEqual(medium.confidence, "medium")

        low = CostEstimator(_calibration(_prior(n=7))).estimate(self.task, "coding", "m")
        # Level 1 needs 8 observations, so a 7-row exact prior falls back to
        # the broader (solver, task_type) median and is marked low confidence.
        self.assertEqual(low.drivers[0], "scope=solver+type")
        self.assertEqual(low.confidence, "low")

    def test_level2_used_when_exact_model_is_missing(self) -> None:
        calibration = _calibration(_prior(model="*", n=20, input_tokens=2000, output_tokens=50))
        estimate = CostEstimator(calibration).estimate(self.task, "coding", "m")
        self.assertEqual(estimate.drivers[0], "scope=solver+type")
        self.assertEqual(estimate.confidence, "medium")
        self.assertAlmostEqual(estimate.expected_cost, 2000 / 10 + 50)

        small = CostEstimator(_calibration(_prior(model="*", n=5))).estimate(self.task, "coding", "m")
        self.assertEqual(small.drivers[0], "scope=solver+type")
        self.assertEqual(small.confidence, "low")

    def test_level2_aggregates_full_priors_when_no_wildcard_exists(self) -> None:
        calibration = _calibration(
            _prior(model="a", n=8, input_tokens=1000, output_tokens=100),
            _prior(model="b", n=8, input_tokens=3000, output_tokens=300),
        )
        estimate = CostEstimator(calibration).estimate(self.task, "coding", "missing-model")
        self.assertEqual(estimate.drivers[0], "scope=solver+type")
        self.assertAlmostEqual(estimate.expected_input_tokens, 2000.0)
        self.assertAlmostEqual(estimate.expected_output_tokens, 200.0)

    def test_level3_and_heuristic_are_low_confidence(self) -> None:
        level3 = _calibration(
            {
                "solver": "coding",
                "task_type": "*",
                "model": "m",
                "n": 40,
                "input_tokens": 500.0,
                "output_tokens": 10.0,
                "source": "test",
                "split": "train",
            }
        )
        estimate = CostEstimator(level3).estimate(self.task, "coding", "m")
        self.assertEqual(estimate.drivers[0], "scope=solver+model")
        self.assertEqual(estimate.confidence, "low")
        self.assertAlmostEqual(estimate.expected_cost, 500 / 10 + 10)

        heuristic = CostEstimator(_calibration()).estimate(self.task, "coding", "m")
        self.assertEqual(heuristic.drivers[0], "scope=heuristic")
        self.assertEqual(heuristic.confidence, "low")
        self.assertGreater(heuristic.expected_cost, 0)
        self.assertIn("chars_per_token=unmeasured", heuristic.drivers)

    def test_weak_transfer_caps_confidence(self) -> None:
        weak1 = CostEstimator(_calibration(_prior(n=100, weak_transfer=True))).estimate(self.task, "coding", "m")
        self.assertEqual(weak1.confidence, "low")
        weak2 = CostEstimator(
            _calibration(_prior(model="*", n=100, weak_transfer=True))
        ).estimate(self.task, "coding", "m")
        self.assertEqual(weak2.confidence, "low")
        strong2 = CostEstimator(_calibration(_prior(model="*", n=100))).estimate(self.task, "coding", "m")
        self.assertEqual(strong2.confidence, "medium")

    def test_unknown_type_falls_back_to_heuristic(self) -> None:
        task = make_task(self._tmp.name, "weird", task_type="puzzle", files={"a.txt": "hello"})
        estimate = CostEstimator(_calibration()).estimate(task, "generic", "m")
        self.assertEqual(estimate.drivers[0], "scope=heuristic")
        self.assertEqual(estimate.confidence, "low")


class ZeroCostDeterministicTests(unittest.TestCase):
    def test_deterministic_solvers_estimate_zero_with_high_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "math_1",
                task_type="math",
                files={"huge.py": "x = 1\n" * 5000},
            )
            estimator = CostEstimator(_calibration())
            for solver in ("math", "search", "data_analysis"):
                estimate = estimator.estimate(task, solver, "qwen3.6-35b-a3b-noreason")
                self.assertEqual(estimate.expected_cost, 0.0)
                self.assertEqual(estimate.upper_cost, 0.0)
                self.assertEqual(estimate.expected_input_tokens, 0.0)
                self.assertEqual(estimate.expected_output_tokens, 0.0)
                self.assertEqual(estimate.confidence, "high")
                self.assertTrue(estimate.drivers)
                self.assertIn("deterministic_solver=true", estimate.drivers)

    def test_examples_are_zero_for_deterministic_types(self) -> None:
        estimator = CostEstimator(_calibration())
        for task in load_tasks(EXAMPLES):
            estimate = estimator.estimate(task, task.task_type, "m")
            if task.task_type in {"math", "search", "data_analysis"}:
                self.assertEqual(estimate.expected_cost, 0.0, task.task_id)
            else:
                self.assertGreater(estimate.expected_cost, 0.0, task.task_id)


class OnlineCorrectionTests(unittest.TestCase):
    def test_observe_lowers_error_on_second_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "coding_online", task_type="coding", files={"main.py": "x = 1\n"})
            calibration = _calibration(_prior(n=20, input_tokens=1000, output_tokens=100))
            estimator = CostEstimator(calibration)

            before = estimator.estimate(task, "coding", "m")
            actual_cost = 4000 / 10 + 500
            before_error = abs(actual_cost - before.expected_cost)

            estimator.observe(task, "coding", "m", {"input_tokens": 4000, "output_tokens": 500})
            after = estimator.estimate(task, "coding", "m")
            after_error = abs(actual_cost - after.expected_cost)

            self.assertLess(after_error, before_error)
            self.assertAlmostEqual(after.expected_cost, actual_cost, places=6)
            self.assertTrue(any(driver.startswith("online_factor=") for driver in after.drivers))
            self.assertGreaterEqual(after.upper_cost, after.expected_cost)

    def test_observe_updates_estimates_for_not_started_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observed = make_task(tmp, "coding_seen", task_type="coding", files={"main.py": "x = 1\n"})
            pending = make_task(tmp, "coding_pending", task_type="coding", files={"main.py": "y = 2\n"})
            estimator = CostEstimator(_calibration(_prior(n=20)))
            before = estimator.estimate(pending, "coding", "m").expected_cost
            estimator.observe(observed, "coding", "m", {"input_tokens": 4000, "output_tokens": 500})
            after = estimator.estimate(pending, "coding", "m").expected_cost
            self.assertGreater(after, before)

    def test_observe_accepts_token_usage_and_llm_usage_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "coding_shapes", task_type="coding", files={"main.py": "x = 1\n"})
            estimator = CostEstimator(_calibration(_prior(n=20)))
            estimator.observe(task, "coding", "m", TokenUsage(input_tokens=2000, output_tokens=200))
            estimate = estimator.estimate(task, "coding", "m")
            self.assertTrue(any(d.startswith("online_samples=") for d in estimate.drivers))

            other = CostEstimator(_calibration(_prior(n=20)))
            other.observe(
                task,
                "coding",
                "m",
                SimpleNamespace(prompt_tokens=2000, completion_tokens=200),
            )
            self.assertTrue(any(d.startswith("online_samples=") for d in other.estimate(task, "coding", "m").drivers))

    def test_observe_does_not_rewrite_calibration_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "coding_file", task_type="coding", files={"main.py": "x = 1\n"})
            path = Path(tmp) / "calibration.json"
            path.write_text(json.dumps(_calibration(_prior(n=20))), encoding="utf-8")
            before_bytes = path.read_bytes()

            estimator = CostEstimator(path)
            baseline = estimator.estimate(task, "coding", "m")
            estimator.observe(task, "coding", "m", {"input_tokens": 9000, "output_tokens": 100})
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertNotEqual(estimator.estimate(task, "coding", "m").expected_cost, baseline.expected_cost)

            estimator.reset_online()
            self.assertEqual(estimator.estimate(task, "coding", "m").expected_cost, baseline.expected_cost)

    def test_invalid_or_zero_usage_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "coding_bad", task_type="coding", files={"main.py": "x = 1\n"})
            estimator = CostEstimator(_calibration(_prior(n=20)))
            baseline = estimator.estimate(task, "coding", "m")
            estimator.observe(task, "coding", "m", {"input_tokens": 0, "output_tokens": 0})
            estimator.observe(task, "coding", "m", {"input_tokens": -1, "output_tokens": 5})
            self.assertEqual(estimator.estimate(task, "coding", "m"), baseline)

    def test_deterministic_solver_observation_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "math_obs", task_type="math")
            estimator = CostEstimator(_calibration())
            estimator.observe(task, "math", "m", {"input_tokens": 100, "output_tokens": 100})
            self.assertEqual(estimator.estimate(task, "math", "m").expected_cost, 0.0)


class DeterminismAndPerformanceTests(unittest.TestCase):
    def test_same_inputs_same_output_and_under_50ms_for_100_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [
                make_task(
                    tmp,
                    f"coding_{index:03d}",
                    task_type="coding",
                    instruction=f"Fix module {index}.",
                    files={"main.py": f"def value():\n    return {index}\n" * 8},
                )
                for index in range(100)
            ]
            estimator = CostEstimator(_calibration())

            start = time.perf_counter()
            first = [estimator.estimate(task, "coding", "qwen3.6-35b-a3b-noreason") for task in tasks]
            elapsed = time.perf_counter() - start

            second = [estimator.estimate(task, "coding", "qwen3.6-35b-a3b-noreason") for task in tasks]

            self.assertLess(elapsed, 0.050, f"100 estimates took {elapsed * 1000:.1f} ms")
            self.assertEqual([item.as_dict() for item in first], [item.as_dict() for item in second])
            self.assertTrue(all(item.upper_cost >= item.expected_cost for item in first))

    def test_feature_cache_does_not_change_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "coding_cache", task_type="coding", files={"main.py": "x = 1\n"})
            cached = CostEstimator(_calibration(), cache_features=True)
            fresh = CostEstimator(_calibration(), cache_features=False)
            self.assertEqual(
                cached.estimate(task, "coding", "m"),
                fresh.estimate(task, "coding", "m"),
            )


class OfflineAndSafetyTests(unittest.TestCase):
    def test_no_socket_or_urlopen_is_used(self) -> None:
        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("estimator must not open a network connection")

        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(
                tmp,
                "coding_net",
                task_type="coding",
                instruction="Collect facts from the local markdown files.",
                files={
                    "kb/notes.md": "Alice: engineer\n",
                    "main.py": "print('x')\n",
                    "data.json": '{"a": 1}',
                },
            )
            estimator = CostEstimator(_calibration(_prior(n=20)))
            with mock.patch.object(socket, "socket", forbidden), mock.patch.object(
                socket, "create_connection", forbidden
            ), mock.patch.object(urllib.request, "urlopen", forbidden):
                estimate = estimator.estimate(task, "coding", "m")
                estimator.observe(task, "coding", "m", {"input_tokens": 1200, "output_tokens": 200})
                estimator.estimate(task, "coding", "m")
            self.assertGreater(estimate.expected_cost, 0)

    def test_missing_calibration_file_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "coding_missing", task_type="coding", files={"main.py": "x = 1\n"})
            estimator = CostEstimator(Path(tmp) / "absent.json")
            estimate = estimator.estimate(task, "coding", "m")
            self.assertEqual(estimate.confidence, "low")
            self.assertGreater(estimate.expected_cost, 0)

    def test_malformed_calibration_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            estimator = CostEstimator(path)
            task = make_task(tmp, "coding_broken", task_type="coding", files={"main.py": "x = 1\n"})
            self.assertGreater(estimator.estimate(task, "coding", "m").expected_cost, 0)


class UpperBoundTests(unittest.TestCase):
    def test_upper_cost_is_never_below_expected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "coding_upper", task_type="coding", files={"main.py": "x = 1\n"})
            calibrations = [
                _calibration(),
                _calibration(_prior(n=20, input_tokens=0.0, output_tokens=0.0)),
                _calibration(_prior(n=20, p90_cost=1.0)),
                _calibration(_prior(n=5, p90_cost=99999.0)),
                _calibration(_prior(model="*", n=20, weak_transfer=True)),
            ]
            for calibration in calibrations:
                estimator = CostEstimator(calibration)
                for solver in ("coding", "generic"):
                    estimate = estimator.estimate(task, solver, "m")
                    self.assertGreaterEqual(estimate.upper_cost, estimate.expected_cost)
                    self.assertGreaterEqual(estimate.expected_input_tokens, 0.0)
                    self.assertGreaterEqual(estimate.expected_output_tokens, 0.0)

    def test_zero_expected_has_zero_upper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task(tmp, "math_upper", task_type="math")
            estimate = CostEstimator(_calibration()).estimate(task, "math", "m")
            self.assertEqual((estimate.expected_cost, estimate.upper_cost), (0.0, 0.0))


class FeaturesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task = make_task(
            self._tmp.name,
            "coding_features",
            task_type="coding",
            instruction="Fix main.py.",
            files={
                "main.py": "def f():\n    return 1\n",
                "test_main.py": "from main import f\nassert f() == 1\n",
                "data.json": '{"k": "v"}',
                "notes.md": "a note\n",
                "nested/deep.py": "x = 1\n" * 100,
                "kb/alice.md": "Alice: engineer\n",
                "raw.bin": "\x00" * 100,
            },
        )

    def test_coding_features_only_include_files_the_solver_sends(self) -> None:
        features = extract_features(self.task, "coding", "m")
        self.assertEqual(features.task_type, "coding")
        self.assertEqual(features.file_count, 2)  # main.py + test_main.py
        self.assertGreater(features.code_chars, 0)
        self.assertEqual(features.json_chars, 0)  # coding solver does not send data.json
        self.assertEqual(features.text_chars, 0)
        self.assertTrue(features.has_tests)
        self.assertGreater(features.kb_bytes, 0)
        self.assertEqual(features.instruction_chars, len("Fix main.py."))
        self.assertEqual(
            features.total_file_bytes,
            (self.task.workdir / "main.py").stat().st_size
            + (self.task.workdir / "test_main.py").stat().st_size,
        )

    def test_deterministic_features_have_no_prompt_files(self) -> None:
        features = extract_features(self.task, "math", "m")
        self.assertEqual(features.file_count, 0)
        self.assertEqual(features.prompt_chars, features.instruction_chars)
        self.assertGreater(features.kb_bytes, 0)  # cheap feature, no content read

    def test_generic_features_include_text_and_json(self) -> None:
        features = extract_features(self.task, "generic", "m")
        self.assertGreater(features.text_chars, 0)
        self.assertGreater(features.json_chars, 0)
        self.assertGreater(features.code_chars, 0)
        self.assertEqual(features.prompt_chars, features.instruction_chars + features.text_chars
                         + features.code_chars + features.json_chars)


class UsageNormalizationTests(unittest.TestCase):
    def test_estimator_reexports_the_canonical_usage_type(self) -> None:
        from harness import usage as canonical

        self.assertIs(TokenUsage, canonical.TokenUsage)
        self.assertIs(normalize_usage, canonical.normalize_usage)

    def test_prompt_completion_aliases(self) -> None:
        usage = normalize_usage({"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual((usage.input_tokens, usage.output_tokens), (10, 5))
        self.assertEqual(usage.total_cost, 6.0)

    def test_reasoning_is_added_once_only_when_separate(self) -> None:
        separate = normalize_usage({"input_tokens": 10, "output_tokens": 0, "reasoning_tokens": 7})
        self.assertEqual(separate.output_tokens, 7)
        self.assertEqual(separate.reasoning_tokens, 7)
        self.assertEqual(separate.total_cost, 8.0)
        self.assertIn("reasoning_tokens", separate.source_fields)

        included = normalize_usage({"input_tokens": 10, "output_tokens": 7, "reasoning_tokens": 7})
        self.assertEqual(included.output_tokens, 7)
        self.assertEqual(included.reasoning_tokens, 7)
        self.assertEqual(included.total_cost, 8.0)
        self.assertIn("reasoning_tokens", included.source_fields)

    def test_canonical_normalizer_tolerates_malformed_mappings(self) -> None:
        # A truncated proxy payload normalises to zero rather than crashing the
        # run; the strict invariants live on the TokenUsage constructor.
        truncated = normalize_usage({"prompt_tokens": 10})
        self.assertEqual(
            (truncated.input_tokens, truncated.output_tokens, truncated.total_cost),
            (10, 0, 1.0),
        )
        negative = normalize_usage({"prompt_tokens": -1, "completion_tokens": 1})
        self.assertEqual((negative.input_tokens, negative.output_tokens), (0, 1))
        with self.assertRaises(ValueError):
            TokenUsage(input_tokens=1, output_tokens=-1)


if __name__ == "__main__":
    unittest.main()
