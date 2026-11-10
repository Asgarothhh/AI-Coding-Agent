"""Deterministic, offline cost estimator (spec 001 v5.0, sections 4 and 6).

The estimator answers one question for the planner and the budget policy:
*how many challenge cost units will this task consume, and how much should be
reserved for it?*  It never calls a model, never touches the network and never
rewrites ``calibration.json``.

Fallback order (spec 6.2):

1. median for ``(solver, task_type, model)``;
2. median for ``(solver, task_type)``;
3. global median for ``(solver, model)`` / ``(solver,)`` / all data;
4. conservative heuristic from prompt characters and expected completion.

Confidence follows the number of comparable observations (``high`` >= 20,
``medium`` 8-19, ``low`` < 8).  A fallback that drops the model or the task
type, and every heuristic estimate, is capped at ``low``.

``observe()`` keeps an in-memory multiplicative correction per
``(model, solver, task_type)`` (and progressively broader keys).  It is
deliberately *not* persisted: a competition run must stay reproducible from
the calibration file (spec 6.5).

Cost unit: ``input_tokens / 10 + output_tokens`` (spec 1.2).  Reasoning is
already included in ``output_tokens`` when the proxy counts it that way; it is
added once only when the proxy reports it separately and output is zero.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import statistics
import threading
from collections import deque
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from .features import DETERMINISTIC_SOLVERS, Features, extract_features
from .task import Task

# The canonical usage record lives in ``harness.usage``.  These names are
# re-exported for backward compatibility with callers that imported them from
# the estimator (and with ``CostEstimator.observe``).
from .usage import TokenUsage, normalize_usage

__all__ = [
    "ALLOWED_MODELS",
    "DEFAULT_CALIBRATION_PATH",
    "DEFAULT_HEURISTIC",
    "CostEstimate",
    "CostEstimator",
    "TokenUsage",
    "load_calibration",
    "normalize_usage",
]

log = logging.getLogger(__name__)

Confidence = Literal["low", "medium", "high"]

DEFAULT_CALIBRATION_PATH: Final = Path(__file__).with_name("calibration.json")

#: The only model aliases allowed by the competition rules.
ALLOWED_MODELS: Final = (
    "qwen3.8-27b",
    "qwen3.8-27b-noreason",
    "qwen3.6-35b-a3b",
    "qwen3.6-35b-a3b-noreason",
)

MIN_OBSERVATIONS: Final = 8
HIGH_OBSERVATIONS: Final = 20
_MAX_ONLINE_SAMPLES: Final = 64
_MIN_CORRECTION: Final = 0.2
_MAX_CORRECTION: Final = 5.0

#: Built-in defaults used when ``calibration.json`` is absent.  The
#: ``chars_per_token`` values are conservative, *unmeasured* defaults: Cyrillic
#: text tokenizes worse than English, so ``text`` uses 2.6 chars/token rather
#: than 4.  They must not be presented as a <= 5% guarantee (spec 6.3).
DEFAULT_HEURISTIC: Final[dict[str, Any]] = {
    "chars_per_token": {"text": 2.6, "code": 3.0, "json": 2.4},
    "prompt_overhead_chars": {
        "default": 512,
        "coding": 768,
        "generic": 768,
        "search": 256,
        "math": 256,
        "data_analysis": 256,
    },
    "completion_tokens": {
        "default": 400,
        "math": 24,
        "search": 160,
        "data_analysis": 96,
        "coding": 384,
        "generic": 400,
    },
    "steps": {
        "default": 1,
        "coding": 2,
        "generic": 1,
        "math": 1,
        "search": 1,
        "data_analysis": 1,
    },
    "upper_multiplier": {"low": 2.5, "medium": 1.8, "high": 1.4},
}

_DEFAULT_CALIBRATION: Final[dict[str, Any]] = {
    "version": 1,
    "source": "built-in defaults (no calibration file)",
    "split": "n/a",
    "heuristic": DEFAULT_HEURISTIC,
    "priors": [],
}


# --------------------------------------------------------------------------- #
# Public data contracts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Cost forecast for one task (spec 4)."""

    expected_cost: float
    upper_cost: float
    expected_input_tokens: float
    expected_output_tokens: float
    confidence: Confidence
    model: str
    solver: str
    drivers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_cost": self.expected_cost,
            "upper_cost": self.upper_cost,
            "expected_input_tokens": self.expected_input_tokens,
            "expected_output_tokens": self.expected_output_tokens,
            "confidence": self.confidence,
            "model": self.model,
            "solver": self.solver,
            "drivers": list(self.drivers),
        }


