"""Unit tests for the budget-aware decision policy (spec 001 v5.0, section 8)."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, dataclass
from itertools import combinations

from harness.budget import BudgetLedger
from harness.estimator import CostEstimate
from harness.policy import (
    ACTION_REASONS,
    POLICY_NAMES,
    Decision,
    PolicyConfig,
    plan,
    plan_entries,
    planning_reserve,
    replan,
    solve_probability,
    summarize,
)
from harness.usage import TokenUsage

MODEL = "qwen3.8-27b"


@dataclass(frozen=True)
class SimTask:
    task_id: str
    reward: float
    task_type: str = "coding"


def estimate(
    upper: float,
    *,
    confidence: str = "high",
    solver: str = "coding",
    output_tokens: float = 100.0,
    expected: float | None = None,
) -> CostEstimate:
    expected_cost = float(upper) if expected is None else float(expected)
    return CostEstimate(
        expected_cost=expected_cost,
        upper_cost=float(upper),
        expected_input_tokens=100.0,
        expected_output_tokens=float(output_tokens),
        confidence=confidence,  # type: ignore[arg-type]
        model=MODEL,
        solver=solver,
        drivers=("test",),
    )


def make_estimates(tasks: list[SimTask], costs: list[float], **kwargs: object) -> dict[str, CostEstimate]:
    return {
        task.task_id: estimate(cost, **kwargs)  # type: ignore[arg-type]
        for task, cost in zip(tasks, costs)
    }


def decisions_by_id(entries: list) -> dict[str, Decision]:
    return {entry.task.task_id: entry.decision for entry in entries}


def accepted_ids(entries: list) -> list[str]:
    return [entry.task.task_id for entry in entries if entry.decision.accepted]


class SolveProbabilityTests(unittest.TestCase):
    def test_probability_does_not_depend_on_reward(self) -> None:
        low = SimTask("low", reward=1.0, task_type="coding")
        high = SimTask("high", reward=1000.0, task_type="coding")
        estimate_one = estimate(300.0, confidence="medium")
        p_low, source_low = solve_probability(low, estimate_one)
        p_high, source_high = solve_probability(high, estimate_one)
        self.assertEqual(p_low, 0.5)
        self.assertEqual(p_high, 0.5)
        self.assertEqual(source_low, source_high)
        self.assertEqual(source_low, "heuristic:medium")

    def test_confidence_heuristic_is_configurable(self) -> None:
        task = SimTask("t", reward=1.0)
        expected = {"high": 0.7, "medium": 0.5, "low": 0.3}
        for confidence, value in expected.items():
            with self.subTest(confidence=confidence):
                probability, source = solve_probability(
                    task, estimate(100.0, confidence=confidence)
                )
                self.assertAlmostEqual(probability, value)
                self.assertEqual(source, f"heuristic:{confidence}")
        custom = PolicyConfig(probability_high=0.9, probability_low=0.1)
        self.assertAlmostEqual(
            solve_probability(task, estimate(100.0, confidence="high"), config=custom)[0],
            0.9,
        )
        self.assertAlmostEqual(
            solve_probability(task, estimate(100.0, confidence="low"), config=custom)[0],
            0.1,
        )

    def test_deterministic_solver_has_probability_one(self) -> None:
        task = SimTask("m", reward=5.0, task_type="math")
        probability, source = solve_probability(
            task, estimate(0.0, confidence="low", solver="math")
        )
        self.assertEqual(probability, 1.0)
        self.assertEqual(source, "deterministic")

    def test_historical_rate_overrides_heuristic_and_records_source(self) -> None:
        task = SimTask("coding_1", reward=5.0, task_type="coding")
        probability, source = solve_probability(
            task,
            estimate(100.0, confidence="high"),
            historical_solve_rates={"coding": 0.42},
        )
        self.assertAlmostEqual(probability, 0.42)
        self.assertEqual(source, "historical:coding")

        by_task = solve_probability(
            task,
            estimate(100.0),
            historical_solve_rates={"coding_1": 0.9, "coding": 0.4},
        )
        self.assertAlmostEqual(by_task[0], 0.9)
        self.assertEqual(by_task[1], "historical:coding_1")

    def test_impossible_probability_is_zero(self) -> None:
        task = SimTask("bad", reward=10.0, task_type="search")
        probability, source = solve_probability(task, estimate(50.0), feasible=False)
        self.assertEqual(probability, 0.0)
        self.assertEqual(source, "impossible")


class ReserveTests(unittest.TestCase):
    def test_base_reserve_is_five_percent_of_total(self) -> None:
        estimates = [estimate(100.0, confidence="high") for _ in range(3)]
        self.assertAlmostEqual(planning_reserve(1000.0, estimates), 50.0)

    def test_mostly_low_confidence_raises_reserve_to_ten_percent(self) -> None:
        low = [estimate(100.0, confidence="low") for _ in range(2)]
        high = [estimate(100.0, confidence="high")]
        self.assertAlmostEqual(planning_reserve(1000.0, low + high), 100.0)
        # Exactly half is not "mostly low".
        self.assertAlmostEqual(planning_reserve(1000.0, low[:1] + high), 50.0)

    def test_empty_estimates_use_base_rate(self) -> None:
        self.assertAlmostEqual(planning_reserve(1000.0, []), 50.0)

    def test_zero_budget_has_zero_reserve(self) -> None:
        self.assertEqual(planning_reserve(0.0, [estimate(10.0)]), 0.0)


class SelectionTests(unittest.TestCase):
    def test_reserve_constraint_blocks_task_that_would_fit_budget(self) -> None:
        tasks = [SimTask("a", reward=10.0), SimTask("b", reward=10.0)]
        estimates = make_estimates(tasks, [500.0, 500.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="accept_all",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=10_000.0,
            impossible_ids=frozenset(),
        )
        self.assertEqual(accepted_ids(entries), ["a"])
        decisions = decisions_by_id(entries)
        self.assertEqual(decisions["b"].action, "reject_cost")
        self.assertEqual(decisions["b"].reason, "reject:cost")
        # 500 + 500 == 1000 would fit the raw budget but not with the 5% reserve.
        self.assertLessEqual(sum(e.decision.planned_cost for e in entries if e.decision.accepted), 950.0)

    def test_no_per_task_cap(self) -> None:
        tasks = [SimTask("whole", reward=1.0)]
        estimates = make_estimates(tasks, [950.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="value_cost_greedy",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=10_000.0,
            impossible_ids=frozenset(),
        )
        self.assertEqual(accepted_ids(entries), ["whole"])

    def test_zero_budget_rejects_non_zero_cost(self) -> None:
        tasks = [SimTask("a", reward=10.0), SimTask("b", reward=5.0)]
        estimates = make_estimates(tasks, [100.0, 200.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="value_cost_greedy",
            remaining=0.0,
            total_budget=0.0,
            time_left=1000.0,
            impossible_ids=frozenset(),
        )
        self.assertEqual(accepted_ids(entries), [])
        self.assertEqual(
            {entry.decision.action for entry in entries},
            {"reject_cost"},
        )

    def test_zero_cost_task_is_accepted_at_zero_budget(self) -> None:
        tasks = [SimTask("det", reward=3.0, task_type="math")]
        estimates = make_estimates(tasks, [0.0], solver="math", output_tokens=0.0)
        entries = plan_entries(
            tasks,
            estimates,
            policy="accept_all",
            remaining=0.0,
            total_budget=0.0,
            time_left=1000.0,
            impossible_ids=frozenset(),
        )
        self.assertEqual(accepted_ids(entries), ["det"])

    def test_time_limit_zero_rejects_every_task(self) -> None:
        tasks = [SimTask("a", reward=10.0), SimTask("b", reward=5.0)]
        estimates = make_estimates(tasks, [100.0, 200.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="accept_all",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=0.0,
            impossible_ids=frozenset(),
        )
        self.assertEqual(accepted_ids(entries), [])
        for entry in entries:
            self.assertEqual(entry.decision.action, "reject_time")
            self.assertEqual(entry.decision.reason, "reject:time")

    def test_time_constraint_accumulates(self) -> None:
        # 10s base + output / 10 tokens per second.
        fast = SimTask("fast", reward=10.0)
        slow = SimTask("slow", reward=8.0)
        estimates = {
            "fast": estimate(300.0, output_tokens=500.0),  # 60 s
            "slow": estimate(300.0, output_tokens=900.0),  # 100 s
        }
        entries = plan_entries(
            [fast, slow],
            estimates,
            policy="value_cost_greedy",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=150.0,
            impossible_ids=frozenset(),
        )
        decisions = decisions_by_id(entries)
        self.assertEqual(decisions["fast"].action, "accept")
        self.assertEqual(decisions["slow"].action, "reject_time")
        self.assertLessEqual(sum(e.time_cost for e in entries if e.decision.accepted), 150.0)

    def test_impossible_task_is_rejected_with_reason(self) -> None:
        tasks = [SimTask("ok", reward=10.0), SimTask("bad", reward=100.0, task_type="search")]
        estimates = make_estimates(tasks, [100.0, 100.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="value_cost_greedy",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=1000.0,
            impossible_ids={"bad"},
        )
        decisions = decisions_by_id(entries)
        self.assertEqual(decisions["ok"].action, "accept")
        self.assertEqual(decisions["bad"].action, "reject_impossible")
        self.assertEqual(decisions["bad"].reason, "reject:impossible")
        self.assertEqual(decisions["bad"].expected_reward, 0.0)
        bad_entry = next(entry for entry in entries if entry.task.task_id == "bad")
        self.assertEqual(bad_entry.probability, 0.0)
        self.assertEqual(bad_entry.probability_source, "impossible")

    def test_accept_all_keeps_input_order(self) -> None:
        tasks = [
            SimTask("c", reward=1.0),
            SimTask("a", reward=10.0),
            SimTask("b", reward=5.0),
        ]
        estimates = make_estimates(tasks, [100.0, 100.0, 100.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="accept_all",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=1000.0,
            impossible_ids=frozenset(),
        )
        self.assertEqual(accepted_ids(entries), ["c", "a", "b"])

    def test_reward_sort_orders_by_reward_descending(self) -> None:
        tasks = [
            SimTask("c", reward=1.0),
            SimTask("a", reward=10.0),
            SimTask("b", reward=5.0),
        ]
        estimates = make_estimates(tasks, [100.0, 100.0, 100.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="reward_sort",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=1000.0,
            impossible_ids=frozenset(),
        )
        self.assertEqual(accepted_ids(entries), ["a", "b", "c"])

    def test_value_cost_greedy_uses_ratio_and_skips_non_fitting(self) -> None:
        tasks = [
            SimTask("cheap", reward=10.0),
            SimTask("expensive", reward=20.0),
            SimTask("medium", reward=8.0),
        ]
        # high confidence -> P=0.7, so v = reward * 0.7.
        estimates = make_estimates(tasks, [100.0, 600.0, 300.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="value_cost_greedy",
            remaining=700.0,
            total_budget=700.0,
            time_left=10_000.0,
            impossible_ids=frozenset(),
        )
        # Reserve is 35, capacity 665. Expensive (600) does not fit after
        # cheap (100), so greedy continues with medium (300).
        self.assertEqual(accepted_ids(entries), ["cheap", "medium"])
        decisions = decisions_by_id(entries)
        self.assertEqual(decisions["expensive"].action, "reject_cost")

    def test_knapsack_dp_matches_bruteforce_value(self) -> None:
        tasks = [SimTask(f"t{i}", reward=float(i + 1)) for i in range(10)]
        costs = [120.0, 200.0, 310.0, 90.0, 450.0, 60.0, 500.0, 330.0, 180.0, 250.0]
        estimates = make_estimates(tasks, costs)
        remaining = 1000.0
        total_budget = 1000.0
        entries = plan_entries(
            tasks,
            estimates,
            policy="knapsack_dp",
            remaining=remaining,
            total_budget=total_budget,
            time_left=100_000.0,
            impossible_ids=frozenset(),
        )
        capacity = remaining - planning_reserve(total_budget, list(estimates.values()))
        selected = [entry for entry in entries if entry.decision.accepted]
        self.assertLessEqual(sum(entry.decision.planned_cost for entry in selected), capacity + 1e-6)

        best = 0.0
        for size in range(len(tasks) + 1):
            for subset in combinations(range(len(tasks)), size):
                if sum(costs[index] for index in subset) <= capacity + 1e-9:
                    value = sum(tasks[index].reward * 0.7 for index in subset)
                    best = max(best, value)
        self.assertAlmostEqual(sum(entry.value for entry in selected), best, places=6)

    def test_planning_is_deterministic(self) -> None:
        tasks = [SimTask(f"t{i}", reward=float(i % 3), task_type="coding") for i in range(8)]
        estimates = make_estimates(tasks, [100.0 + i * 37.0 for i in range(8)])
        kwargs = dict(
            policy="value_cost_greedy",
            remaining=500.0,
            total_budget=500.0,
            time_left=1000.0,
            impossible_ids=frozenset(),
        )
        first = [entry.as_dict() for entry in plan_entries(tasks, estimates, **kwargs)]
        second = [entry.as_dict() for entry in plan_entries(tasks, estimates, **kwargs)]
        self.assertEqual(first, second)
        # Rejected decisions follow accepted ones in input order.
        accepted_count = sum(1 for entry in first if entry["action"] == "accept")
        rejected_indices = [entry["task_id"] for entry in first[accepted_count:]]
        self.assertEqual(
            rejected_indices,
            sorted(rejected_indices, key=lambda task_id: int(task_id[1:])),
        )

    def test_budget_monotonicity_for_every_policy(self) -> None:
        tasks = [SimTask(f"t{i}", reward=float(i + 1)) for i in range(8)]
        costs = [60.0, 120.0, 180.0, 250.0, 330.0, 450.0, 500.0, 800.0]
        estimates = make_estimates(tasks, costs)
        budgets = [100.0, 300.0, 600.0, 1000.0, 2000.0]
        for policy in POLICY_NAMES:
            with self.subTest(policy=policy):
                values = []
                spends = []
                for budget in budgets:
                    entries = plan_entries(
                        tasks,
                        estimates,
                        policy=policy,
                        remaining=budget,
                        total_budget=budget,
                        time_left=100_000.0,
                        impossible_ids=frozenset(),
                    )
                    accepted = [entry for entry in entries if entry.decision.accepted]
                    values.append(sum(entry.value for entry in accepted))
                    spends.append(sum(entry.decision.planned_cost for entry in accepted))
                    reserve = planning_reserve(budget, list(estimates.values()))
                    self.assertLessEqual(spends[-1], budget - reserve + 1e-6)
                # More budget can never reduce the expected value, for every
                # policy.  Spend is monotone for the greedy family; the DP may
                # trade a cheaper equal-value subset at a larger capacity.
                self.assertEqual(values, sorted(values))
                if policy != "knapsack_dp":
                    self.assertEqual(spends, sorted(spends))

    def test_all_reasons_are_canonical(self) -> None:
        tasks = [SimTask("a", reward=1.0), SimTask("bad", reward=2.0)]
        estimates = make_estimates(tasks, [100.0, 100.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="value_cost_greedy",
            remaining=0.0,
            total_budget=0.0,
            time_left=1000.0,
            impossible_ids={"bad"},
        )
        allowed = set(ACTION_REASONS.values())
        self.assertTrue({entry.decision.reason for entry in entries} <= allowed)

    def test_summary_counts_reasons_and_coverage(self) -> None:
        tasks = [SimTask("a", reward=10.0), SimTask("bad", reward=5.0)]
        estimates = make_estimates(tasks, [100.0, 100.0])
        entries = plan_entries(
            tasks,
            estimates,
            policy="accept_all",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=1000.0,
            impossible_ids={"bad"},
        )
        summary = summarize(entries, policy="accept_all")
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.feasible, 1)
        self.assertEqual(summary.accepted, 1)
        self.assertEqual(summary.coverage, 1.0)
        self.assertEqual(dict(summary.reasons)["reject:impossible"], 1)


class ReplanTests(unittest.TestCase):
    def test_replan_uses_ledger_remaining_without_mutating_past_decisions(self) -> None:
        tasks = [SimTask(f"t{i}", reward=5.0) for i in range(4)]
        estimates = make_estimates(tasks, [300.0, 300.0, 300.0, 300.0])
        before = plan(
            tasks,
            estimates,
            policy="accept_all",
            remaining=1000.0,
            total_budget=1000.0,
            time_left=1000.0,
            impossible_ids=frozenset(),
        )
        before_copy = list(before)
        self.assertEqual(sum(1 for decision in before if decision.accepted), 3)

        ledger = BudgetLedger(1000.0)
        reservation = ledger.reserve("task-1", estimated_input_tokens=3000, estimated_output_tokens=0)
        ledger.settle(reservation, TokenUsage(input_tokens=3000, output_tokens=0, total_cost=300.0))
        snapshot = ledger.snapshot()
        self.assertAlmostEqual(snapshot.remaining, 700.0)

        after = replan(
            tasks,
            estimates,
            snapshot,
            1000.0,
            policy="accept_all",
            impossible_ids=frozenset(),
        )
        self.assertEqual(before, before_copy)  # frozen decisions are untouched
        self.assertEqual(sum(1 for decision in after if decision.accepted), 2)
        with self.assertRaises(FrozenInstanceError):
            before[0].action = "reject_cost"  # type: ignore[misc]

    def test_replan_accepts_a_mapping_snapshot(self) -> None:
        tasks = [SimTask("a", reward=5.0), SimTask("b", reward=4.0)]
        estimates = make_estimates(tasks, [400.0, 400.0])
        expected = plan(
            tasks,
            estimates,
            policy="value_cost_greedy",
            remaining=600.0,
            total_budget=1000.0,
            time_left=1000.0,
            impossible_ids=frozenset(),
        )
        actual = replan(
            tasks,
            estimates,
            {"remaining": 600.0, "total": 1000.0},
            1000.0,
            policy="value_cost_greedy",
            impossible_ids=frozenset(),
        )
        self.assertEqual(expected, actual)

    def test_replan_requires_ledger_remaining(self) -> None:
        with self.assertRaises(ValueError):
            replan([SimTask("a", 1.0)], {"a": estimate(10.0)}, {}, 100.0)


class ContractShapeTests(unittest.TestCase):
    def test_decision_has_spec_fields(self) -> None:
        decision = Decision(
            action="accept",
            reason=ACTION_REASONS["accept"],
            planned_cost=10.0,
            expected_reward=7.0,
        )
        self.assertEqual(decision.action, "accept")
        self.assertEqual(decision.reason, "accept")
        self.assertEqual(decision.planned_cost, 10.0)
        self.assertEqual(decision.expected_reward, 7.0)
        self.assertTrue(decision.accepted)

    def test_negative_or_non_finite_estimates_are_rejected(self) -> None:
        task = SimTask("a", reward=1.0)
        for bad in (-1.0, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    plan_entries(
                        [task],
                        {"a": estimate(bad)},
                        policy="accept_all",
                        remaining=100.0,
                        total_budget=100.0,
                        time_left=100.0,
                        impossible_ids=frozenset(),
                    )

    def test_missing_or_duplicate_task_ids_fail_fast(self) -> None:
        with self.assertRaises(ValueError):
            plan_entries(
                [SimTask("a", 1.0)],
                {},
                policy="accept_all",
                remaining=100.0,
                total_budget=100.0,
                time_left=100.0,
                impossible_ids=frozenset(),
            )
        estimate_one = estimate(10.0)
        with self.assertRaises(ValueError):
            plan_entries(
                [SimTask("a", 1.0), SimTask("a", 2.0)],
                {"a": estimate_one},
                policy="accept_all",
                remaining=100.0,
                total_budget=100.0,
                time_left=100.0,
                impossible_ids=frozenset(),
            )

    def test_unknown_policy_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            plan_entries(
                [SimTask("a", 1.0)],
                {"a": estimate(10.0)},
                policy="nope",
                remaining=100.0,
                total_budget=100.0,
                time_left=100.0,
                impossible_ids=frozenset(),
            )


if __name__ == "__main__":
    unittest.main()
