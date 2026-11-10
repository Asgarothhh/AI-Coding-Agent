"""Cheap heuristic planning and the budget-aware decision API.

Two layers live here:

* the legacy, token-free :func:`build_plan` / :func:`estimate_cost` helpers
  used by the runner for ordering and triage.  They remain unchanged for
  backward compatibility;
* the spec v5.0 decision planner from :mod:`harness.policy`, re-exported so
  callers can use one import site: ``Decision``, ``plan``, ``plan_entries``,
  ``replan``, the named policies and ``planning_reserve``.

The decision layer converts a :class:`~harness.estimator.CostEstimate` into
``v_i = reward_i * P_solve_i`` and ``c_i = upper_cost_i``, then respects the
single global budget plus a separate time limit.  It never calls an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import (
    ACTION_REASONS,
    DEFAULT_CONFIG,
    DEFAULT_POLICY,
    POLICIES,
    POLICY_NAMES,
    Decision,
    PlanEntry,
    PlanSummary,
    PolicyConfig,
    accept_all,
    estimate_time,
    knapsack_dp,
    plan,
    plan_entries,
    planning_reserve,
    replan,
    reward_sort,
    solve_probability,
    summarize,
    value_cost_greedy,
)
from .task import Task
from .triage import DEFAULT_THRESHOLD, TriageResult, triage

__all__ = [
    "ACTION_REASONS",
    "DEFAULT_CONFIG",
    "DEFAULT_POLICY",
    "DETERMINISTIC_TYPES",
    "POLICIES",
    "POLICY_NAMES",
    "Decision",
    "PlanEntry",
    "PlanItem",
    "PlanSummary",
    "PolicyConfig",
    "accept_all",
    "build_plan",
    "estimate_cost",
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

DETERMINISTIC_TYPES = frozenset({"math", "search", "data_analysis"})

_TYPE_BASE_COST: dict[str, float] = {
    "math": 40.0,
    "search": 60.0,
    "data_analysis": 80.0,
    "coding": 1800.0,
}


@dataclass(frozen=True, slots=True)
class PlanItem:
    task: Task
    estimated_cost: float
    priority: float
    deterministic: bool
    status: str  # "planned" | "skipped"
    reason: str
    triage: TriageResult

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task.task_id,
            "task_type": self.task.task_type,
            "reward": self.task.reward,
            "status": self.status,
            "deterministic": self.deterministic,
            "estimated_cost": round(self.estimated_cost, 3),
            "priority": round(self.priority, 6),
            "reason": self.reason,
            "triage": self.triage.as_dict(),
        }


def estimate_cost(task: Task) -> float:
    """Return a cheap heuristic estimate in challenge cost units."""

    base = _TYPE_BASE_COST.get(task.task_type, 600.0)
    instruction_factor = min(600.0, len(task.instruction) / 4.0)

    if task.task_type in DETERMINISTIC_TYPES:
        # Deterministic solvers should cost zero; the estimate reserves a
        # little room for a future LLM fallback without distorting priorities.
        return float(min(250.0, base + len(task.instruction) / 30.0))

    if task.task_type == "coding":
        workdir = task.workdir
        py_files = list(workdir.glob("*.py")) if workdir.is_dir() else []
        return float(min(3000.0, base + 120.0 * len(py_files) + instruction_factor))

    return float(min(3000.0, base + instruction_factor))


def build_plan(tasks: list[Task], threshold: float = DEFAULT_THRESHOLD) -> list[PlanItem]:
    items: list[PlanItem] = []
    for task in tasks:
        triage_result = triage(task, threshold)
        deterministic = task.task_type in DETERMINISTIC_TYPES
        estimated = estimate_cost(task)
        priority = task.reward / max(estimated, 1.0)

        if triage_result.infeasible:
            joined = "; ".join(triage_result.reasons) or "triage threshold reached"
            status = "skipped"
            reason = f"triage {triage_result.score:.2f} >= {threshold:.2f}: {joined}"
        else:
            status = "planned"
            kind = "deterministic" if deterministic else "llm"
            reason = f"{kind} solver; estimated {estimated:.0f} cost units"

        items.append(
            PlanItem(
                task=task,
                estimated_cost=estimated,
                priority=priority,
                deterministic=deterministic,
                status=status,
                reason=reason,
                triage=triage_result,
            )
        )

    # Planned first, deterministic first, then best reward/cost ratio.
    items.sort(
        key=lambda item: (
            item.status != "planned",
            not item.deterministic,
            -item.priority,
            item.task.task_id,
        )
    )
    return items