# --------------------------------------------------------------------------- #
# Internal calibration helpers
# --------------------------------------------------------------------------- #
def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _name_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "*":
        return None
    return text


def _confidence_for(n: int) -> Confidence:
    if n >= HIGH_OBSERVATIONS:
        return "high"
    if n >= MIN_OBSERVATIONS:
        return "medium"
    return "low"


def _numeric_map(value: Any, defaults: MappingABC[str, float], *, allow_zero: bool) -> dict[str, float]:
    result = {str(key): float(number) for key, number in defaults.items()}
    if not isinstance(value, MappingABC):
        return result
    for key, raw in value.items():
        number = _number(raw)
        if number is None:
            continue
        if number < 0 or (number == 0 and not allow_zero):
            continue
        result[str(key)] = number
    return result


@dataclass(frozen=True, slots=True)
class _Heuristic:
    chars_per_token: MappingABC[str, float]
    overhead_chars: MappingABC[str, float]
    completion_tokens: MappingABC[str, float]
    steps: MappingABC[str, float]
    upper_multiplier: MappingABC[str, float]

    def chars_for(self, kind: str) -> float:
        return float(self.chars_per_token.get(kind, self.chars_per_token.get("text", 2.6)))

    def overhead_for(self, solver: str, task_type: str) -> float:
        for key in (solver, task_type, "default"):
            if key in self.overhead_chars:
                return float(self.overhead_chars[key])
        return 0.0

    def completion_for(self, solver: str, task_type: str) -> float:
        for key in (solver, task_type, "default"):
            if key in self.completion_tokens:
                return float(self.completion_tokens[key])
        return 0.0

    def steps_for(self, solver: str, task_type: str) -> float:
        for key in (solver, task_type, "default"):
            if key in self.steps:
                value = float(self.steps[key])
                if value > 0:
                    return value
        return 1.0

    def multiplier_for(self, confidence: str) -> float:
        value = float(self.upper_multiplier.get(confidence, self.upper_multiplier.get("low", 2.5)))
        return max(value, 1.0)


@dataclass(frozen=True, slots=True)
class _Prior:
    """One calibration entry; ``None`` fields are wildcards."""

    solver: str | None
    task_type: str | None
    model: str | None
    n: int
    input_tokens: float
    output_tokens: float
    p90_cost: float | None
    steps: float | None
    source: str
    split: str
    weak_transfer: bool


@dataclass(frozen=True, slots=True)
class _Selection:
    prior: _Prior
    scope: str
    level: int


