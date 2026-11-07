#!/usr/bin/env python3
"""Entry point for the openJiuwen Challenge 2026 MVP harness.

Example::

    python run_agent.py --tasks-dir /data/tasks --offline
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from harness.llm import (
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    LLMClient,
    LLMConfig,
    build_llm_client,
)
from harness.mock_proxy import MockProxyServer
from harness.runner import RunOptions, run

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
    parser.add_argument("--budget", type=float, default=None, help="total cost budget (default: $BUDGET or 100000)")
    parser.add_argument("--reserve", type=float, default=0.0, help="budget reserve kept untouched (default: 0)")
    parser.add_argument("--per-task-cap", type=float, default=3000.0, help="per-task cost cap (default: 3000)")
    parser.add_argument("--max-workers", type=int, default=4, help="parallel solver workers, capped at 5 (default: 4)")
    parser.add_argument("--time-limit", type=float, default=3300.0, help="wall-clock limit in seconds (default: 3300)")
    parser.add_argument("--triage-threshold", type=float, default=0.6, help="infeasibility threshold in [0,1] (default: 0.6)")
    parser.add_argument("--max-iterations", type=int, default=6, help="max LLM repair iterations for coding (default: 6)")
    parser.add_argument("--mutation-repair", action="store_true", help="enable tiny deterministic mutation repair for coding")
    parser.add_argument("--test-timeout", type=float, default=60.0, help="per-test-file timeout in seconds (default: 60)")
    parser.add_argument("--max-tokens", type=int, default=None, help="maximum completion tokens per LLM call (default: $LLM_MAX_TOKENS or 1024)")
    parser.add_argument(
        "--llm-backend",
        choices=("auto", "stdlib", "openjiuwen"),
        default="auto",
        help="LLM transport backend: auto (default), stdlib urllib, or openjiuwen",
    )
    parser.add_argument("--model", default=None, help=f"default model (default: $LLM_MODEL or {DEFAULT_MODEL})")
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
    if not 0.0 <= args.triage_threshold <= 1.0:
        parser.error("--triage-threshold must be within [0, 1]")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    budget = args.budget if args.budget is not None else _env_float("BUDGET", 100_000.0)
    model = args.model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
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
            llm = build_llm_client(config, backend=args.llm_backend)
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
            reserve=args.reserve,
            per_task_cap=args.per_task_cap,
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
        )
        summary = run(options)
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
        if proxy is not None:
            proxy.stop()


if __name__ == "__main__":
    raise SystemExit(main())
