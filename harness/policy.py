"""Budget-aware task selection policy (spec 001 v5.0, sections 4 and 8).

This module is the *decision* layer between the cost estimator and the
runner.  It never calls a model and never touches the network.  For every
task it turns a :class:`~harness.estimator.CostEstimate` plus a solve
probability into an explicit :class:`Decision`::

    v_i = reward_i * P_solve_i          # expected score
    c_i = planned cost under cost_basis  # expected, or conservative upper
    t_i = heuristic wall-clock estimate

The selection problem is::

    maximise  sum(v_i * x_i)
    subject to sum(c_i * x_i) <= remaining - planning_reserve
               sum(t_i * x_i) <= time_left
               x_i in {0, 1}

``cost_basis`` selects what ``c_i`` means.  ``expected`` (the default) plans
with ``max(expected_cost, EXPECTED_COST_FLOOR)`` and relies on the budget
ledger to enforce hard per-call safety; ``upper`` is the conservative mode and
plans with ``upper_cost``.  A genuine zero-cost deterministic task stays at
``0`` so it remains free at a zero budget.

There is deliberately **one global budget**.  No per-task cap and no
per-task token budget exists; a single task may consume the whole available
remainder (section 2 of the spec, AGENTS.md hard rule).

``P_solve`` is never derived from ``reward``.  It comes from one of, in
priority order:

1. a historical solve rate supplied by the caller
   (``task_id`` > ``solver:task_type`` > ``solver`` > ``task_type``);
2. the deterministic-solver rule: a feasible task solved by a deterministic
   solver has ``P_solve = 1.0``;
3. the documented confidence heuristic for LLM solvers:
   ``high -> 0.7``, ``medium -> 0.5``, ``low -> 0.3`` (all configurable).

Every :class:`PlanEntry` records ``probability_source`` so a run can explain
why a probability was used.

Policies
--------
``accept_all``
    Consider feasible tasks in input order and take every one that fits.
``reward_sort``
    Sort by raw reward descending.
``value_cost_greedy``
    Sort by ``v_i / c_i`` descending (fallback recommended by section 8.1).
``knapsack_dp``
    Discretised 0/1 knapsack over the planned costs ``c_i``, then execute the
    selected set in ``v / c`` order.  It is bounded by
    ``MAX_KNAPSACK_STATES``.

Replanning
----------
:func:`replan` takes a fresh ledger snapshot and recomputes decisions for the
remaining tasks.  Actual usage is reflected by the ledger's ``remaining``
value; past :class:`Decision` objects are frozen and are never mutated.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

from .estimator import CostEstimate
from .features import DETERMINISTIC_SOLVERS
from .triage import DEFAULT_THRESHOLD, triage

__all__ = [
    "ACTION_REASONS",
    "COST_BASES",
    "DEFAULT_CONFIG",
    "DEFAULT_POLICY",
    "EXPECTED_COST_FLOOR",
    "POLICIES",
    "POLICY_NAMES",
    "CostBasis",
    "Decision",
    "PlanEntry",
    "PlanSummary",
    "PolicyConfig",
    "accept_all",
    "estimate_time",
    "knapsack_dp",
    "plan",
    "plan_entries",
    "planning_reserve",
    "replan",
    "reward_sort",
    "solve_probability",
    "summarize",
    "value_cost_greedy",
]

log = logging.getLogger(__name__)

Action = Literal["accept", "reject_cost", "reject_impossible", "reject_time"]
Status = Literal["solvable", "uncertain", "impossible"]
PolicyName = Literal["accept_all", "reward_sort", "value_cost_greedy", "knapsack_dp"]
CostBasis = Literal["expected", "upper"]

ACCEPT: Final[Action] = "accept"
REJECT_COST: Final[Action] = "reject_cost"
REJECT_IMPOSSIBLE: Final[Action] = "reject_impossible"
REJECT_TIME: Final[Action] = "reject_time"

#: Canonical, deterministic reason strings required by section 8 / D2.
ACTION_REASONS: Final[dict[Action, str]] = {
    ACCEPT: "accept",
    REJECT_COST: "reject:cost",
    REJECT_IMPOSSIBLE: "reject:impossible",
    REJECT_TIME: "reject:time",
}

DEFAULT_POLICY: Final[PolicyName] = "value_cost_greedy"

#: Cost-basis spellings accepted on the CLI and in :class:`PolicyConfig`.
COST_BASES: Final[tuple[CostBasis, ...]] = ("expected", "upper")

#: Floor for a positive expected cost.  It keeps a non-zero task from becoming
#: effectively free in the ratio and knapsack arithmetic.  A genuine
#: zero-cost deterministic task is exempt and stays free at a zero budget.
EXPECTED_COST_FLOOR: Final = 1.0

#: Deterministic task types (kept local so policy does not import planner and
#: create a cycle).  This mirrors ``estimator.DETERMINISTIC_SOLVERS``.
DETERMINISTIC_TASK_TYPES: Final[frozenset[str]] = frozenset(DETERMINISTIC_SOLVERS)

#: Fixed discretisation unit for the knapsack DP, in cost units.  A fixed unit
#: keeps the DP state grid stable as the budget grows, so a larger budget can
#: never select a worse set because the grid changed.
KNAPSACK_UNIT: Final = 1.0
MAX_KNAPSACK_STATES: Final = 200_000

_EPS: Final = 1e-9


@runtime_checkable
class TaskLike(Protocol):
    """Minimal task surface used by the policy (spec 4 ``Task`` contract)."""

    task_id: str
    reward: float
    task_type: str


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Tunable, documented probabilities and reserves.

    ``low_confidence_share`` is the fraction of low-confidence estimates that
    switches the planning reserve from 5% to 10%.  "Mostly low" is a strict
    majority (``> low_confidence_share``).

    ``cost_basis`` chooses the planned cost ``c_i``:

    * ``expected`` (default) uses ``max(expected_cost, EXPECTED_COST_FLOOR)``.
      It avoids rejecting profitable work because a conservative upper bound
      is far above the typical cost; hard safety remains the ledger's job.
    * ``upper`` uses ``upper_cost`` and is the conservative planning mode.
    """

    probability_high: float = 0.7
    probability_medium: float = 0.5
    probability_low: float = 0.3
    probability_default: float = 0.5
    planning_reserve_rate: float = 0.05
    low_confidence_reserve_rate: float = 0.10
    low_confidence_share: float = 0.5
    cost_basis: CostBasis = "expected"
    deterministic_time_seconds: float = 1.0
    llm_base_time_seconds: float = 10.0
    output_tokens_per_second: float = 10.0

    def __post_init__(self) -> None:
        for name in (
            "probability_high",
            "probability_medium",
            "probability_low",
            "probability_default",
            "planning_reserve_rate",
            "low_confidence_reserve_rate",
            "low_confidence_share",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be numeric, got {value!r}")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value!r}")
        for name in (
            "deterministic_time_seconds",
            "llm_base_time_seconds",
            "output_tokens_per_second",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
        if float(self.output_tokens_per_second) <= 0.0:
            raise ValueError("output_tokens_per_second must be > 0")
        if self.cost_basis not in COST_BASES:
            raise ValueError(
                f"cost_basis must be one of {', '.join(COST_BASES)}, "
                f"got {self.cost_basis!r}"
            )

    def probability_for(self, confidence: str) -> float:
        if confidence == "high":
            return float(self.probability_high)
        if confidence == "medium":
            return float(self.probability_medium)
        if confidence == "low":
            return float(self.probability_low)
        return float(self.probability_default)


DEFAULT_CONFIG: Final = PolicyConfig()


@dataclass(frozen=True, slots=True)
class Decision:
    """One accept/reject decision (spec 4 contract).

    ``action`` is the machine-readable enum; ``reason`` is the explicit,
    deterministic tag from :data:`ACTION_REASONS`.  ``expected_reward`` is the
    expected value ``v_i`` of the task (0 for a provably impossible task), and
    ``planned_cost`` is the planned cost ``c_i`` under the configured basis.
    """

    action: Action
    reason: str
    planned_cost: float
    expected_reward: float
    task_id: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTION_REASONS:
            raise ValueError(f"unknown decision action: {self.action!r}")
        for name in ("planned_cost", "expected_reward"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric, got {value!r}")
            number = float(value)
            if not math.isfinite(number) or number < 0.0:
                raise ValueError(f"{name} must be finite and >= 0, got {value!r}")
        if not isinstance(self.task_id, str):
            raise ValueError(f"task_id must be a string, got {self.task_id!r}")
        if not isinstance(self.reason, str) or not self.reason:
            object.__setattr__(self, "reason", ACTION_REASONS[self.action])

    @property
    def accepted(self) -> bool:
        return self.action == ACCEPT

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "action": self.action,
            "reason": self.reason,
            "planned_cost": round(float(self.planned_cost), 6),
            "expected_reward": round(float(self.expected_reward), 6),
        }


@dataclass(frozen=True, slots=True)
class PlanEntry:
    """A task together with its estimate and final decision."""

    task: TaskLike
    estimate: CostEstimate
    decision: Decision
    probability: float
    probability_source: str
    value: float
    time_cost: float
    status: Status
    index: int

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": self.task.task_id,
            "task_type": self.task.task_type,
            "reward": float(self.task.reward),
            "status": self.status,
            "probability": round(float(self.probability), 6),
            "probability_source": self.probability_source,
            "value": round(float(self.value), 6),
            "time_seconds": round(float(self.time_cost), 3),
        }
        payload.update(self.decision.as_dict())
        return payload


