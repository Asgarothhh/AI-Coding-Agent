"""Consumer contract: production BudgetLedger under exhaustion.

These tests import :class:`harness.budget.BudgetLedger` (the real class, not a
copy) and exercise only the public ``reserve`` / ``settle`` / ``release`` /
``snapshot`` / ``stop`` API.  They are the R3 hardening B-slice additions; they
do not replace the existing unit coverage in ``tests/test_budget.py``
ReservationTests or ``tests/contract/test_budget_contract.py`` LedgerContractTests.

Gateway/send is involved only in the optional B2 spy.  B7 (timeout after send)
is a D1 defect and is not patched here.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.budget import (
    BudgetError,
    BudgetExhausted,
    BudgetLedger,
    BudgetSnapshot,
)
from harness.usage import normalize_usage

_SNAPSHOT_DIR = (
    Path(__file__).resolve().parents[2] / "eval" / "results" / "budget_stress"
)


def _dump(name: str, payload: dict[str, Any]) -> None:
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _SNAPSHOT_DIR / f"{name}.json"
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, PermissionError):
        return


def _snap(ledger: BudgetLedger) -> dict[str, Any]:
    snapshot = ledger.snapshot()
    assert isinstance(snapshot, BudgetSnapshot)
    return asdict(snapshot)


class ProductionLedgerImportTests(unittest.TestCase):
    def test_imports_harness_budget_class_not_a_copy(self) -> None:
        import harness.budget as budget_mod
        from harness.budget import BudgetLedger as Imported

        self.assertIs(Imported, budget_mod.BudgetLedger)
        self.assertEqual(Imported.__module__, "harness.budget")
        ledger = Imported(10.0)
        self.assertIsInstance(ledger, budget_mod.BudgetLedger)
        self.assertTrue(callable(ledger.reserve))
        self.assertTrue(callable(ledger.settle))
        self.assertTrue(callable(ledger.release))
        self.assertTrue(callable(ledger.snapshot))
        self.assertTrue(callable(ledger.stop))


class ExhaustionContractTests(unittest.TestCase):
    """B1, B5, B6, B8, B9, B10, B11, B12 on the public ledger API."""

    def test_b1_exact_fit_then_second_reserve_exhausted(self) -> None:
        # Sequential: one output=10 request (cost 10) fills total=10 after settle.
        # Existing exact-fit *without* settle: tests/test_budget.py:155
        # (ReservationTests.test_strict_boundary_allows_exact_fit_only).
        ledger = BudgetLedger(10.0)
        handle = ledger.reserve("b1-first", 0, 10)
        self.assertEqual(handle.amount, 10.0)
        charged = ledger.settle(
            handle, {"prompt_tokens": 0, "completion_tokens": 10}
        )
        self.assertEqual(charged, 10.0)
        self.assertEqual(ledger.spent, 10.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.remaining, 0.0)
        with self.assertRaises(BudgetExhausted):
            ledger.reserve("b1-second", 0, 10)
        after = _snap(ledger)
        self.assertEqual(after["spent"], 10.0)
        self.assertEqual(after["reserved"], 0.0)
        _dump(
            "b1_exact_fit",
            {
                "scenario": "B1",
                "production_class": "harness.budget.BudgetLedger",
                "snapshot": after,
            },
        )

    def test_b5_two_sequential_settles_sum_formula_costs(self) -> None:
        # Existing settle shape: tests/test_budget.py:164
        # (ReservationTests.test_settle_returns_unused_reserve).
        ledger = BudgetLedger(100.0)
        first = ledger.reserve("b5-a", 0, 10)
        cost_a = ledger.settle(
            first, {"prompt_tokens": 0, "completion_tokens": 4}
        )
        second = ledger.reserve("b5-b", 20, 10)
        cost_b = ledger.settle(
            second, {"prompt_tokens": 20, "completion_tokens": 7}
        )
        self.assertEqual(cost_a, 4.0)
        self.assertEqual(cost_b, 9.0)
        self.assertEqual(ledger.spent, 13.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.spent, cost_a + cost_b)
        _dump(
            "b5_distinct_actuals",
            {
                "scenario": "B5",
                "costs": [cost_a, cost_b],
                "snapshot": _snap(ledger),
            },
        )

    def test_b6_release_restores_remaining_without_settle(self) -> None:
        # Existing: tests/test_budget.py:193
        # (ReservationTests.test_release_restores_capacity_without_charge).
        ledger = BudgetLedger(100.0)
        handle = ledger.reserve("b6", 0, 40)
        self.assertEqual(ledger.remaining, 60.0)
        released = ledger.release(handle)
        self.assertEqual(released, 40.0)
        self.assertEqual(ledger.spent, 0.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.remaining, 100.0)
        _dump(
            "b6_release_no_settle",
            {"scenario": "B6", "released": released, "snapshot": _snap(ledger)},
        )

    def test_b8_double_settle_and_double_release_do_not_double_charge(self) -> None:
        # Existing: tests/test_budget.py:203
        # (ReservationTests.test_double_settle_and_double_release_raise).
        ledger = BudgetLedger(100.0)
        settled = ledger.reserve("b8-settle", 0, 10)
        charged = ledger.settle(
            settled, {"prompt_tokens": 0, "completion_tokens": 5}
        )
        self.assertEqual(charged, 5.0)
        with self.assertRaises(BudgetError) as ctx:
            ledger.settle(settled, {"prompt_tokens": 0, "completion_tokens": 5})
        self.assertIn("already settled", str(ctx.exception))
        self.assertEqual(ledger.spent, 5.0)
        self.assertEqual(ledger.reserved, 0.0)

        released = ledger.reserve("b8-release", 0, 10)
        ledger.release(released)
        with self.assertRaises(BudgetError) as ctx:
            ledger.release(released)
        self.assertIn("already released", str(ctx.exception))
        self.assertEqual(ledger.spent, 5.0)
        self.assertEqual(ledger.reserved, 0.0)
        _dump(
            "b8_double_settle_release",
            {"scenario": "B8", "snapshot": _snap(ledger)},
        )

    def test_b9_settle_none_charges_zero_current_fail_open(self) -> None:
        # CURRENT behavior, not a v5 fail-closed pass.
        # normalize_usage(None) → zero TokenUsage is locked by
        # tests/test_usage.py:113 test_missing_usage_normalizes_to_zero.
        # Do not treat this assertion as closing the D1 defect.
        ledger = BudgetLedger(100.0)
        handle = ledger.reserve("b9-none", 0, 10)
        charged = ledger.settle(handle, None)
        self.assertEqual(charged, 0.0)
        self.assertEqual(ledger.spent, 0.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.remaining, 100.0)

        negative_map = ledger.reserve("b9-neg-map", 0, 10)
        charged_neg = ledger.settle(
            negative_map, {"prompt_tokens": -5, "completion_tokens": -3}
        )
        # normalize_usage ignores negative ints (_as_int returns None).
        self.assertEqual(charged_neg, 0.0)

        bad_object = ledger.reserve("b9-bad-obj", 0, 10)
        with self.assertRaises(BudgetError) as ctx:
            ledger.settle(bad_object, object())
        self.assertIn("unsupported usage object", str(ctx.exception))
        # _coerce_usage runs before the lock, so the reservation is still live.
        self.assertEqual(ledger.reserved, 10.0)
        ledger.release(bad_object)

        class _NegativeDuck:
            input_tokens = -1
            output_tokens = 2

        duck = ledger.reserve("b9-neg-duck", 0, 10)
        with self.assertRaises(ValueError):
            ledger.settle(duck, _NegativeDuck())
        ledger.release(duck)
        _dump(
            "b9_settle_none_fail_open",
            {
                "scenario": "B9",
                "settle_none_charged": charged,
                "negative_mapping_charged": charged_neg,
                "unsupported_object": "BudgetError",
                "negative_duck": "ValueError from TokenUsage",
                "v5_fail_closed": False,
                "snapshot": _snap(ledger),
            },
        )

    def test_b10_nested_reasoning_already_in_completion_counted_once(self) -> None:
        payload = {
            "prompt_tokens": 10,
            "completion_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 40},
        }
        expected = normalize_usage(payload)
        self.assertEqual(expected.output_tokens, 100)
        self.assertEqual(expected.reasoning_tokens, 40)
        self.assertEqual(expected.total_cost, 101.0)

        ledger = BudgetLedger(1_000.0)
        handle = ledger.reserve("b10", 10, 100)
        charged = ledger.settle(handle, payload)
        self.assertEqual(charged, 101.0)
        self.assertEqual(ledger.spent, 101.0)
        self.assertNotEqual(ledger.spent, 141.0)
        self.assertEqual(ledger.completion_tokens, 100)
        _dump(
            "b10_reasoning_once",
            {
                "scenario": "B10",
                "charged": charged,
                "double_count_would_be": 141.0,
                "snapshot": _snap(ledger),
            },
        )

    def test_b11_settle_actual_above_reserve_can_make_remaining_negative(self) -> None:
        # Existing "next reserve sees truth" (remaining stays >= 0):
        # tests/test_budget.py:178
        # (ReservationTests.test_settle_actual_above_estimate_then_next_reserve_sees_truth).
        # This case deliberately overspends the *total* so remaining goes negative.
        ledger = BudgetLedger(20.0)
        handle = ledger.reserve("b11", 0, 10)
        charged = ledger.settle(
            handle, {"prompt_tokens": 0, "completion_tokens": 50}
        )
        self.assertEqual(charged, 50.0)
        self.assertEqual(ledger.spent, 50.0)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertLess(ledger.remaining, 0.0)
        self.assertEqual(ledger.remaining, -30.0)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.remaining, -30.0)
        self.assertTrue(snapshot.stopped)
        self.assertNotIn("invariant_violation", asdict(snapshot))
        self.assertFalse(hasattr(snapshot, "invariant_violation"))
        _dump(
            "b11_remaining_negative",
            {
                "scenario": "B11",
                "charged": charged,
                "invariant_event": None,
                "snapshot": asdict(snapshot),
            },
        )

    def test_b12_stop_blocks_further_reserve_without_changing_spent(self) -> None:
        ledger = BudgetLedger(100.0)
        handle = ledger.reserve("b12", 0, 10)
        ledger.settle(handle, {"prompt_tokens": 0, "completion_tokens": 10})
        spent_before = ledger.spent
        self.assertEqual(spent_before, 10.0)
        ledger.stop()
        self.assertTrue(ledger.stopped)
        with self.assertRaises(BudgetExhausted) as ctx:
            ledger.reserve("b12-after-stop", 0, 1)
        self.assertIn("stopped", str(ctx.exception))
        self.assertEqual(ledger.spent, spent_before)
        self.assertEqual(ledger.reserved, 0.0)
        _dump(
            "b12_stop",
            {
                "scenario": "B12",
                "spent_before_stop": spent_before,
                "snapshot": _snap(ledger),
            },
        )


class _FakeModel:
    """Minimal invoke spy copied from tests/test_openjiuwen_gateway.FakeModel."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec
        self.calls: list[tuple[Any, int | None, float | None]] = []

    async def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(
            (messages, kwargs.get("max_tokens"), kwargs.get("temperature"))
        )
        raise AssertionError("invoke must not run after a failed reserve")


