#!/usr/bin/env python3
"""Entry point for the openJiuwen Challenge 2026 MVP harness.

Example::

    python run_agent.py --tasks-dir /data/tasks --offline
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, cast

from harness.budget import BudgetConfigurationError, BudgetLedger, BudgetProvider
from harness.llm import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    LLMClient,
    LLMConfig,
    build_llm_client,
)
from harness.mock_proxy import MockProxyServer
from harness.model_router import select_model
from harness.policy import COST_BASES, POLICY_NAMES, CostBasis
from harness.runner import (
    DEFAULT_TIME_LIMIT,
    EXTERNAL_TIMEOUT_SECONDS,
    RunOptions,
    run,
)

__all__ = ["build_parser", "main"]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_agent.py",
        description="Budget-aware MVP harness for the openJiuwen Challenge 2026.",
    )
    parser.add_argument("--tasks-dir", required=True, help="directory containing <task_id>/task.json")
    parser.add_argument("--out", default=None, help="output directory or solutions.json path (default: $OUT or /tmp/openjiuwen-out)")
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="total cost budget; priority: --budget > $OJ_TOKEN_BUDGET > $BUDGET (required unless --offline)",
    )
    parser.add_argument("--reserve", type=float, default=0.0, help="budget reserve kept untouched (default: 0)")
    parser.add_argument(
        "--policy",
        choices=POLICY_NAMES,
        default="value_cost_greedy",
        help="task selection policy (default: value_cost_greedy)",
    )
    parser.add_argument(
        "--cost-basis",
        choices=COST_BASES,
        default="expected",
        help=(
            "planned cost basis: expected (default; rely on the ledger for "
            "hard safety) or upper (conservative upper-bound planning)"
        ),
    )
    parser.add_argument(
        "--planner-reserve",
        type=float,
        default=None,
        help=(
            "planning reserve: a fraction of the total budget in [0, 1], or "
            "absolute cost units when > 1; default: policy heuristic (5%%, "
            "or 10%% when mostly low-confidence)"
        ),
    )
    parser.add_argument("--max-workers", type=int, default=4, help="parallel solver workers, capped at 5 (default: 4)")
    parser.add_argument(
        "--time-limit",
        type=float,
        default=DEFAULT_TIME_LIMIT,
        help=f"wall-clock limit in seconds, below the 1-hour judge timeout (default: {DEFAULT_TIME_LIMIT:g})",
    )
    parser.add_argument("--triage-threshold", type=float, default=0.6, help="infeasibility threshold in [0,1] (default: 0.6)")
    parser.add_argument("--max-iterations", type=int, default=6, help="max LLM repair iterations for coding (default: 6)")
    parser.add_argument("--mutation-repair", action="store_true", help="enable tiny deterministic mutation repair for coding")
    parser.add_argument("--test-timeout", type=float, default=60.0, help="per-test-file timeout in seconds (default: 60)")
    parser.add_argument("--max-tokens", type=int, default=None, help="maximum completion tokens per LLM call (default: $LLM_MAX_TOKENS or 1024)")
    parser.add_argument(
        "--llm-backend",
        choices=("auto", "stdlib", "openjiuwen", "gateway"),
        default="auto",
        help=(
            "LLM transport backend: auto (default), stdlib urllib, openjiuwen, "
            "or gateway (the v5 production path through openJiuwen Core; "
            "never falls back to stdlib)"
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "default/fallback model (default: $LLM_MODEL or "
            f"{DEFAULT_MODEL}); with --model-policy metrics the measured "
            "stats file may select another allowlisted model"
        ),
    )
    parser.add_argument(
        "--model-policy",
        choices=("fixed", "metrics"),
        default="metrics",
        help=(
            "model selection policy: metrics (default; best measured "
            "score/cost from --model-stats) or fixed (always --model)"
        ),
    )
    parser.add_argument(
        "--model-stats",
        default="harness/model_stats.json",
        help=(
            "measured model stats JSON for --model-policy metrics "
            "(default: harness/model_stats.json)"
        ),
    )
    parser.add_argument("--escalation-model", default=None, help=f"escalation model (default: $LLM_ESCALATION_MODEL or {ESCALATION_MODEL})")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit without solving")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock-llm", action="store_true", help="start a local mock OpenAI-compatible proxy")
    mode.add_argument("--offline", action="store_true", help="disable all LLM calls (deterministic solvers only)")
    parser.add_argument("--verbose", "-v", action="store_true", help="verbose progress on stderr")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    tasks_dir = Path(args.tasks_dir)
    if not tasks_dir.exists():
        parser.error(f"--tasks-dir does not exist: {tasks_dir}")
    if not tasks_dir.is_dir():
        parser.error(f"--tasks-dir is not a directory: {tasks_dir}")
    if args.max_workers < 1:
        parser.error("--max-workers must be >= 1")
    if args.time_limit < 0:
        parser.error("--time-limit must be >= 0")
    if args.time_limit > EXTERNAL_TIMEOUT_SECONDS:
        parser.error(
            f"--time-limit must not exceed the external 1-hour timeout "
            f"({EXTERNAL_TIMEOUT_SECONDS:g}s)"
        )
    if not 0.0 <= args.triage_threshold <= 1.0:
        parser.error("--triage-threshold must be within [0, 1]")
    if args.planner_reserve is not None and (
        not math.isfinite(args.planner_reserve) or args.planner_reserve < 0.0
    ):
        parser.error("--planner-reserve must be a finite non-negative number")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        budget_decision = BudgetProvider(
            args.budget,
            offline=args.offline,
        ).resolve()
    except BudgetConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    budget = budget_decision.total
    print(
        f"budget: {budget:g} (source: {budget_decision.source})",
        file=sys.stderr,
    )
    try:
        ledger = BudgetLedger(budget, reserve=args.reserve)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    default_model = args.model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    selection = select_model(
        default_model,
        policy=args.model_policy,
        stats_path=args.model_stats,
    )
    model = selection.model
    print(f"model: {model} (policy={selection.policy}; {selection.reason})", file=sys.stderr)
    escalation_model = (
        args.escalation_model
        or os.environ.get("LLM_ESCALATION_MODEL")
        or ESCALATION_MODEL
    )
    max_tokens = (
        args.max_tokens
        if args.max_tokens is not None
        else int(_env_float("LLM_MAX_TOKENS", 1024))
    )
    if max_tokens < 1:
        parser.error("--max-tokens/LLM_MAX_TOKENS must be >= 1")
    proxy: MockProxyServer | None = None
    llm: Any = None
    chosen_backend = "offline" if args.offline else ("stdlib-mock" if args.mock_llm else args.llm_backend)

    try:
        if args.mock_llm:
            proxy = MockProxyServer(response_text="MOCK")
            proxy.start()
            llm = LLMClient(
                LLMConfig(
                    base_url=proxy.base_url,
                    api_key="mock-key",
                    model=model,
                    escalation_model=escalation_model,
                    max_tokens=max_tokens,
                    timeout=30.0,
                    max_retries=1,
                    backoff_base=0.05,
                )
            )
            chosen_backend = "stdlib-mock"
        elif not args.offline:
            config = LLMConfig.from_env(
                model=model,
                escalation_model=escalation_model,
                max_tokens=max_tokens,
            )
            llm = build_llm_client(config, backend=args.llm_backend, ledger=ledger)
            chosen_backend = getattr(llm, "backend_name", args.llm_backend)
            if not getattr(llm, "available", False):
                print(
                    "warning: LLM_BASE_URL/LLM_API_KEY are not set; coding tasks will fail without an LLM",
                    file=sys.stderr,
                )

        print(f"LLM backend: {chosen_backend}", file=sys.stderr)

        options = RunOptions(
            tasks_dir=tasks_dir,
            out=Path(args.out) if args.out else None,
            budget=budget,
            budget_source=budget_decision.source,
            reserve=args.reserve,
            max_workers=min(args.max_workers, 5),
            time_limit=args.time_limit,
            triage_threshold=args.triage_threshold,
            max_iterations=args.max_iterations,
            mutation_repair=args.mutation_repair,
            test_timeout=args.test_timeout,
            max_tokens=max_tokens,
            dry_run=args.dry_run,
            offline=args.offline,
            verbose=args.verbose,
            model=model,
            escalation_model=escalation_model,
            llm=llm,
            policy=args.policy,
            cost_basis=cast("CostBasis", args.cost_basis),
            planner_reserve=args.planner_reserve,
            ledger=ledger,
        )
        summary = run(options)
        stats_source = selection.stats.get("source")
        summary["model_selection"] = {
            "model": selection.model,
            "reason": selection.reason,
            "policy": selection.policy,
            "source": stats_source,
        }
        summary["llm_backend"] = chosen_backend
        summary["llm_backend_requested"] = args.llm_backend
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - CLI boundary
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.verbose:
            raise
        return 2
    finally:
        if llm is not None:
            close_llm = getattr(llm, "close", None)
            if callable(close_llm):
                try:
                    close_llm()
                except Exception as exc:  # pragma: no cover - shutdown best effort
                    logging.getLogger(__name__).warning("failed to close LLM client: %s", exc)
        if proxy is not None:
            proxy.stop()


if __name__ == "__main__":
    raise SystemExit(main())