def _parse_prior(raw: Any) -> _Prior | None:
    if not isinstance(raw, MappingABC):
        return None
    n_value = _number(raw.get("n"))
    if n_value is None or n_value < 1:
        return None
    n = int(n_value)
    input_tokens = _number(raw.get("input_tokens"))
    output_tokens = _number(raw.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        # Fall back to a cost-only entry, but do not invent a token split.
        cost = _number(raw.get("cost"))
        if cost is None:
            return None
        input_tokens = 0.0
        output_tokens = cost
    if input_tokens < 0 or output_tokens < 0:
        return None
    p90 = _number(raw.get("p90_cost"))
    if p90 is not None and p90 < 0:
        p90 = None
    steps = _number(raw.get("steps"))
    if steps is not None and steps <= 0:
        steps = None
    return _Prior(
        solver=_name_or_none(raw.get("solver")),
        task_type=_name_or_none(raw.get("task_type")),
        model=_name_or_none(raw.get("model")),
        n=n,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        p90_cost=p90,
        steps=steps,
        source=str(raw.get("source") or "unknown"),
        split=str(raw.get("split") or "unknown"),
        weak_transfer=bool(raw.get("weak_transfer", False)),
    )


def _aggregate(candidates: list[_Prior]) -> _Prior:
    if len(candidates) == 1:
        prior = candidates[0]
        return prior
    total = sum(prior.n for prior in candidates)
    input_tokens = sum(prior.input_tokens * prior.n for prior in candidates) / total
    output_tokens = sum(prior.output_tokens * prior.n for prior in candidates) / total
    step_candidates = [prior for prior in candidates if prior.steps is not None]
    steps = None
    if step_candidates:
        weighted_steps = 0.0
        step_weight = 0
        for prior in step_candidates:
            assert prior.steps is not None
            weighted_steps += prior.steps * prior.n
            step_weight += prior.n
        steps = weighted_steps / step_weight
    p90_values = [prior.p90_cost for prior in candidates if prior.p90_cost is not None]
    return _Prior(
        solver=None,
        task_type=None,
        model=None,
        n=total,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        p90_cost=max(p90_values) if p90_values else None,
        steps=steps,
        source="+".join(sorted({prior.source for prior in candidates})),
        split="+".join(sorted({prior.split for prior in candidates})),
        weak_transfer=all(prior.weak_transfer for prior in candidates),
    )


def _heuristic_from(raw: Any) -> _Heuristic:
    source = raw if isinstance(raw, MappingABC) else {}
    defaults = DEFAULT_HEURISTIC
    return _Heuristic(
        chars_per_token=_numeric_map(source.get("chars_per_token"), defaults["chars_per_token"], allow_zero=False),
        overhead_chars=_numeric_map(
            source.get("prompt_overhead_chars"), defaults["prompt_overhead_chars"], allow_zero=True
        ),
        completion_tokens=_numeric_map(
            source.get("completion_tokens"), defaults["completion_tokens"], allow_zero=True
        ),
        steps=_numeric_map(source.get("steps"), defaults["steps"], allow_zero=False),
        upper_multiplier=_numeric_map(
            source.get("upper_multiplier"), defaults["upper_multiplier"], allow_zero=False
        ),
    )


def load_calibration(source: MappingABC[str, Any] | str | Path | None = None) -> dict[str, Any]:
    """Load calibration data without ever touching the network.

    A missing default file falls back to built-in defaults.  A malformed
    supplied file is logged and also falls back: an estimator failure must not
    stop the run (spec 3.3).
    """

    if isinstance(source, MappingABC):
        return copy.deepcopy(dict(source))

    path = Path(source) if source is not None else DEFAULT_CALIBRATION_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if source is None:
            log.info("calibration file %s not found, using built-in defaults", path)
        else:
            log.warning("calibration file %s not found, using built-in defaults", path)
        return copy.deepcopy(_DEFAULT_CALIBRATION)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("cannot read calibration %s (%s), using built-in defaults", path, exc)
        return copy.deepcopy(_DEFAULT_CALIBRATION)
    if not isinstance(raw, dict):
        log.warning("calibration %s is not an object, using built-in defaults", path)
        return copy.deepcopy(_DEFAULT_CALIBRATION)
    result = copy.deepcopy(_DEFAULT_CALIBRATION)
    result.update(raw)
    return result


# --------------------------------------------------------------------------- #
# Estimator
# --------------------------------------------------------------------------- #
class CostEstimator:
    """Offline, deterministic cost estimator with in-memory online correction."""

    def __init__(
        self,
        calibration: MappingABC[str, Any] | str | Path | None = None,
        *,
        min_observations: int = MIN_OBSERVATIONS,
        cache_features: bool = True,
    ) -> None:
        if isinstance(min_observations, bool) or not isinstance(min_observations, int) or min_observations < 1:
            raise ValueError("min_observations must be an integer >= 1")
        self._min_observations = min_observations
        self._cache_features = cache_features
        self._calibration = load_calibration(calibration)
        self._heuristic = _heuristic_from(self._calibration.get("heuristic"))
        self._priors: tuple[_Prior, ...] = tuple(
            prior
            for prior in (_parse_prior(raw) for raw in self._calibration.get("priors") or ())
            if prior is not None
        )
        self._lock = threading.RLock()
        self._feature_cache: dict[tuple[str, str, str, str], Features] = {}
        self._online: dict[tuple[str, ...], deque[float]] = {}

    # -- public API ---------------------------------------------------------
    def estimate(self, task: Task, solver: str, model: str) -> CostEstimate:
        """Return a deterministic forecast for ``task`` under ``solver``/``model``."""

        solver_name = _clean_name(solver, "generic")
        model_name = _clean_name(model, "unknown")
        features = self._features(task, solver_name, model_name)

        if solver_name in DETERMINISTIC_SOLVERS:
            return CostEstimate(
                expected_cost=0.0,
                upper_cost=0.0,
                expected_input_tokens=0.0,
                expected_output_tokens=0.0,
                confidence="high",
                model=model_name,
                solver=solver_name,
                drivers=(
                    "deterministic_solver=true",
                    f"solver={solver_name}",
                    "llm_calls=0",
                ),
            )

        selection = self._select_prior(solver_name, features.task_type, model_name)
        if selection is not None:
            input_tokens = selection.prior.input_tokens
            output_tokens = selection.prior.output_tokens
            if selection.level == 1:
                confidence: Confidence = _confidence_for(selection.prior.n)
            elif selection.level == 2:
                # Dropping the model is a fallback: never higher than medium.
                confidence = "medium" if selection.prior.n >= MIN_OBSERVATIONS else "low"
            else:
                # A fallback that drops the task type (or all specificity) is
                # always low confidence (spec 6.2).
                confidence = "low"
            if selection.prior.weak_transfer:
                confidence = "low"
            p90 = selection.prior.p90_cost
            drivers: list[str] = [
                f"scope={selection.scope}",
                f"n={selection.prior.n}",
                f"source={selection.prior.source}",
                f"split={selection.prior.split}",
            ]
            if selection.prior.weak_transfer:
                drivers.append("weak_transfer=true")
        else:
            input_tokens, output_tokens = self._heuristic_tokens(features, solver_name, features.task_type)
            confidence = "low"
            p90 = None
            steps = self._heuristic.steps_for(solver_name, features.task_type)
            drivers = [
                "scope=heuristic",
                f"instruction_chars={features.instruction_chars}",
                f"prompt_chars={features.prompt_chars}",
                f"steps={steps:g}",
                "chars_per_token=unmeasured",
            ]

        factor, key, samples = self._correction(solver_name, features.task_type, model_name)
        if samples and factor != 1.0:
            input_tokens *= factor
            output_tokens *= factor
            if p90 is not None:
                p90 *= factor
            drivers.append(f"online_factor={factor:.3f}")
            drivers.append(f"online_samples={samples}")
            drivers.append(f"online_key={key}")
        elif samples:
            drivers.append(f"online_samples={samples}")

        input_tokens = max(0.0, float(input_tokens))
        output_tokens = max(0.0, float(output_tokens))
        expected_cost = input_tokens / 10.0 + output_tokens
        # ``upper_cost`` is an empirical, risk-oriented estimate - never a
        # conformal guarantee (spec 6.4).
        upper_cost = max(expected_cost * self._heuristic.multiplier_for(confidence), expected_cost)
        if p90 is not None:
            upper_cost = max(upper_cost, float(p90))
        if not math.isfinite(upper_cost):
            upper_cost = expected_cost
        return CostEstimate(
            expected_cost=expected_cost,
            upper_cost=upper_cost,
            expected_input_tokens=input_tokens,
            expected_output_tokens=output_tokens,
            confidence=confidence,
            model=model_name,
            solver=solver_name,
            drivers=tuple(drivers),
        )

    def observe(self, task: Task, solver: str, model: str, usage: Any) -> None:
        """Record actual usage and adapt future estimates in memory.

        ``calibration.json`` is never rewritten.  The correction is a median of
        observed ``actual / expected`` ratios, bounded to ``[0.2, 5.0]`` so one
        outlier cannot zero out or explode the budget forecast.
        """

        solver_name = _clean_name(solver, "generic")
        model_name = _clean_name(model, "unknown")
        if solver_name in DETERMINISTIC_SOLVERS:
            return
        if _has_invalid_token_fields(usage):
            log.warning(
                "estimator.observe: invalid usage for %s", getattr(task, "task_id", "?")
            )
            return
        try:
            normalized = normalize_usage(usage)
        except (TypeError, ValueError) as exc:
            log.warning("estimator.observe: invalid usage for %s: %s", getattr(task, "task_id", "?"), exc)
            return
        # ``total_cost`` may be absent on a directly constructed record; fall
        # back to the formula-derived cost so observation still works.
        actual_cost = float(normalized.total_cost or normalized.cost or 0.0)
        if actual_cost <= 0:
            return
        try:
            expected_cost = self.estimate(task, solver_name, model_name).expected_cost
        except Exception:  # pragma: no cover - defensive: never break a run
            log.exception("estimator.observe: estimate failed for %s", getattr(task, "task_id", "?"))
            return
        if expected_cost <= 0:
            return
        ratio = min(max(actual_cost / expected_cost, _MIN_CORRECTION), _MAX_CORRECTION)
        task_type = _clean_name(getattr(task, "task_type", None), "unknown")
        keys = self._correction_keys(solver_name, task_type, model_name)
        with self._lock:
            for key in keys:
                samples = self._online.get(key)
                if samples is None:
                    samples = deque(maxlen=_MAX_ONLINE_SAMPLES)
                    self._online[key] = samples
                samples.append(ratio)

    def clear_cache(self) -> None:
        """Drop cached features; online observations are kept."""

        with self._lock:
            self._feature_cache.clear()

    def reset_online(self) -> None:
        """Drop in-memory observations, restoring calibration-only behaviour."""

        with self._lock:
            self._online.clear()

    # -- internals ----------------------------------------------------------
    def _features(self, task: Task, solver: str, model: str) -> Features:
        workdir = getattr(task, "workdir", None) or getattr(task, "task_dir", "")
        key = (
            str(getattr(task, "task_id", "")),
            str(workdir),
            solver,
            model,
        )
        if self._cache_features:
            with self._lock:
                cached = self._feature_cache.get(key)
            if cached is not None:
                return cached
        features = extract_features(task, solver, model)
        if self._cache_features:
            with self._lock:
                self._feature_cache[key] = features
        return features

    def _select_prior(self, solver: str, task_type: str, model: str) -> _Selection | None:
        minimum = self._min_observations

        def pick(candidates: list[_Prior], scope: str, level: int) -> _Selection | None:
            if not candidates:
                return None
            aggregated = _aggregate(candidates)
            # Only the most specific level needs the 8-observation threshold
            # (spec 6.2); broader medians are allowed but get low confidence
            # when their sample is smaller.
            required = minimum if level == 1 else 1
            if aggregated.n < required:
                return None
            return _Selection(aggregated, scope, level)

        # Level 1: exact (solver, task_type, model).
        selection = pick(
            [
                prior
                for prior in self._priors
                if prior.solver == solver and prior.task_type == task_type and prior.model == model
            ],
            "solver+type+model",
            1,
        )
        if selection is not None:
            return selection

        # Level 2: (solver, task_type), optionally aggregated across models.
        exact = [
            prior
            for prior in self._priors
            if prior.solver == solver and prior.task_type == task_type and prior.model is None
        ]
        if not exact:
            exact = [
                prior
                for prior in self._priors
                if prior.solver == solver and prior.task_type == task_type and prior.model is not None
            ]
        selection = pick(exact, "solver+type", 2)
        if selection is not None:
            return selection

        # Level 3: (solver, model), then (solver,), then global.
        exact = [
            prior
            for prior in self._priors
            if prior.solver == solver and prior.task_type is None and prior.model == model
        ]
        if not exact:
            exact = [
                prior
                for prior in self._priors
                if prior.solver == solver and prior.task_type is None and prior.model is not None
            ]
        selection = pick(exact, "solver+model", 3)
        if selection is not None:
            return selection

        exact = [
            prior
            for prior in self._priors
            if prior.solver == solver and prior.task_type is None and prior.model is None
        ]
        if not exact:
            exact = [prior for prior in self._priors if prior.solver == solver]
        selection = pick(exact, "solver", 3)
        if selection is not None:
            return selection

        global_priors = [
            prior
            for prior in self._priors
            if prior.solver is None and prior.task_type is None and prior.model is None
        ]
        return pick(global_priors, "global", 3)

    def _heuristic_tokens(self, features: Features, solver: str, task_type: str) -> tuple[float, float]:
        heuristic = self._heuristic
        text_chars = features.instruction_chars + features.text_chars
        input_per_call = (
            text_chars / heuristic.chars_for("text")
            + features.code_chars / heuristic.chars_for("code")
            + features.json_chars / heuristic.chars_for("json")
            + heuristic.overhead_for(solver, task_type) / heuristic.chars_for("text")
        )
        steps = heuristic.steps_for(solver, task_type)
        completion_per_call = heuristic.completion_for(solver, task_type)
        # One token of prompt is a floor so a non-deterministic task never
        # claims a literal zero before any observation exists.
        return max(input_per_call * steps, 1.0), max(completion_per_call * steps, 0.0)

    @staticmethod
    def _correction_keys(solver: str, task_type: str, model: str) -> tuple[tuple[str, ...], ...]:
        return (
            ("model_solver_type", model, solver, task_type),
            ("solver_type", solver, task_type),
            ("model_solver", model, solver),
            ("solver", solver),
            ("global",),
        )

    def _correction(self, solver: str, task_type: str, model: str) -> tuple[float, str, int]:
        with self._lock:
            for key in self._correction_keys(solver, task_type, model):
                samples = self._online.get(key)
                if samples:
                    factor = min(max(statistics.median(samples), _MIN_CORRECTION), _MAX_CORRECTION)
                    return factor, "/".join(key), len(samples)
        return 1.0, "", 0


def _clean_name(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _has_invalid_token_fields(usage: Any) -> bool:
    """True when a usage payload carries a negative/non-numeric token count.

    The canonical normalizer tolerates malformed proxy payloads by treating
    bad fields as zero.  Online correction is different: a corrupt sample must
    be ignored completely rather than skewing the median, so ``observe``
    rejects such payloads up front.
    """

    if isinstance(usage, MappingABC):
        values = (usage.get(name) for name in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "reasoning_tokens"))
    else:
        values = (
            getattr(usage, name, None)
            for name in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "reasoning_tokens")
        )
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return True
    return False