class _FakeCountingFactory:
    def __init__(self) -> None:
        self.models: list[_FakeModel] = []

    def __call__(self, spec: Any) -> _FakeModel:
        model = _FakeModel(spec)
        self.models.append(model)
        return model


class GatewayReserveFailSpyTests(unittest.TestCase):
    """B2: public OpenJiuwenGateway + real BudgetLedger, no D1 edits."""

    def test_b2_generate_does_not_invoke_when_estimate_does_not_fit(self) -> None:
        try:
            from harness.openjiuwen_gateway import (
                GatewayBudgetError,
                GatewayConfigError,
                GatewayUnavailableError,
                OpenJiuwenGateway,
            )
        except Exception as exc:  # pragma: no cover - import-time SDK issues
            self.skipTest(f"OpenJiuwenGateway import failed: {type(exc).__name__}: {exc}")

        factory = _FakeCountingFactory()
        ledger = BudgetLedger(1.0)
        try:
            gateway = OpenJiuwenGateway(
                base_url="http://127.0.0.1:9/v1",
                api_key="x",
                ledger=ledger,
                model_factory=factory,
                max_retries=0,
                normalizer=normalize_usage,
            )
        except (GatewayConfigError, GatewayUnavailableError) as exc:
            self.skipTest(f"gateway construction failed: {type(exc).__name__}: {exc}")
        self.addCleanup(gateway.close)

        with self.assertRaises((BudgetExhausted, GatewayBudgetError)):
            gateway.generate(
                model="qwen3.6-35b-a3b-noreason",
                messages=[{"role": "user", "content": "ping"}],
                max_output_tokens=1_000,
                request_id="r1",
            )

        invoke_calls = sum(len(model.calls) for model in factory.models)
        self.assertEqual(invoke_calls, 0)
        # _get_model constructs the FakeModel before reserve; that is not a send.
        self.assertEqual(len(factory.models), 1)
        self.assertEqual(ledger.reserved, 0.0)
        self.assertEqual(ledger.spent, 0.0)
        _dump(
            "b2_gateway_no_invoke",
            {
                "scenario": "B2",
                "models_constructed": len(factory.models),
                "invoke_calls": invoke_calls,
                "snapshot": _snap(ledger),
            },
        )


if __name__ == "__main__":
    unittest.main()
