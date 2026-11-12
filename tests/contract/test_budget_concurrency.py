"""Consumer contract: production BudgetLedger under concurrent reserve.

Uses ``threading.Barrier`` (never ``time.sleep``) so the burst is synchronized.
The production runner cap ``MAX_PARALLEL_SESSIONS = 5`` is read, not changed.

Existing coverage that this file does not wholesale duplicate:

* tests/test_budget.py:260 ConcurrencyTests.test_only_one_of_sixteen_concurrent_reserves_succeeds
  (16 workers, K=1, Barrier)
* tests/test_budget.py:288 ConcurrencyTests.test_concurrent_reserve_settle_release_keeps_invariant
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.budget import BudgetExhausted, BudgetLedger, Reservation
from harness.runner import MAX_PARALLEL_SESSIONS

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


class ProductionLedgerImportTests(unittest.TestCase):
    def test_imports_harness_budget_class_not_a_copy(self) -> None:
        import harness.budget as budget_mod
        from harness.budget import BudgetLedger as Imported

        self.assertIs(Imported, budget_mod.BudgetLedger)
        self.assertEqual(Imported.__module__, "harness.budget")


class ConcurrencyContractTests(unittest.TestCase):
    def test_production_parallel_cap_is_five_and_untouched(self) -> None:
        self.assertEqual(MAX_PARALLEL_SESSIONS, 5)

    def test_b3_five_threads_k2_of_cost_ten(self) -> None:
        ledger = BudgetLedger(20.0)
        workers = 5
        barrier = threading.Barrier(workers)
        granted: list[Reservation] = []
        exhausted = 0
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            nonlocal exhausted
            try:
                barrier.wait(timeout=10)
                reservation = ledger.reserve(f"b3-{index}", 0, 10)
            except BudgetExhausted:
                with lock:
                    exhausted += 1
            except BaseException as exc:  # pragma: no cover - unexpected
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    granted.append(reservation)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, range(workers)))

        self.assertEqual(errors, [])
        self.assertEqual(len(granted), 2)
        self.assertEqual(exhausted, 3)
        snap = ledger.snapshot()
        self.assertIn(snap.reserved, {0.0, 10.0, 20.0})
        self.assertEqual(snap.reserved, 20.0)
        self.assertEqual(snap.spent, 0.0)
        self.assertLessEqual(snap.spent + snap.reserved, snap.total)
        _dump(
            "b3_five_threads_k2",
            {
                "scenario": "B3",
                "granted": len(granted),
                "exhausted": exhausted,
                "left_reserved": True,
                "sync": "threading.Barrier",
                "snapshot": asdict(snap),
            },
        )

    def test_b4_cost_two_does_not_fit_remainder_one(self) -> None:
        ledger = BudgetLedger(5.0)
        workers = 5
        barrier = threading.Barrier(workers)
        granted: list[Reservation] = []
        exhausted = 0
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            nonlocal exhausted
            try:
                barrier.wait(timeout=10)
                reservation = ledger.reserve(f"b4-{index}", 0, 2)
            except BudgetExhausted:
                snap = ledger.snapshot()
                self.assertLessEqual(snap.spent + snap.reserved, snap.total)
                with lock:
                    exhausted += 1
            except BaseException as exc:  # pragma: no cover - unexpected
                with lock:
                    errors.append(exc)
            else:
                snap = ledger.snapshot()
                self.assertLessEqual(snap.spent + snap.reserved, snap.total)
                with lock:
                    granted.append(reservation)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, range(workers)))

        self.assertEqual(errors, [])
        self.assertEqual(len(granted), 2)
        self.assertEqual(exhausted, 3)
        snap = ledger.snapshot()
        self.assertEqual(snap.reserved, 4.0)
        self.assertEqual(snap.spent, 0.0)
        self.assertLessEqual(snap.spent + snap.reserved, 5.0)
        self.assertLessEqual(snap.spent + snap.reserved, snap.total)
        _dump(
            "b4_remainder_one",
            {
                "scenario": "B4",
                "granted": len(granted),
                "exhausted": exhausted,
                "sync": "threading.Barrier",
                "snapshot": asdict(snap),
            },
        )

    def test_b13_eight_threads_k3_still_atomic_above_runner_cap(self) -> None:
        # Production cap is 5 (harness/runner.py MAX_PARALLEL_SESSIONS). This
        # test only stresses the ledger API with 8 threads; it does not raise
        # the runner cap.
        self.assertEqual(MAX_PARALLEL_SESSIONS, 5)
        self.assertGreater(8, MAX_PARALLEL_SESSIONS)

        ledger = BudgetLedger(3.0)
        workers = 8
        barrier = threading.Barrier(workers)
        granted: list[Reservation] = []
        exhausted = 0
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            nonlocal exhausted
            try:
                barrier.wait(timeout=10)
                reservation = ledger.reserve(f"b13-{index}", 0, 1)
            except BudgetExhausted:
                snap = ledger.snapshot()
                self.assertLessEqual(snap.spent + snap.reserved, snap.total)
                with lock:
                    exhausted += 1
            except BaseException as exc:  # pragma: no cover - unexpected
                with lock:
                    errors.append(exc)
            else:
                snap = ledger.snapshot()
                self.assertLessEqual(snap.spent + snap.reserved, snap.total)
                with lock:
                    granted.append(reservation)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, range(workers)))

        self.assertEqual(errors, [])
        self.assertEqual(len(granted), 3)
        self.assertEqual(exhausted, 5)
        snap = ledger.snapshot()
        self.assertEqual(snap.reserved, 3.0)
        self.assertEqual(snap.spent, 0.0)
        self.assertLessEqual(snap.spent + snap.reserved, snap.total)
        self.assertEqual(MAX_PARALLEL_SESSIONS, 5)
        _dump(
            "b13_eight_threads_k3",
            {
                "scenario": "B13",
                "granted": len(granted),
                "exhausted": exhausted,
                "workers": workers,
                "production_max_parallel_sessions": MAX_PARALLEL_SESSIONS,
                "runner_cap_raised": False,
                "sync": "threading.Barrier",
                "snapshot": asdict(snap),
            },
        )


if __name__ == "__main__":
    unittest.main()
