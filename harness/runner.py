"""Run orchestration: load -> estimate -> plan -> solve -> store -> trace.

Since v5.0 the runner uses the deterministic :mod:`harness.policy` decision
layer instead of executing every triage-feasible task:

1. every task is estimated once by :class:`harness.estimator.CostEstimator`;
2. :func:`harness.policy.plan_entries` selects a set under the single global
   budget (minus the 5-10% planning reserve) and the wall-clock limit;
3. only accepted tasks run, in policy order;
4. after every completed task the observed usage is fed back to the
   estimator, every not-yet-submitted task is re-estimated and the whole
   remaining set is replanned from a fresh
   :class:`harness.budget.BudgetSnapshot`, without mutating past decisions.

Tasks rejected by the initial plan stay in the ``remaining`` set, so they can
become accepted once actual usage lowers the estimates. Only tasks that are
still rejected when the run ends are persisted as ``skipped`` with the
canonical ``reject:cost`` / ``reject:impossible`` / ``reject:time`` reason;
they never get an ``answer.txt``. Every task is finalised exactly once.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent import futures
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .budget import Budget, BudgetExhausted, BudgetLedger, cost_of
from .estimator import CostEstimate, CostEstimator
from .llm import DEFAULT_MODEL, ESCALATION_MODEL, LLMClient, LLMConfig
from .planner import PlanItem, build_plan
from .policy import (
    DEFAULT_POLICY,
    CostBasis,
    Decision,
    PolicyConfig,
    TaskLike,
    plan_entries,
    planning_reserve,
    replan,
    summarize,
)
from .solvers.base import SolverContext, SolveResult, SolverOptions
from .solvers.coding import solve as solve_coding
from .solvers.data_analysis import solve as solve_data_analysis
from .solvers.generic import solve as solve_generic
from .solvers.math import solve as solve_math
from .solvers.search import solve as solve_search
from .store import AnswerStore, estimate_theory
from .task import Task, load_tasks
from .trace import TraceWriter

__all__ = [
    "DEFAULT_TIME_LIMIT",
    "EXTERNAL_TIMEOUT_SECONDS",
    "RunOptions",
    "run",
]

log = logging.getLogger(__name__)

_SOLVERS: dict[str, Callable[[Task, SolverContext], SolveResult]] = {
    "math": solve_math,
    "search": solve_search,
    "data_analysis": solve_data_analysis,
    "coding": solve_coding,
}

MAX_PARALLEL_SESSIONS = 5

# The external judge kills the container after one hour.  Keep the default
# deadline below that so the process stops starting new tasks first.
EXTERNAL_TIMEOUT_SECONDS = 3_600.0
DEFAULT_TIME_LIMIT = 3_300.0


@dataclass(slots=True)
class RunOptions:
    tasks_dir: Path
    out: Path | None = None
    budget: float = 100_000.0
    budget_source: str = "runner-default"
    reserve: float = 0.0
    max_workers: int = 4
    time_limit: float = DEFAULT_TIME_LIMIT
    triage_threshold: float = 0.6
    max_iterations: int = 6
    mutation_repair: bool = False
    test_timeout: float = 60.0
    max_tokens: int = 1024
    dry_run: bool = False
    offline: bool = False
    verbose: bool = False
    model: str = DEFAULT_MODEL
    escalation_model: str = ESCALATION_MODEL
    llm: LLMClient | None = None
    policy: str = DEFAULT_POLICY
    # Programmatic callers keep the conservative upper basis unless they opt
    # in; the CLI and ``PolicyConfig`` default to ``expected``.
    cost_basis: CostBasis = "upper"
    planner_reserve: float | None = None
    ledger: BudgetLedger | None = None
    estimator: CostEstimator | None = None


class _GatewaySolverLedger:
    """Read-through ledger view for solvers when the gateway owns accounting.

    :class:`harness.openjiuwen_gateway.GatewayChatAdapter` reserves and settles
    every call on the shared ledger before returning.  The coding solver also
    reports ``response.usage`` back through ``Budget.charge``; without this
    view the same tokens would be charged twice.  ``charge`` therefore returns
    the cost for per-task reporting while leaving the ledger untouched.
    """

    def __init__(self, ledger: BudgetLedger) -> None:
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ledger, name)

    def charge(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        task_id: str | None = None,
    ) -> float:
        del task_id
        return cost_of(int(prompt_tokens), int(completion_tokens))


def _new_type_stats() -> dict[str, Any]:
    return {
        "total": 0,
        "planned": 0,
        "solved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": 0,
        "tokens": 0.0,
        "reward_solved": 0.0,
    }


def _build_llm(options: RunOptions) -> LLMClient | None:
    if options.offline:
        return None
    if options.llm is not None:
        return options.llm
    client = LLMClient(
        LLMConfig.from_env(
            model=options.model,
            escalation_model=options.escalation_model,
            max_tokens=options.max_tokens,
        )
    )
    if not client.available:
        log.warning("LLM_BASE_URL is not set; LLM-dependent tasks will fail")
    return client


def _count(duration: float) -> float:
    return round(float(duration), 6)


def _solver_name(task: Task) -> str:
    """Estimator solver key for a task type (unknown types use generic)."""

    return task.task_type if task.task_type in _SOLVERS else "generic"


def _policy_config(options: RunOptions, total_budget: float) -> PolicyConfig:
    if options.planner_reserve is None:
        return PolicyConfig(cost_basis=options.cost_basis)
    value = float(options.planner_reserve)
    # ``--planner-reserve`` accepts a fraction of the total budget (the
    # documented 5-10% form).  A value above 1 is interpreted as absolute cost
    # units and converted to a rate, so both spellings are unambiguous.
    if value <= 1.0:
        rate = value
    else:
        rate = min(1.0, value / total_budget) if total_budget > 0 else 1.0
    return PolicyConfig(
        planning_reserve_rate=rate,
        low_confidence_reserve_rate=rate,
        cost_basis=options.cost_basis,
    )


def _estimate_tasks(
    tasks: list[Task],
    estimator: Any,
    model: str,
) -> dict[str, CostEstimate]:
    estimates: dict[str, CostEstimate] = {}
    for task in tasks:
        estimates[task.task_id] = estimator.estimate(task, _solver_name(task), model)
    return estimates


def _record_predictions(
    tasks: list[Task],
    estimates: dict[str, CostEstimate],
    store: AnswerStore,
    model: str,
) -> int:
    """Write one ``predictions.jsonl`` line per task after the initial estimates.

    Called once, before planning, so replans and per-iteration re-estimation do
    not create duplicates.  This also runs for ``--dry-run`` when ``$OUT`` is
    writable: the plan predictions are diagnostic and never touch
    ``answer.txt``.  A read-only ``$OUT`` is logged and ignored instead of
    failing the run.
    """

    written = 0
    for task in tasks:
        estimate = estimates.get(task.task_id)
        if estimate is None:
            continue
        try:
            store.record_prediction(task, estimate, model, estimate.solver)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never kill a run
            log.warning(
                "failed to write predictions to %s (%s); diagnostics only",
                store.predictions_path,
                type(exc).__name__,
            )
            break
        written += 1
    return written


def _observe_result(
    estimator: Any,
    task: Task,
    result: SolveResult,
    model: str,
) -> None:
    """Feed one completed task's usage back to the estimator.

    ``SolveResult.tokens`` is already expressed in challenge cost units, so it
    is passed as the output side of a synthetic usage record: the estimator
    only consumes the resulting total ratio.  Fake estimators used by tests are
    allowed to implement ``observe`` without the full ``CostEstimator`` API;
    an estimator without it is simply left untouched.
    """

    observe = getattr(estimator, "observe", None)
    if not callable(observe):
        return
    try:
        observe(
            task,
            _solver_name(task),
            model,
            {"input_tokens": 0, "output_tokens": float(result.tokens)},
        )
    except Exception:  # pragma: no cover - an observation must never kill a run
        log.exception("estimator.observe failed for %s", task.task_id)


def _initial_time_left(options: RunOptions) -> float | None:
    """Time budget for the first plan.

    A positive deadline is enforced while submitting tasks: pre-rejecting every
    task because the heuristic time estimate exceeds a tiny remaining window
    would prevent even the first attempt.  The degenerate ``--time-limit 0``
    case *is* a planning rejection and must produce ``reject:time`` for every
    task.
    """

    return 0.0 if options.time_limit <= 0 else None


def _plan_items(
    entries: list[Any],
    legacy_by_id: dict[str, PlanItem],
) -> list[PlanItem]:
    items: list[PlanItem] = []
    for entry in entries:
        task = entry.task
        legacy = legacy_by_id[task.task_id]
        accepted = entry.decision.accepted
        status = "planned" if accepted else "skipped"
        if accepted:
            reason = legacy.reason
        else:
            reason = entry.decision.reason
        estimated = float(entry.estimate.upper_cost)
        priority = (float(entry.value) / estimated) if estimated > 0.0 else 0.0
        items.append(
            PlanItem(
                task=task,
                estimated_cost=estimated,
                priority=priority,
                deterministic=legacy.deterministic,
                status=status,
                reason=reason,
                triage=legacy.triage,
            )
        )
    return items


def _planner_stats(
    entries: list[Any],
    *,
    policy: str,
    config: PolicyConfig,
    total_budget: float,
) -> dict[str, Any]:
    summary = summarize(entries, policy=policy)
    stats = summary.as_dict()
    stats["rejected"] = summary.total - summary.accepted
    feasible = [entry.estimate for entry in entries if entry.status != "impossible"]
    stats["reserve"] = round(
        planning_reserve(total_budget, feasible, config=config), 6
    )
    stats["cost_basis"] = config.cost_basis
    return stats


def _estimator_stats(
    estimates: dict[str, CostEstimate],
    model: str,
) -> dict[str, Any]:
    variants = {"deterministic": 0, "heuristic": 0, "calibration": 0}
    confidence = {"high": 0, "medium": 0, "low": 0}
    for estimate in estimates.values():
        confidence[estimate.confidence] = confidence.get(estimate.confidence, 0) + 1
        drivers = estimate.drivers
        if "deterministic_solver=true" in drivers:
            variants["deterministic"] += 1
        elif any(driver.startswith("scope=heuristic") for driver in drivers):
            variants["heuristic"] += 1
        else:
            variants["calibration"] += 1
    return {
        "model": model,
        "total": len(estimates),
        "variant": variants,
        "confidence": confidence,
    }


def run(options: RunOptions) -> dict[str, Any]:
    started = time.monotonic()
    tasks = load_tasks(options.tasks_dir)
    store = AnswerStore(options.out, verbose=options.verbose)
    trace = TraceWriter(store.trace_path, verbose=options.verbose)
    ledger: BudgetLedger = options.ledger or BudgetLedger(
        options.budget,
        reserve=options.reserve,
    )
    llm = _build_llm(options)
    gateway_accounting = (
        llm is not None and getattr(llm, "backend_name", "") == "openjiuwen-gateway"
    )
    solver_budget: Any = _GatewaySolverLedger(ledger) if gateway_accounting else ledger
    estimator = options.estimator or CostEstimator()

    # The legacy planner supplies triage metadata and the infeasible set; the
    # policy layer below turns estimates into the actual execution order.
    legacy_plan = build_plan(tasks, options.triage_threshold)
    legacy_by_id = {item.task.task_id: item for item in legacy_plan}
    impossible_ids = {
        item.task.task_id for item in legacy_plan if item.status == "skipped"
    }

    estimates = _estimate_tasks(tasks, estimator, options.model)
    _record_predictions(tasks, estimates, store, options.model)
    policy_config = _policy_config(options, ledger.total)
    entries = plan_entries(
        cast("list[TaskLike]", tasks),
        estimates,
        policy=options.policy,
        remaining=ledger.remaining,
        total_budget=ledger.total,
        time_left=_initial_time_left(options),
        config=policy_config,
        impossible_ids=impossible_ids,
        triage_threshold=options.triage_threshold,
    )
    plan = _plan_items(entries, legacy_by_id)
    planner = _planner_stats(
        entries,
        policy=options.policy,
        config=policy_config,
        total_budget=ledger.total,
    )
    estimator_stats = _estimator_stats(estimates, options.model)
    replans = 0

    # Initial decisions are frozen for the planner statistics.  ``latest`` is
    # only updated for tasks that are still unsubmitted, so the final summary
    # can report how the decision set evolved without rewriting history.
    initial_decisions: dict[str, Decision] = {
        entry.decision.task_id: entry.decision for entry in entries
    }
    latest_decisions: dict[str, Decision] = dict(initial_decisions)
    initial_rejected = {
        task_id
        for task_id, decision in initial_decisions.items()
        if not decision.accepted
    }
    reconsidered_ids: set[str] = set()

    if options.verbose:
        _print_plan(plan)

    infeasible = len(impossible_ids)
    planned = sum(1 for item in plan if item.status == "planned")

    if options.dry_run:
        return _dry_run_summary(
            tasks=tasks,
            plan=plan,
            store=store,
            budget=ledger,
            options=options,
            infeasible=infeasible,
            planned=planned,
            duration=time.monotonic() - started,
            planner=planner,
            estimator=estimator_stats,
            replans=replans,
            reconsidered=0,
            final_accepted=planned,
        )

    max_workers = max(1, min(int(options.max_workers), MAX_PARALLEL_SESSIONS))
    solver_options = SolverOptions(
        max_iterations=options.max_iterations,
        mutation_repair=options.mutation_repair,
        test_timeout=options.test_timeout,
        max_tokens=options.max_tokens,
        verbose=options.verbose,
    )

    results: dict[str, dict[str, Any]] = {}
    results_lock = threading.Lock()
    finalized: set[str] = set()

    def finalize(
        item: PlanItem,
        result: SolveResult,
        status: str,
        duration: float,
        *,
        error: str | None = None,
    ) -> None:
        task = item.task
        with results_lock:
            if task.task_id in finalized:
                return
            finalized.add(task.task_id)
        try:
            record = store.record_solution(task, result, status, duration, error=error)
        except Exception as exc:  # pragma: no cover - disk failure
            log.exception("failed to persist solution for %s", task.task_id)
            record = {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "reward": task.reward,
                "status": status,
                "solver": result.solver,
                "answer": result.answer,
                "tokens": result.tokens,
                "duration": _count(duration),
                "notes": result.notes,
                "error": error or f"persist error: {exc}",
            }
        try:
            estimate = estimates.get(task.task_id)
            trace.write(
                task_id=task.task_id,
                task_type=task.task_type,
                reward=task.reward,
                status=status,
                solver=result.solver,
                tokens=result.tokens,
                duration=duration,
                error=error,
                notes=result.notes,
                y_hat=round(float(estimate.expected_cost), 6) if estimate is not None else 0.0,
                y_actual=_count(result.tokens),
                theory=estimate_theory(estimate) if estimate is not None else "unknown",
                estimator_confidence=(
                    estimate.confidence if estimate is not None else "unknown"
                ),
                model=options.model,
            )
        except Exception:  # pragma: no cover - disk failure
            log.exception("failed to write trace for %s", task.task_id)

        with results_lock:
            results[task.task_id] = record

    def run_one(item: PlanItem) -> None:
        task = item.task
        task_started = time.monotonic()
        elapsed_total = time.monotonic() - started
        if options.time_limit > 0 and elapsed_total >= options.time_limit:
            finalize(item, SolveResult(None, 0.0, "", "reject:time"), "skipped", 0.0)
            return
        if not ledger.can_start_task():
            finalize(item, SolveResult(None, 0.0, "", "skipped: budget exhausted"), "skipped", 0.0)
            return

        solver = _SOLVERS.get(task.task_type, solve_generic)
        error: str | None = None
        context = SolverContext(budget=solver_budget, llm=llm, options=solver_options)
        try:
            result = solver(task, context)
        except BudgetExhausted as exc:
            result = SolveResult(None, 0.0, task.task_type, f"budget stop: {exc}")
        except Exception as exc:
            log.exception("solver crashed for %s", task.task_id)
            error = f"{type(exc).__name__}: {exc}"
            result = SolveResult(None, 0.0, task.task_type, f"solver error: {error}")

        # Feed the actual usage back before the completion is observed by the
        # scheduler, so the next replan uses the adapted multiplier.
        _observe_result(estimator, task, result, options.model)

        if result.answer is not None:
            try:
                store.write_answer(task, result.answer)
                status = "solved"
            except Exception as exc:
                log.exception("failed to write answer for %s", task.task_id)
                error = f"answer write failed: {type(exc).__name__}: {exc}"
                status = "error"
        elif result.notes.startswith(("budget stop:", "skipped:")):
            status = "skipped"
        else:
            status = "failed"

        finalize(item, result, status, time.monotonic() - task_started, error=error)

    # ``remaining`` holds every task that has not been submitted yet, whether
    # it was accepted by the initial plan or rejected.  ``queue`` is the
    # runnable subset in policy order.  A submitted task is removed from both,
    # so a completed or running task can never be resubmitted.
    remaining: dict[str, PlanItem] = {item.task.task_id: item for item in plan}
    queue: deque[PlanItem] = deque(item for item in plan if item.status == "planned")
    pending: dict[futures.Future[None], PlanItem] = {}
    stop_reason: str | None = "time_limit" if options.time_limit <= 0 else None

    def requeue() -> None:
        """Re-estimate and replan every not-yet-submitted task.

        The estimator is the only source of the in-memory ``observe``
        correction, so every remaining task is estimated again before the
        whole remaining set is handed to :func:`harness.policy.replan`.  Tasks
        that were rejected initially stay in ``remaining`` and may now become
        accepted.  Submitted tasks are already past the decision point and are
        never cancelled, mutated or resubmitted; the ledger snapshot is the
        only source of truth for the remaining budget, and past ``Decision``
        objects stay frozen.
        """

        nonlocal replans, stop_reason
        if not remaining:
            return
        remaining_items = list(remaining.values())
        remaining_tasks = [item.task for item in remaining_items]
        for item in remaining_items:
            estimates[item.task.task_id] = estimator.estimate(
                item.task,
                _solver_name(item.task),
                options.model,
            )
        remaining_estimates = {
            item.task.task_id: estimates[item.task.task_id]
            for item in remaining_items
        }
        if stop_reason is None and not ledger.can_start_task():
            stop_reason = "budget"
        # A response that was already paid for may push the ledger below zero.
        # The policy requires a non-negative remainder, so clamp it for the
        # audit snapshot instead of crashing the run at the deadline.
        snapshot = ledger.snapshot()
        if snapshot.remaining < 0.0:
            ledger_snapshot: Any = {
                "total": snapshot.total,
                "spent": snapshot.spent,
                "reserved": snapshot.reserved,
                "remaining": 0.0,
                "prompt_tokens": snapshot.prompt_tokens,
                "completion_tokens": snapshot.completion_tokens,
                "stopped": snapshot.stopped,
            }
        else:
            ledger_snapshot = snapshot
        # While a deadline is still live the pool enforces it on submission;
        # the heuristic time estimate must not pre-empt that real enforcement
        # (it would reject every LLM task on a short test window).  Once the
        # deadline has passed, remaining time is zero and remaining tasks
        # become explicit ``reject:time`` decisions.
        if options.time_limit > 0:
            time_remaining = options.time_limit - (time.monotonic() - started)
            time_left: float | None = 0.0 if time_remaining <= 0 else None
            if time_left == 0.0 and stop_reason is None:
                stop_reason = "time_limit"
        else:
            time_left = None
        decisions = replan(
            cast("list[TaskLike]", remaining_tasks),
            remaining_estimates,
            ledger_snapshot,
            time_left,
            policy=options.policy,
            total_budget=ledger.total,
            config=policy_config,
            impossible_ids=impossible_ids,
            triage_threshold=options.triage_threshold,
        )
        replans += 1
        item_by_id = {item.task.task_id: item for item in remaining_items}
        accepted_ids = [decision.task_id for decision in decisions if decision.accepted]
        reconsidered_ids.update(
            task_id for task_id in accepted_ids if task_id in initial_rejected
        )
        queue.clear()
        queue.extend(
            item_by_id[task_id]
            for task_id in accepted_ids
            if task_id in item_by_id
        )

        # Keep the next replan input in decision order (accepted first in
        # policy order, rejected after it in stable input order).  Rejections
        # are not finalised here: they remain candidates for a later replan.
        ordered: list[PlanItem] = []
        decided: set[str] = set()
        for decision in decisions:
            ordered_item = item_by_id.get(decision.task_id)
            if ordered_item is None:
                continue
            ordered.append(ordered_item)
            decided.add(decision.task_id)
            latest_decisions[decision.task_id] = decision
        for pending_item in remaining_items:
            if pending_item.task.task_id not in decided:
                ordered.append(pending_item)
        remaining.clear()
        remaining.update((item.task.task_id, item) for item in ordered)

    with futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent") as pool:
        while queue or pending:
            while queue and len(pending) < max_workers:
                if options.time_limit > 0 and (time.monotonic() - started) >= options.time_limit:
                    stop_reason = "time_limit"
                    break
                if not ledger.can_start_task():
                    stop_reason = "budget"
                    break
                item = queue.popleft()
                remaining.pop(item.task.task_id, None)
                pending[pool.submit(run_one, item)] = item

            if not pending:
                if queue and stop_reason is None:  # pragma: no cover - defensive
                    stop_reason = "no_workers"
                break

            timeout = 0.5
            if options.time_limit > 0:
                time_remaining = options.time_limit - (time.monotonic() - started)
                if time_remaining <= 0:
                    stop_reason = "time_limit"
                timeout = max(0.05, min(0.5, time_remaining))
            done, _ = futures.wait(pending, timeout=timeout, return_when=futures.FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                try:
                    future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("worker failed for %s", item.task.task_id)
                    finalize(
                        item,
                        SolveResult(None, 0.0, "", f"worker error: {exc}"),
                        "error",
                        0.0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                # Replan after each completed task, using the fresh ledger.
                requeue()

    # Anything still in the runnable queue is also part of ``remaining`` by
    # construction; folding it back keeps the finalisation loop total even if
    # a future caller ever changes that invariant.
    for item in queue:
        remaining.setdefault(item.task.task_id, item)
    queue.clear()

    # Every unsubmitted task is finalised exactly once.  A task whose latest
    # decision is a rejection keeps the canonical ``reject:*`` reason; an
    # accepted task that never started is recorded as skipped by the stop
    # reason, preserving the previous behaviour.
    for item in remaining.values():
        decision = latest_decisions.get(item.task.task_id)
        if decision is not None and not decision.accepted:
            reason = decision.reason
        else:
            reason = f"skipped: not started ({stop_reason or 'run stopped'})"
        finalize(
            item,
            SolveResult(None, 0.0, "", reason),
            "skipped",
            0.0,
        )

    final_accepted_ids = {
        task_id
        for task_id, decision in latest_decisions.items()
        if decision.accepted
    }

    return _solve_summary(
        tasks=tasks,
        plan=plan,
        results=results,
        store=store,
        budget=ledger,
        options=options,
        llm=llm,
        infeasible=infeasible,
        planned=planned,
        stop_reason=stop_reason,
        duration=time.monotonic() - started,
        planner=planner,
        estimator=estimator_stats,
        replans=replans,
        reconsidered=len(reconsidered_ids),
        final_accepted=len(final_accepted_ids),
    )


def _base_summary(
    *,
    tasks: list[Task],
    plan: list[PlanItem],
    store: AnswerStore,
    budget: Budget,
    options: RunOptions,
    infeasible: int,
    planned: int,
    duration: float,
    planner: dict[str, Any],
    estimator: dict[str, Any],
) -> dict[str, Any]:
    return {
        "dry_run": options.dry_run,
        "offline": options.offline,
        "tasks_dir": str(options.tasks_dir),
        "out": str(store.out_dir),
        "solutions_path": str(store.solutions_path),
        "trace_path": str(store.trace_path),
        "predictions_path": str(store.predictions_path),
        "tasks": len(tasks),
        "planned": planned,
        "infeasible": infeasible,
        "policy": options.policy,
        "cost_basis": options.cost_basis,
        "planner": planner,
        "estimator": estimator,
        "budget_source": options.budget_source,
        "budget": asdict(budget.snapshot()),
        "duration": _count(duration),
    }


def _dry_run_summary(
    *,
    tasks: list[Task],
    plan: list[PlanItem],
    store: AnswerStore,
    budget: Budget,
    options: RunOptions,
    infeasible: int,
    planned: int,
    duration: float,
    planner: dict[str, Any],
    estimator: dict[str, Any],
    replans: int,
    reconsidered: int,
    final_accepted: int,
) -> dict[str, Any]:
    per_type: dict[str, dict[str, Any]] = {}
    for item in plan:
        stats = per_type.setdefault(item.task.task_type, _new_type_stats())
        stats["total"] += 1
        if item.status == "planned":
            stats["planned"] += 1
        else:
            stats["skipped"] += 1

    summary = _base_summary(
        tasks=tasks,
        plan=plan,
        store=store,
        budget=budget,
        options=options,
        infeasible=infeasible,
        planned=planned,
        duration=duration,
        planner=planner,
        estimator=estimator,
    )
    summary.update(
        {
            "solved": 0,
            "skipped": sum(1 for item in plan if item.status == "skipped"),
            "failed": 0,
            "errors": 0,
            "score": 0.0,
            "tokens_spent": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "stop_reason": None,
            "replans": replans,
            "reconsidered": reconsidered,
            "final_accepted": final_accepted,
            "per_type": per_type,
            "plan": [item.as_dict() for item in plan],
        }
    )
    return summary


def _solve_summary(
    *,
    tasks: list[Task],
    plan: list[PlanItem],
    results: dict[str, dict[str, Any]],
    store: AnswerStore,
    budget: Budget,
    options: RunOptions,
    llm: LLMClient | None,
    infeasible: int,
    planned: int,
    stop_reason: str | None,
    duration: float,
    planner: dict[str, Any],
    estimator: dict[str, Any],
    replans: int,
    reconsidered: int,
    final_accepted: int,
) -> dict[str, Any]:
    per_type: dict[str, dict[str, Any]] = {}
    for task in tasks:
        per_type.setdefault(task.task_type, _new_type_stats())["total"] += 1
    for item in plan:
        per_type.setdefault(item.task.task_type, _new_type_stats())["planned"] += 1

    solved = skipped = failed = errors = 0
    score = 0.0
    for record in results.values():
        stats = per_type.setdefault(record["task_type"], _new_type_stats())
        stats["tokens"] += float(record.get("tokens", 0.0) or 0.0)
        status = record.get("status")
        if status == "solved":
            solved += 1
            stats["solved"] += 1
            score += float(record.get("reward", 0.0) or 0.0)
            stats["reward_solved"] += float(record.get("reward", 0.0) or 0.0)
        elif status == "skipped":
            skipped += 1
            stats["skipped"] += 1
        elif status == "error":
            failed += 1
            errors += 1
            stats["failed"] += 1
            stats["errors"] += 1
        else:
            failed += 1
            stats["failed"] += 1

    snapshot = budget.snapshot()
    summary = _base_summary(
        tasks=tasks,
        plan=plan,
        store=store,
        budget=budget,
        options=options,
        infeasible=infeasible,
        planned=planned,
        duration=duration,
        planner=planner,
        estimator=estimator,
    )
    summary.update(
        {
            "solved": solved,
            "skipped": skipped,
            "failed": failed,
            "errors": errors,
            "score": round(score, 6),
            "tokens_spent": round(snapshot.spent, 6),
            "prompt_tokens": snapshot.prompt_tokens,
            "completion_tokens": snapshot.completion_tokens,
            "model": llm.config.model if llm is not None else None,
            "stop_reason": stop_reason,
            "replans": replans,
            "reconsidered": reconsidered,
            "final_accepted": final_accepted,
            "per_type": per_type,
            "results": [results[key] for key in sorted(results)],
        }
    )
    return summary


def _print_plan(plan: list[PlanItem]) -> None:
    print("plan (deterministic first, skipped last):", file=sys.stderr)
    for index, item in enumerate(plan, start=1):
        print(
            f"  {index:>3}. {item.task.task_id:<20} type={item.task.task_type:<14} "
            f"reward={item.task.reward:<5} est={item.estimated_cost:<7.0f} "
            f"priority={item.priority:.4f} status={item.status} :: {item.reason}",
            file=sys.stderr,
        )
