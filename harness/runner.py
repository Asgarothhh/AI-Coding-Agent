"""Run orchestration: load -> triage -> plan -> solve -> store -> trace."""

from __future__ import annotations

import concurrent.futures as futures
import logging
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .budget import Budget, BudgetExhausted
from .llm import DEFAULT_MODEL, ESCALATION_MODEL, LLMClient, LLMConfig
from .planner import PlanItem, build_plan
from .solvers.base import SolveResult, SolverContext, SolverOptions
from .solvers.coding import solve as solve_coding
from .solvers.data_analysis import solve as solve_data_analysis
from .solvers.math import solve as solve_math
from .solvers.search import solve as solve_search
from .store import AnswerStore
from .task import Task, load_tasks
from .trace import TraceWriter

__all__ = ["RunOptions", "run"]

log = logging.getLogger(__name__)

_SOLVERS: dict[str, Callable[[Task, SolverContext], SolveResult]] = {
    "math": solve_math,
    "search": solve_search,
    "data_analysis": solve_data_analysis,
    "coding": solve_coding,
}

MAX_PARALLEL_SESSIONS = 5


@dataclass(slots=True)
class RunOptions:
    tasks_dir: Path
    out: Path | None = None
    budget: float = 100_000.0
    reserve: float = 0.0
    per_task_cap: float = 3_000.0
    max_workers: int = 4
    time_limit: float = 3_300.0
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


def run(options: RunOptions) -> dict[str, Any]:
    started = time.monotonic()
    tasks = load_tasks(options.tasks_dir)
    plan = build_plan(tasks, options.triage_threshold)
    store = AnswerStore(options.out, verbose=options.verbose)
    trace = TraceWriter(store.trace_path, verbose=options.verbose)
    budget = Budget(
        options.budget,
        reserve=options.reserve,
        per_task_cap=options.per_task_cap,
    )
    llm = _build_llm(options)

    if options.verbose:
        _print_plan(plan)

    infeasible = sum(1 for item in plan if item.status == "skipped")
    planned = sum(1 for item in plan if item.status == "planned")

    if options.dry_run:
        return _dry_run_summary(
            tasks=tasks,
            plan=plan,
            store=store,
            budget=budget,
            options=options,
            infeasible=infeasible,
            planned=planned,
            duration=time.monotonic() - started,
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

    def finalize(
        item: PlanItem,
        result: SolveResult,
        status: str,
        duration: float,
        *,
        error: str | None = None,
    ) -> None:
        task = item.task
        try:
            record = store.record_solution(task, result, status, duration, error=error)
        except Exception as exc:  # pragma: no cover - disk failure
            log.exception("failed to persist solution for %s: %s", task.task_id, exc)
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
            )
        except Exception as exc:  # pragma: no cover - disk failure
            log.exception("failed to write trace for %s: %s", task.task_id, exc)

        with results_lock:
            results[task.task_id] = record

    # Record triage skips before starting the pool.
    for item in plan:
        if item.status == "skipped":
            finalize(
                item,
                SolveResult(None, 0.0, "", item.reason),
                "skipped",
                0.0,
            )

    def run_one(item: PlanItem) -> None:
        task = item.task
        task_started = time.monotonic()
        elapsed_total = time.monotonic() - started
        if options.time_limit > 0 and elapsed_total >= options.time_limit:
            finalize(item, SolveResult(None, 0.0, "", "skipped: time limit reached"), "skipped", 0.0)
            return
        if not budget.can_start_task():
            finalize(item, SolveResult(None, 0.0, "", "skipped: budget exhausted"), "skipped", 0.0)
            return

        solver = _SOLVERS.get(task.task_type)
        error: str | None = None
        if solver is None:
            result = SolveResult(None, 0.0, "", f"no solver registered for task type {task.task_type!r}")
        else:
            context = SolverContext(budget=budget, llm=llm, options=solver_options)
            try:
                result = solver(task, context)
            except BudgetExhausted as exc:
                result = SolveResult(None, 0.0, task.task_type, f"budget stop: {exc}")
            except Exception as exc:
                log.exception("solver crashed for %s", task.task_id)
                error = f"{type(exc).__name__}: {exc}"
                result = SolveResult(None, 0.0, task.task_type, f"solver error: {error}")

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

    queue: deque[PlanItem] = deque(item for item in plan if item.status == "planned")
    pending: dict[futures.Future[None], PlanItem] = {}
    stop_reason: str | None = None

    with futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent") as pool:
        while queue or pending:
            while queue and len(pending) < max_workers:
                if options.time_limit > 0 and (time.monotonic() - started) >= options.time_limit:
                    stop_reason = "time_limit"
                    break
                if not budget.can_start_task():
                    stop_reason = "budget"
                    break
                item = queue.popleft()
                pending[pool.submit(run_one, item)] = item

            if not pending:
                if queue and stop_reason is None:  # pragma: no cover - defensive
                    stop_reason = "no_workers"
                break

            timeout = 0.5
            if options.time_limit > 0:
                remaining = options.time_limit - (time.monotonic() - started)
                if remaining <= 0:
                    stop_reason = "time_limit"
                timeout = max(0.05, min(0.5, remaining))
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

    while queue:
        item = queue.popleft()
        reason = f"skipped: not started ({stop_reason or 'run stopped'})"
        finalize(item, SolveResult(None, 0.0, "", reason), "skipped", 0.0)

    return _solve_summary(
        tasks=tasks,
        plan=plan,
        results=results,
        store=store,
        budget=budget,
        options=options,
        llm=llm,
        infeasible=infeasible,
        planned=planned,
        stop_reason=stop_reason,
        duration=time.monotonic() - started,
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
) -> dict[str, Any]:
    return {
        "dry_run": options.dry_run,
        "offline": options.offline,
        "tasks_dir": str(options.tasks_dir),
        "out": str(store.out_dir),
        "solutions_path": str(store.solutions_path),
        "trace_path": str(store.trace_path),
        "tasks": len(tasks),
        "planned": planned,
        "infeasible": infeasible,
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
    )
    summary.update(
        {
            "solved": 0,
            "skipped": infeasible,
            "failed": 0,
            "errors": 0,
            "score": 0.0,
            "tokens_spent": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "stop_reason": None,
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