@dataclass(frozen=True, slots=True)
class PlanSummary:
    """Aggregate of one policy run; useful for tests and simulation."""

    policy: str
    total: int
    feasible: int
    accepted: int
    coverage: float
    planned_cost: float
    expected_reward: float
    time_used: float
    reasons: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "total": self.total,
            "feasible": self.feasible,
            "accepted": self.accepted,
            "coverage": round(self.coverage, 6),
            "planned_cost": round(self.planned_cost, 6),
            "expected_reward": round(self.expected_reward, 6),
            "time_used": round(self.time_used, 3),
            "reasons": dict(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    task: TaskLike
    estimate: CostEstimate
    probability: float
    probability_source: str
    value: float
    cost: float
    time_cost: float
    status: Status
    index: int


# --------------------------------------------------------------------------- #
# Probability, reserve and time heuristics
# --------------------------------------------------------------------------- #
def _clean_name(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _historical_rate(
    task: TaskLike,
    estimate: CostEstimate,
    rates: Mapping[str, float] | None,
) -> tuple[float, str] | None:
    if not rates:
        return None
    solver = _clean_name(estimate.solver)
    task_type = _clean_name(getattr(task, "task_type", ""))
    task_id = _clean_name(getattr(task, "task_id", ""))
    keys = [
        task_id,
        f"{solver}:{task_type}" if solver and task_type else "",
        solver,
        task_type,
    ]
    for key in keys:
        if not key or key not in rates:
            continue
        value = rates[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"historical solve rate for {key!r} must be numeric, got {value!r}")
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ValueError(f"historical solve rate for {key!r} must be in [0, 1], got {value!r}")
        return number, f"historical:{key}"
    return None


def solve_probability(
    task: TaskLike,
    estimate: CostEstimate,
    *,
    feasible: bool = True,
    historical_solve_rates: Mapping[str, float] | None = None,
    config: PolicyConfig = DEFAULT_CONFIG,
) -> tuple[float, str]:
    """Return ``(P_solve, source)`` without looking at ``reward``.

    The source string is one of ``impossible``, ``historical:<key>``,
    ``deterministic`` or ``heuristic:<confidence>``.
    """

    if not feasible:
        return 0.0, "impossible"

    historical = _historical_rate(task, estimate, historical_solve_rates)
    if historical is not None:
        return historical

    solver = _clean_name(estimate.solver)
    task_type = _clean_name(getattr(task, "task_type", ""))
    if solver in DETERMINISTIC_TASK_TYPES or task_type in DETERMINISTIC_TASK_TYPES:
        return 1.0, "deterministic"

    confidence = _clean_name(estimate.confidence).lower()
    return config.probability_for(confidence), f"heuristic:{confidence or 'unknown'}"


def estimate_time(
    task: TaskLike,
    estimate: CostEstimate,
    *,
    config: PolicyConfig = DEFAULT_CONFIG,
) -> float:
    """Crude wall-clock estimate in seconds, never a guarantee.

    Deterministic solvers are assumed to be fast.  LLM solvers pay a fixed
    base plus the expected output tokens at a conservative throughput.  The
    estimate is used only for the separate time constraint.
    """

    solver = _clean_name(estimate.solver)
    task_type = _clean_name(getattr(task, "task_type", ""))
    if solver in DETERMINISTIC_TASK_TYPES or task_type in DETERMINISTIC_TASK_TYPES:
        return float(config.deterministic_time_seconds)
    tokens = max(0.0, float(estimate.expected_output_tokens))
    return float(config.llm_base_time_seconds) + tokens / float(config.output_tokens_per_second)


def planning_reserve(
    total_budget: float,
    estimates: Iterable[CostEstimate] | Mapping[str, CostEstimate],
    *,
    config: PolicyConfig = DEFAULT_CONFIG,
) -> float:
    """Return the global planning reserve (5%, or 10% when mostly low).

    The reserve is a share of the *total* budget, not of the current
    remainder, so replanning cannot silently shrink the safety margin.  It is
    an empirical planning buffer, never a conformance guarantee.
    """

    total = float(total_budget)
    if not math.isfinite(total) or total < 0.0:
        raise ValueError(f"total_budget must be finite and >= 0, got {total_budget!r}")
    values = estimates.values() if isinstance(estimates, Mapping) else estimates
    confidences = [str(estimate.confidence).strip().lower() for estimate in values]
    rate = float(config.planning_reserve_rate)
    if confidences:
        low = sum(1 for confidence in confidences if confidence == "low")
        if low / len(confidences) > float(config.low_confidence_share):
            rate = float(config.low_confidence_reserve_rate)
    return total * rate


# --------------------------------------------------------------------------- #
# Selection primitives
# --------------------------------------------------------------------------- #
def _ratio(candidate: _Candidate) -> float:
    if candidate.cost <= _EPS:
        return math.inf
    return candidate.value / candidate.cost


def _order_ratio(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    # Section 8.2.5: highest v/c first, ties by larger absolute value, then
    # input position for a fully deterministic order.
    return sorted(
        candidates,
        key=lambda candidate: (-_ratio(candidate), -candidate.value, candidate.index),
    )


def _order_reward(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    return sorted(
        candidates,
        key=lambda candidate: (-float(candidate.task.reward), -candidate.value, candidate.index),
    )


def _greedy_allocate(
    ordered: Sequence[_Candidate],
    *,
    capacity: float,
    time_left: float,
) -> tuple[list[_Candidate], dict[str, Action]]:
    accepted: list[_Candidate] = []
    rejected: dict[str, Action] = {}
    spent = 0.0
    used_time = 0.0
    for candidate in ordered:
        task_id = _clean_name(candidate.task.task_id)
        if candidate.time_cost > time_left - used_time + _EPS:
            rejected[task_id] = REJECT_TIME
            continue
        if candidate.cost > capacity - spent + _EPS:
            rejected[task_id] = REJECT_COST
            continue
        accepted.append(candidate)
        spent += candidate.cost
        used_time += candidate.time_cost
    return accepted, rejected


def _knapsack_select(candidates: Sequence[_Candidate], capacity: float) -> set[str]:
    """Discretised 0/1 knapsack on the planned cost maximising expected value.

    Weights are ``ceil(cost / unit)`` so the discretised solution is always
    conservative: the true cost of a selected set cannot exceed the capacity.
    Ties keep the earlier item order, which makes the result deterministic.
    """

    budget = max(0.0, float(capacity))
    unit = max(KNAPSACK_UNIT, budget / MAX_KNAPSACK_STATES)
    cap = min(int(math.floor(budget / unit + _EPS)), MAX_KNAPSACK_STATES)
    items = [candidate for candidate in candidates if candidate.value > 0.0]
    weights: list[int] = [0] * len(items)
    chosen: set[str] = set()

    dp = [float("-inf")] * (cap + 1)
    dp[0] = 0.0
    keep = [bytearray(cap + 1) for _ in items]

    for index, candidate in enumerate(items):
        weight = int(math.ceil(candidate.cost / unit - _EPS)) if candidate.cost > _EPS else 0
        weights[index] = weight
        if weight <= 0:
            # A zero-cost task never consumes capacity; take it unconditionally.
            continue
        if weight > cap:
            continue
        for state in range(cap, weight - 1, -1):
            previous = dp[state - weight]
            if previous == float("-inf"):
                continue
            candidate_value = previous + candidate.value
            if candidate_value > dp[state] + _EPS:
                dp[state] = candidate_value
                keep[index][state] = 1

    best_weight = 0
    best_value = 0.0
    for state in range(cap + 1):
        if dp[state] > best_value + _EPS:
            best_value = dp[state]
            best_weight = state
        # On equal value prefer the smaller weight (less budget spent), and
        # by scanning ascending that is the first state we saw.

    state = best_weight
    for index in range(len(items) - 1, -1, -1):
        if state < 0:
            break
        if keep[index][state]:
            chosen.add(_clean_name(items[index].task.task_id))
            state -= weights[index]

    for index, candidate in enumerate(items):
        if weights[index] == 0:
            chosen.add(_clean_name(candidate.task.task_id))
    return chosen


def _select(
    policy: PolicyName,
    feasible: Sequence[_Candidate],
    *,
    capacity: float,
    time_left: float,
) -> tuple[list[_Candidate], dict[str, Action]]:
    if policy == "accept_all":
        return _greedy_allocate(feasible, capacity=capacity, time_left=time_left)
    if policy == "reward_sort":
        return _greedy_allocate(_order_reward(feasible), capacity=capacity, time_left=time_left)
    if policy == "value_cost_greedy":
        return _greedy_allocate(_order_ratio(feasible), capacity=capacity, time_left=time_left)
    if policy == "knapsack_dp":
        return _knapsack_allocate(feasible, capacity=capacity, time_left=time_left)
    raise ValueError(
        f"unknown policy {policy!r}; expected one of {', '.join(POLICY_NAMES)}"
    )


def _knapsack_allocate(
    feasible: Sequence[_Candidate],
    *,
    capacity: float,
    time_left: float,
) -> tuple[list[_Candidate], dict[str, Action]]:
    rejected: dict[str, Action] = {}
    candidates: list[_Candidate] = []
    for candidate in feasible:
        task_id = _clean_name(candidate.task.task_id)
        if candidate.time_cost > time_left + _EPS:
            rejected[task_id] = REJECT_TIME
        elif candidate.cost > capacity + _EPS:
            rejected[task_id] = REJECT_COST
        else:
            candidates.append(candidate)

    selected_ids = _knapsack_select(candidates, capacity)
    selected = [
        candidate
        for candidate in _order_ratio(candidates)
        if _clean_name(candidate.task.task_id) in selected_ids
    ]
    accepted, time_rejected = _greedy_allocate(selected, capacity=capacity, time_left=time_left)
    rejected.update(time_rejected)

    accepted_ids = {_clean_name(candidate.task.task_id) for candidate in accepted}
    for candidate in candidates:
        task_id = _clean_name(candidate.task.task_id)
        if task_id in accepted_ids or task_id in rejected:
            continue
        if task_id not in selected_ids:
            rejected[task_id] = REJECT_COST
    return accepted, rejected


# --------------------------------------------------------------------------- #
# Planning entry points
# --------------------------------------------------------------------------- #
def _coerce_estimates(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
) -> dict[str, CostEstimate]:
    if isinstance(estimates, Mapping):
        mapping = {str(key): value for key, value in estimates.items()}
    else:
        sequence = list(estimates)
        if len(sequence) != len(tasks):
            raise ValueError(
                "a sequence of estimates must have exactly one entry per task "
                f"({len(sequence)} != {len(tasks)})"
            )
        mapping = {_clean_name(task.task_id): estimate for task, estimate in zip(tasks, sequence)}

    result: dict[str, CostEstimate] = {}
    for task in tasks:
        task_id = _clean_name(task.task_id)
        if not task_id:
            raise ValueError("every task must have a non-empty task_id")
        if task_id in result:
            raise ValueError(f"duplicate task_id {task_id!r}")
        estimate = mapping.get(task_id)
        if estimate is None:
            raise ValueError(f"no cost estimate supplied for task {task_id!r}")
        if not isinstance(estimate, CostEstimate):
            raise TypeError(
                f"estimate for task {task_id!r} must be CostEstimate, "
                f"got {type(estimate).__name__}"
            )
        for name in ("expected_cost", "upper_cost"):
            value = float(getattr(estimate, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"{name} for task {task_id!r} must be finite and >= 0, got {value!r}"
                )
        result[task_id] = estimate
    return result


def _impossible_tasks(
    tasks: Sequence[TaskLike],
    impossible_ids: Iterable[str] | None,
    triage_threshold: float,
) -> set[str]:
    if impossible_ids is not None:
        return {str(task_id) for task_id in impossible_ids}

    flagged: set[str] = set()
    for task in tasks:
        # Only run triage on the real Task surface; duck-typed simulation
        # tasks pass an explicit (possibly empty) impossible_ids set.
        if not (hasattr(task, "instruction") and hasattr(task, "workdir")):
            continue
        try:
            if triage(task, triage_threshold).infeasible:  # type: ignore[arg-type]
                flagged.add(_clean_name(task.task_id))
        except (AttributeError, OSError, ValueError) as exc:  # pragma: no cover - defensive
            log.warning("triage failed for %s: %s", getattr(task, "task_id", "?"), exc)
    return flagged


def _planned_cost(
    estimate: CostEstimate,
    basis: CostBasis,
    *,
    deterministic: bool,
) -> float:
    """Return ``c_i`` for the configured basis.

    A genuine zero-cost task (deterministic solver) stays free; otherwise the
    floor keeps a tiny expected cost from vanishing in ratios.
    """

    if basis == "upper":
        return float(estimate.upper_cost)
    expected = float(estimate.expected_cost)
    if expected <= 0.0 and deterministic:
        return 0.0
    return max(expected, EXPECTED_COST_FLOOR)


def _build_candidates(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate],
    *,
    impossible: set[str],
    historical_solve_rates: Mapping[str, float] | None,
    config: PolicyConfig,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for index, task in enumerate(tasks):
        task_id = _clean_name(task.task_id)
        estimate = estimates[task_id]
        reward = float(task.reward)
        if not math.isfinite(reward) or reward < 0.0:
            raise ValueError(f"reward for task {task_id!r} must be finite and >= 0, got {task.reward!r}")

        feasible = task_id not in impossible
        probability, source = solve_probability(
            task,
            estimate,
            feasible=feasible,
            historical_solve_rates=historical_solve_rates,
            config=config,
        )
        solver = _clean_name(estimate.solver)
        task_type = _clean_name(getattr(task, "task_type", ""))
        deterministic = (
            solver in DETERMINISTIC_TASK_TYPES
            or task_type in DETERMINISTIC_TASK_TYPES
        )
        if not feasible:
            status: Status = "impossible"
        elif deterministic:
            status = "solvable"
        else:
            status = "uncertain"

        value = reward * probability
        candidates.append(
            _Candidate(
                task=task,
                estimate=estimate,
                probability=probability,
                probability_source=source,
                value=value,
                cost=_planned_cost(
                    estimate, config.cost_basis, deterministic=deterministic
                ),
                time_cost=estimate_time(task, estimate, config=config),
                status=status,
                index=index,
            )
        )
    return candidates


def _entry(candidate: _Candidate, action: Action) -> PlanEntry:
    reason = ACTION_REASONS[action]
    expected_reward = 0.0 if action == REJECT_IMPOSSIBLE else candidate.value
    return PlanEntry(
        task=candidate.task,
        estimate=candidate.estimate,
        decision=Decision(
            action=action,
            reason=reason,
            planned_cost=candidate.cost,
            expected_reward=expected_reward,
            task_id=_clean_name(candidate.task.task_id),
        ),
        probability=candidate.probability,
        probability_source=candidate.probability_source,
        value=candidate.value,
        time_cost=candidate.time_cost,
        status=candidate.status,
        index=candidate.index,
    )


def plan_entries(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
    *,
    policy: PolicyName | str = DEFAULT_POLICY,
    remaining: float,
    total_budget: float | None = None,
    time_left: float | None = None,
    config: PolicyConfig = DEFAULT_CONFIG,
    historical_solve_rates: Mapping[str, float] | None = None,
    impossible_ids: Iterable[str] | None = None,
    triage_threshold: float = DEFAULT_THRESHOLD,
) -> list[PlanEntry]:
    """Plan ``tasks`` under the single global budget and a separate time cap.

    ``remaining`` is what the ledger says is still available.  The planned
    cost ``c_i`` of accepted tasks is constrained by
    ``sum(c_i) <= remaining - planning_reserve``; ``c_i`` is the expected cost
    by default and the conservative upper cost when ``cost_basis="upper"``.
    Rejected tasks are
    returned after the accepted ones, in input order, so the accepted prefix
    is the execution order.

    ``total_budget`` defaults to ``remaining`` when the caller does not have
    the original total; passing the real total is preferred because the
    planning reserve is a share of the total.
    """

    tasks_list = list(tasks)
    if not tasks_list:
        return []

    policy_name = str(policy)
    if policy_name not in POLICY_NAMES:
        raise ValueError(
            f"unknown policy {policy!r}; expected one of {', '.join(POLICY_NAMES)}"
        )

    remaining_value = float(remaining)
    if not math.isfinite(remaining_value) or remaining_value < 0.0:
        raise ValueError(f"remaining must be finite and >= 0, got {remaining!r}")
    total_value = remaining_value if total_budget is None else float(total_budget)
    if not math.isfinite(total_value) or total_value < 0.0:
        raise ValueError(f"total_budget must be finite and >= 0, got {total_budget!r}")

    if time_left is None:
        time_value = math.inf
    else:
        time_value = float(time_left)
        if math.isnan(time_value) or time_value < 0.0:
            raise ValueError(f"time_left must be >= 0 or None, got {time_left!r}")

    estimates_by_id = _coerce_estimates(tasks_list, estimates)
    impossible = _impossible_tasks(tasks_list, impossible_ids, triage_threshold)
    candidates = _build_candidates(
        tasks_list,
        estimates_by_id,
        impossible=impossible,
        historical_solve_rates=historical_solve_rates,
        config=config,
    )

    feasible = [candidate for candidate in candidates if candidate.status != "impossible"]
    reserve = planning_reserve(
        total_value,
        [candidate.estimate for candidate in feasible],
        config=config,
    )
    capacity = max(0.0, remaining_value - reserve)

    accepted, rejection_actions = _select(
        policy_name,  # type: ignore[arg-type]
        feasible,
        capacity=capacity,
        time_left=time_value,
    )

    accepted_ids = {_clean_name(candidate.task.task_id) for candidate in accepted}
    entries: list[PlanEntry] = [_entry(candidate, ACCEPT) for candidate in accepted]
    rest = [
        candidate
        for candidate in candidates
        if _clean_name(candidate.task.task_id) not in accepted_ids
    ]
    rest.sort(key=lambda candidate: candidate.index)
    for candidate in rest:
        if candidate.status == "impossible":
            action: Action = REJECT_IMPOSSIBLE
        else:
            action = rejection_actions.get(_clean_name(candidate.task.task_id), REJECT_COST)
        entries.append(_entry(candidate, action))
    return entries


def plan(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
    **kwargs: Any,
) -> list[Decision]:
    """Return only the ordered :class:`Decision` list for one policy."""

    return [entry.decision for entry in plan_entries(tasks, estimates, **kwargs)]


def _snapshot_budget(
    ledger_snapshot: Any,
    total_budget: float | None,
) -> tuple[float, float]:
    if isinstance(ledger_snapshot, Mapping):
        remaining_raw = ledger_snapshot.get("remaining")
        total_raw = ledger_snapshot.get("total")
    else:
        remaining_raw = getattr(ledger_snapshot, "remaining", None)
        total_raw = getattr(ledger_snapshot, "total", None)
    if remaining_raw is None:
        raise ValueError(
            "ledger_snapshot must expose 'remaining' (mapping key or attribute)"
        )
    remaining = float(remaining_raw)
    if not math.isfinite(remaining) or remaining < 0.0:
        raise ValueError(f"ledger remaining must be finite and >= 0, got {remaining!r}")
    if total_budget is not None:
        total = float(total_budget)
    elif total_raw is not None:
        total = float(total_raw)
    else:
        # Documented fallback: without the original total the reserve is a
        # share of the current remainder.  Callers should pass the real total.
        total = remaining
    return remaining, total


def replan(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
    ledger_snapshot: Any,
    time_left: float | None,
    *,
    policy: PolicyName | str = DEFAULT_POLICY,
    total_budget: float | None = None,
    config: PolicyConfig = DEFAULT_CONFIG,
    historical_solve_rates: Mapping[str, float] | None = None,
    impossible_ids: Iterable[str] | None = None,
    triage_threshold: float = DEFAULT_THRESHOLD,
) -> list[Decision]:
    """Recompute decisions after actual usage.

    Actual spend is read from the ledger snapshot's ``remaining``; the ledger
    is the only source of truth for what has been spent.  Previously returned
    :class:`Decision` objects are frozen and are never modified, so a caller
    can keep an audit trail of what was planned at each step.
    """

    remaining, total = _snapshot_budget(ledger_snapshot, total_budget)
    return plan(
        tasks,
        estimates,
        policy=policy,
        remaining=remaining,
        total_budget=total,
        time_left=time_left,
        config=config,
        historical_solve_rates=historical_solve_rates,
        impossible_ids=impossible_ids,
        triage_threshold=triage_threshold,
    )


def summarize(entries: Sequence[PlanEntry], *, policy: str = "") -> PlanSummary:
    """Aggregate a plan for tests, logs and the policy simulation."""

    reasons: dict[str, int] = {}
    accepted = [entry for entry in entries if entry.decision.action == ACCEPT]
    for entry in entries:
        reasons[entry.decision.reason] = reasons.get(entry.decision.reason, 0) + 1
    total = len(entries)
    feasible = sum(1 for entry in entries if entry.status != "impossible")
    return PlanSummary(
        policy=policy,
        total=total,
        feasible=feasible,
        accepted=len(accepted),
        coverage=(len(accepted) / feasible) if feasible else 0.0,
        planned_cost=sum(float(entry.decision.planned_cost) for entry in accepted),
        expected_reward=sum(float(entry.value) for entry in accepted),
        time_used=sum(float(entry.time_cost) for entry in accepted),
        reasons=tuple(sorted(reasons.items())),
    )


# --------------------------------------------------------------------------- #
# Named policy entry points
# --------------------------------------------------------------------------- #
def accept_all(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
    **kwargs: Any,
) -> list[Decision]:
    kwargs["policy"] = "accept_all"
    return plan(tasks, estimates, **kwargs)


def reward_sort(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
    **kwargs: Any,
) -> list[Decision]:
    kwargs["policy"] = "reward_sort"
    return plan(tasks, estimates, **kwargs)


def value_cost_greedy(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
    **kwargs: Any,
) -> list[Decision]:
    kwargs["policy"] = "value_cost_greedy"
    return plan(tasks, estimates, **kwargs)


def knapsack_dp(
    tasks: Sequence[TaskLike],
    estimates: Mapping[str, CostEstimate] | Sequence[CostEstimate],
    **kwargs: Any,
) -> list[Decision]:
    kwargs["policy"] = "knapsack_dp"
    return plan(tasks, estimates, **kwargs)


POLICIES: Final[dict[str, Any]] = {
    "accept_all": accept_all,
    "reward_sort": reward_sort,
    "value_cost_greedy": value_cost_greedy,
    "knapsack_dp": knapsack_dp,
}
POLICY_NAMES: Final[tuple[str, ...]] = tuple(POLICIES)
