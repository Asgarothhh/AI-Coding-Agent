"""Unit tests for the global budget ledger and budget resolution.

Contract highlights:

* there is exactly one global budget and no per-task cap;
* ``reserve`` checks and deducts atomically;
* ``settle`` releases the reserve and charges actual usage, even when it is
  larger than the estimate;
* ``release`` restores capacity and double settle/release is an error.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import run_agent
from harness.budget import (
    DEFAULT_OFFLINE_BUDGET,
    Budget,
    BudgetConfigurationError,
    BudgetError,
    BudgetExhausted,
    BudgetLedger,
    BudgetProvider,
    BudgetSnapshot,
    Reservation,
    cost_of,
)
from harness.runner import (
    DEFAULT_TIME_LIMIT,
    EXTERNAL_TIMEOUT_SECONDS,
    RunOptions,
)
from harness.usage import TokenUsage, normalize_usage
from tests.support import make_task


class CostFormulaTests(unittest.TestCase):
    def test_cost_formula(self) -> None:
        self.assertEqual(cost_of(100, 50), 60.0)
        self.assertEqual(cost_of(0, 0), 0.0)
        self.assertAlmostEqual(cost_of(1, 1), 1.1)
        with self.assertRaises(ValueError):
            cost_of(-1, 0)
        with self.assertRaises(ValueError):
            cost_of(0, -1)


class NoPerTaskBudgetTests(unittest.TestCase):
    def test_per_task_cap_option_is_gone(self) -> None:
        with self.assertRaises(TypeError):
            Budget(10.0, per_task_cap=1.0)  # type: ignore[call-arg]

    def test_snapshot_has_no_per_task_fields(self) -> None:
        ledger = BudgetLedger(100.0)
        snapshot = ledger.snapshot()
        self.assertIsInstance(snapshot, BudgetSnapshot)
        payload = {
            "total": snapshot.total,
            "spent": snapshot.spent,
            "reserved": snapshot.reserved,
            "remaining": snapshot.remaining,
        }
        self.assertEqual(
            payload,
            {"total": 100.0, "spent": 0.0, "reserved": 0.0, "remaining": 100.0},
        )
        self.assertFalse(hasattr(snapshot, "per_task_cap"))

    def test_runner_options_have_no_per_task_cap(self) -> None:
        options = RunOptions(tasks_dir=Path("."))
        self.assertFalse(hasattr(options, "per_task_cap"))


class LegacyChargeApiTests(unittest.TestCase):
    def test_budget_alias_is_the_ledger(self) -> None:
        self.assertIs(Budget, BudgetLedger)

    def test_charge_updates_counters_and_remaining(self) -> None:
        budget = Budget(1000.0)
        cost = budget.charge(100, 50)
        self.assertEqual(cost, 60.0)
        self.assertEqual(budget.spent, 60.0)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.remaining, 940.0)
        self.assertEqual(budget.prompt_tokens, 100)
        self.assertEqual(budget.completion_tokens, 50)
        self.assertFalse(budget.exhausted)

    def test_check_is_relative_to_the_global_remaining(self) -> None:
        budget = Budget(100.0)
        budget.charge(0, 90)
        self.assertTrue(budget.can_start_task())
        budget.check(estimated_cost=10.0)
        with self.assertRaises(BudgetExhausted):
            budget.check(estimated_cost=10.01)

    def test_reserve_blocks_new_calls(self) -> None:
        budget = Budget(100.0, reserve=20.0)
        budget.check(estimated_cost=1.0)
        budget.charge(0, 80)
        self.assertEqual(budget.remaining, 20.0)
        self.assertFalse(budget.can_start_task())
        with self.assertRaises(BudgetExhausted) as ctx:
            budget.check(estimated_cost=1.0)
        self.assertIn("reserve", str(ctx.exception))

    def test_exhaustion_is_a_sticky_stop(self) -> None:
        budget = Budget(10.0)
        budget.charge(0, 10)
        self.assertEqual(budget.remaining, 0.0)
        self.assertTrue(budget.exhausted)
        self.assertFalse(budget.can_start_task())
        with self.assertRaises(BudgetExhausted):
            budget.check()

    def test_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            Budget(-1)
        with self.assertRaises(ValueError):
            Budget(10, reserve=-1)
        with self.assertRaises(ValueError):
            Budget(10, reserve=11)


class ReservationTests(unittest.TestCase):
    def test_reserve_is_deducted_immediately(self) -> None:
        ledger = BudgetLedger(100.0)
        reservation = ledger.reserve("req-1", 100, 10)
        self.assertIsInstance(reservation, Reservation)
        self.assertEqual(reservation.amount, 20.0)
        self.assertEqual(ledger.spent, 0.0)
        self.assertEqual(ledger.reserved, 20.0)
        self.assertEqual(ledger.remaining, 80.0)
        self.assertTrue(ledger.can_start_task())

    def test_reserve_rejects_when_estimate_does_not_fit(self) -> None:
        ledger = BudgetLedger(100.0)
        ledger.reserve("first", 0, 90)
        with self.assertRaises(BudgetExhausted) as ctx:
            ledger.reserve("second", 0, 11)
        self.assertIn("insufficient budget", str(ctx.exception))
        self.assertEqual(ledger.reserved, 90.0)
        self.assertEqual(ledger.remaining, 10.0)

    def test_strict_boundary_allows_exact_fit_only(self) -> None:
        ledger = BudgetLedger(10.0)
        ledger.reserve("exact", 0, 10)
        self.assertEqual(ledger.reserved, 10.0)
        self.assertEqual(ledger.remaining, 0.0)
        with self.assertRaises(BudgetExhausted):
            ledger.reserve("over", 1, 0)
        self.assertEqual(ledger.spent + ledger.reserved, ledger.total)

    def test_settle_returns_unused_reserve(self) -> None:
        ledger = BudgetLedger(100.0)
        reservation = ledger.reserve("req", 100, 10)  # 20.0 reserved
        charged = ledger.settle(
            reservation,
            normalize_usage({"prompt_tokens": 100, "completion_tokens": 5}),
        )
        self.assertEqual(charged, 15.0)
        self.assertEqual(ledger.spent, 15.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.remaining, 85.0)
        self.assertEqual(ledger.prompt_tokens, 100)
        self.assertEqual(ledger.completion_tokens, 5)

    def test_settle_actual_above_estimate_then_next_reserve_sees_truth(self) -> None:
        ledger = BudgetLedger(100.0)
        reservation = ledger.reserve("req", 0, 10)
        charged = ledger.settle(
            reservation,
            TokenUsage(input_tokens=0, output_tokens=95, total_cost=95.0),
        )
        self.assertEqual(charged, 95.0)
        self.assertEqual(ledger.spent, 95.0)
        self.assertEqual(ledger.remaining, 5.0)
        with self.assertRaises(BudgetExhausted):
            ledger.reserve("too-big", 0, 6)
        ledger.reserve("fits", 0, 5)
        self.assertEqual(ledger.spent + ledger.reserved, ledger.total)

    def test_release_restores_capacity_without_charge(self) -> None:
        ledger = BudgetLedger(100.0)
        reservation = ledger.reserve("req", 0, 40)
        self.assertEqual(ledger.remaining, 60.0)
        released = ledger.release(reservation)
        self.assertEqual(released, 40.0)
        self.assertEqual(ledger.spent, 0.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.remaining, 100.0)

    def test_double_settle_and_double_release_raise(self) -> None:
        ledger = BudgetLedger(100.0)
        usage = normalize_usage({"prompt_tokens": 10, "completion_tokens": 1})

        reservation = ledger.reserve("settled", 0, 10)
        ledger.settle(reservation, usage)
        with self.assertRaises(BudgetError) as ctx:
            ledger.settle(reservation, usage)
        self.assertIn("already settled", str(ctx.exception))
        with self.assertRaises(BudgetError) as ctx:
            ledger.release(reservation)
        self.assertIn("already settled", str(ctx.exception))

        released = ledger.reserve("released", 0, 10)
        ledger.release(released)
        with self.assertRaises(BudgetError) as ctx:
            ledger.release(released)
        self.assertIn("already released", str(ctx.exception))
        with self.assertRaises(BudgetError) as ctx:
            ledger.settle(released, usage)
        self.assertIn("already released", str(ctx.exception))

    def test_unknown_and_duplicate_handles(self) -> None:
        ledger = BudgetLedger(100.0)
        with self.assertRaises(BudgetError) as ctx:
            ledger.settle("missing", TokenUsage())
        self.assertIn("unknown reservation", str(ctx.exception))
        with self.assertRaises(BudgetError) as ctx:
            ledger.release("missing")
        self.assertIn("unknown reservation", str(ctx.exception))

        ledger.reserve("dup", 0, 1)
        with self.assertRaises(BudgetError) as ctx:
            ledger.reserve("dup", 0, 1)
        self.assertIn("already active", str(ctx.exception))

    def test_invalid_reserve_arguments(self) -> None:
        ledger = BudgetLedger(100.0)
        with self.assertRaises(ValueError):
            ledger.reserve("", 0, 1)
        with self.assertRaises(ValueError):
            ledger.reserve("r", -1, 1)
        with self.assertRaises(ValueError):
            ledger.reserve("r", 0, -1)
        with self.assertRaises(ValueError):
            ledger.reserve("r", 0, 1, safety_margin=-1)

    def test_settle_accepts_a_mapping_and_uses_the_formula(self) -> None:
        ledger = BudgetLedger(100.0)
        reservation = ledger.reserve("req", 100, 10)
        charged = ledger.settle(
            reservation, {"prompt_tokens": 50, "completion_tokens": 10}
        )
        self.assertEqual(charged, 15.0)


class ConcurrencyTests(unittest.TestCase):
    def test_only_one_of_sixteen_concurrent_reserves_succeeds(self) -> None:
        ledger = BudgetLedger(1.0)
        barrier = threading.Barrier(16)
        granted: list[Reservation] = []
        exhausted = 0
        lock = threading.Lock()

        def worker(index: int) -> None:
            nonlocal exhausted
            try:
                barrier.wait(timeout=10)
                reservation = ledger.reserve(f"req-{index}", 0, 1)
            except BudgetExhausted:
                with lock:
                    exhausted += 1
            else:
                with lock:
                    granted.append(reservation)

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(worker, range(16)))

        self.assertEqual(len(granted), 1)
        self.assertEqual(exhausted, 15)
        self.assertEqual(ledger.reserved, 1.0)
        self.assertEqual(ledger.spent, 0.0)
        self.assertLessEqual(ledger.spent + ledger.reserved, ledger.total)

    def test_concurrent_reserve_settle_release_keeps_invariant(self) -> None:
        ledger = BudgetLedger(1_000.0)

        def worker(index: int) -> None:
            reservation = ledger.reserve(f"req-{index}", 10, 5)
            assert ledger.spent + ledger.reserved <= ledger.total
            if index % 3 == 0:
                ledger.release(reservation)
            else:
                ledger.settle(
                    reservation,
                    TokenUsage(input_tokens=10, output_tokens=5, total_cost=6.0),
                )
            assert ledger.spent + ledger.reserved <= ledger.total

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(56)))

        self.assertGreater(ledger.spent, 0.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertLessEqual(ledger.spent + ledger.reserved, ledger.total)


class BudgetProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.empty_env: dict[str, str] = {}

    def test_cli_budget_has_priority(self) -> None:
        decision = BudgetProvider(
            5.0,
            offline=True,
            environ={"OJ_TOKEN_BUDGET": "100", "BUDGET": "200"},
        ).resolve()
        self.assertEqual((decision.total, decision.source), (5.0, "cli"))

    def test_oj_token_budget_beats_budget(self) -> None:
        decision = BudgetProvider(
            offline=False,
            environ={"OJ_TOKEN_BUDGET": "100", "BUDGET": "200"},
        ).resolve()
        self.assertEqual((decision.total, decision.source), (100.0, "env:OJ_TOKEN_BUDGET"))

    def test_budget_env_is_the_last_fallback(self) -> None:
        decision = BudgetProvider(
            offline=False,
            environ={"OJ_TOKEN_BUDGET": "  ", "BUDGET": "200"},
        ).resolve()
        self.assertEqual((decision.total, decision.source), (200.0, "env:BUDGET"))

    def test_offline_default_has_its_own_source_and_warns(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            decision = BudgetProvider(offline=True, environ=self.empty_env).resolve()
        self.assertEqual(decision.total, DEFAULT_OFFLINE_BUDGET)
        self.assertEqual(decision.source, "offline-default")
        warning = stderr.getvalue()
        self.assertIn("local test assumption", warning)
        self.assertIn("not a competition budget value", warning)

    def test_missing_budget_outside_offline_is_an_error(self) -> None:
        with self.assertRaises(BudgetConfigurationError) as ctx:
            BudgetProvider(offline=False, environ=self.empty_env).resolve()
        self.assertIn("no token budget configured", str(ctx.exception))

    def test_invalid_environment_values_are_rejected(self) -> None:
        for raw in ("abc", "-1", "nan", "inf"):
            with self.subTest(raw=raw), self.assertRaises(BudgetConfigurationError):
                BudgetProvider(
                    offline=False,
                    environ={"OJ_TOKEN_BUDGET": raw},
                ).resolve()

    def test_zero_budget_is_a_valid_explicit_value(self) -> None:
        decision = BudgetProvider(
            offline=False, environ={"OJ_TOKEN_BUDGET": "0"}
        ).resolve()
        self.assertEqual(decision.total, 0.0)


class CliBudgetWiringTests(unittest.TestCase):
    def _run_main(self, args: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            code = run_agent.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_has_no_per_task_cap_and_safe_deadline(self) -> None:
        parser = run_agent.build_parser()
        args = parser.parse_args(["--tasks-dir", "."])
        self.assertEqual(args.time_limit, DEFAULT_TIME_LIMIT)
        self.assertLess(args.time_limit, EXTERNAL_TIMEOUT_SECONDS)
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            parser.parse_args(["--tasks-dir", ".", "--per-task-cap", "1"])
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_missing_budget_outside_offline_fails_before_any_llm_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_a")
            with mock.patch(
                "run_agent.build_llm_client",
                side_effect=AssertionError("LLM client must not be built"),
            ) as builder:
                code, stdout, stderr = self._run_main(
                    [
                        "--tasks-dir",
                        str(root),
                        "--out",
                        str(Path(tmp) / "out"),
                        "--dry-run",
                    ],
                    env={},
                )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("no token budget configured", stderr)
        builder.assert_not_called()

    def test_offline_cli_records_offline_default_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_a")
            code, stdout, stderr = self._run_main(
                [
                    "--tasks-dir",
                    str(root),
                    "--out",
                    str(Path(tmp) / "out"),
                    "--offline",
                    "--dry-run",
                    "--max-workers",
                    "1",
                ],
                env={},
            )
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["budget_source"], "offline-default")
        self.assertEqual(summary["budget"]["total"], DEFAULT_OFFLINE_BUDGET)
        self.assertEqual(summary["budget"]["reserved"], 0.0)
        self.assertNotIn("per_task_cap", summary["budget"])
        self.assertIn("not a competition budget value", stderr)

    def test_cli_budget_source_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_a")
            code, stdout, stderr = self._run_main(
                [
                    "--tasks-dir",
                    str(root),
                    "--out",
                    str(Path(tmp) / "out"),
                    "--offline",
                    "--dry-run",
                    "--budget",
                    "12.5",
                ],
                env={"OJ_TOKEN_BUDGET": "100", "BUDGET": "200"},
            )
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["budget_source"], "cli")
        self.assertEqual(summary["budget"]["total"], 12.5)
        self.assertNotIn("not a competition budget value", stderr)

    def test_environment_budget_source_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            make_task(root, "math_a")
            code, stdout, stderr = self._run_main(
                [
                    "--tasks-dir",
                    str(root),
                    "--out",
                    str(Path(tmp) / "out"),
                    "--offline",
                    "--dry-run",
                ],
                env={"OJ_TOKEN_BUDGET": "42", "BUDGET": "99"},
            )
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["budget_source"], "env:OJ_TOKEN_BUDGET")
        self.assertEqual(summary["budget"]["total"], 42.0)


if __name__ == "__main__":
    unittest.main()
