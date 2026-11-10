# Budget policy and cost-basis theories on a reproducible task set

Measured by `python3.11 eval/simulate_policies.py`; every number below is computed from the same task set for every policy and cost basis.

## Setup

- task set: lorne held-out test split (42 observed runs) from `eval/results/lorne_priors.json`
- tasks: 42; rewards from a fixed SHA-256 schedule (1.0, 2.0, 3.0, 5.0, 8.0, 13.0)
- costs: observed lorne usage `tokens_in / 10 + tokens_out` (or documented synthetic costs)
- `expected` basis plans with observed cost; `upper` basis plans with `max(observed x 1.15, 12,997.9)`, mirroring the ledger-safe production p90 bound
- confidence labels: synthetic SHA-256 cycle over high/medium/low, only to exercise the `P_solve` heuristic; a real run uses estimator confidence
- solve outcomes: deterministic `sha256('policy-sim-v1:outcome:<task_id>')` vs `P_solve`
- budgets: 20,000, 50,000, 100,000 cost units (**synthetic scenarios**, not official competition budgets — spec 12.5)
- time cap: 3,600 s per scenario; planning reserve: 5% of total (10% when most estimates are low confidence)
- no per-task cap and no per-task token budget; only the global budget is enforced

## Measured results

| basis | budget | policy | accepted | coverage | solved | reward | expected | spent | worst-case spend | overspend | over@upper |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `expected` | 20,000 | `accept_all` | 5 | 11.9% | 3 | 16.0 | 13.7 | 18,065.9 | 64,989.5 | 0 | 1 |
| `expected` | 20,000 | `reward_sort` | 4 | 9.5% | 2 | 26.0 | 37.7 | 17,340.8 | 51,991.6 | 0 | 1 |
| `expected` | 20,000 | `value_cost_greedy` | 8 | 19.0% | 4 | 47.0 | 46.9 | 18,355.4 | 103,983.2 | 0 | 1 |
| `expected` | 20,000 | `knapsack_dp` | 8 | 19.0% | 4 | 47.0 | 46.9 | 18,355.4 | 103,983.2 | 0 | 1 |
| `expected` | 50,000 | `accept_all` | 11 | 26.2% | 7 | 55.0 | 32.1 | 46,347.2 | 142,976.9 | 0 | 1 |
| `expected` | 50,000 | `reward_sort` | 12 | 28.6% | 9 | 112.0 | 81.0 | 46,932.9 | 155,974.8 | 0 | 1 |
| `expected` | 50,000 | `value_cost_greedy` | 16 | 38.1% | 8 | 81.0 | 86.0 | 46,952.5 | 207,966.4 | 0 | 1 |
| `expected` | 50,000 | `knapsack_dp` | 16 | 38.1% | 8 | 81.0 | 86.0 | 46,952.5 | 207,966.4 | 0 | 1 |
| `expected` | 100,000 | `accept_all` | 21 | 50.0% | 11 | 64.0 | 63.4 | 94,153.4 | 272,955.9 | 0 | 1 |
| `expected` | 100,000 | `reward_sort` | 24 | 57.1% | 11 | 125.0 | 115.8 | 93,328.7 | 311,949.6 | 0 | 1 |
| `expected` | 100,000 | `value_cost_greedy` | 28 | 66.7% | 15 | 129.0 | 122.9 | 94,732.9 | 363,941.2 | 0 | 1 |
| `expected` | 100,000 | `knapsack_dp` | 28 | 66.7% | 15 | 129.0 | 122.9 | 94,732.9 | 363,941.2 | 0 | 1 |
| `upper` | 20,000 | `accept_all` | 1 | 2.4% | 0 | 0.0 | 2.1 | 2,845.3 | 12,997.9 | 0 | 0 |
| `upper` | 20,000 | `reward_sort` | 1 | 2.4% | 1 | 13.0 | 13.0 | 2,045.1 | 12,997.9 | 0 | 0 |
| `upper` | 20,000 | `value_cost_greedy` | 1 | 2.4% | 1 | 13.0 | 13.0 | 2,045.1 | 12,997.9 | 0 | 0 |
| `upper` | 20,000 | `knapsack_dp` | 1 | 2.4% | 1 | 13.0 | 13.0 | 2,045.1 | 12,997.9 | 0 | 0 |
| `upper` | 50,000 | `accept_all` | 3 | 7.1% | 1 | 1.0 | 5.2 | 10,055.7 | 38,993.7 | 0 | 0 |
| `upper` | 50,000 | `reward_sort` | 3 | 7.1% | 2 | 26.0 | 31.2 | 14,644.8 | 38,993.7 | 0 | 0 |
| `upper` | 50,000 | `value_cost_greedy` | 3 | 7.1% | 2 | 26.0 | 31.2 | 14,644.8 | 38,993.7 | 0 | 0 |
| `upper` | 50,000 | `knapsack_dp` | 3 | 7.1% | 2 | 26.0 | 31.2 | 14,644.8 | 38,993.7 | 0 | 0 |
| `upper` | 100,000 | `accept_all` | 7 | 16.7% | 4 | 29.0 | 22.7 | 30,161.4 | 90,985.3 | 0 | 0 |
| `upper` | 100,000 | `reward_sort` | 7 | 16.7% | 5 | 65.0 | 57.2 | 31,717.3 | 90,985.3 | 0 | 0 |
| `upper` | 100,000 | `value_cost_greedy` | 7 | 16.7% | 5 | 65.0 | 57.2 | 31,717.3 | 90,985.3 | 0 | 0 |
| `upper` | 100,000 | `knapsack_dp` | 7 | 16.7% | 5 | 65.0 | 57.2 | 31,717.3 | 90,985.3 | 0 | 0 |

`spent` and `overspend` use observed (expected) cost. `worst-case spend` sums `upper_cost` of the accepted tasks, and `over@upper` flags a scenario where that worst case exceeds the budget. It is the honest stress view for the `expected` basis.

## Aggregate across the three budgets

| basis | policy | accepted | solved | reward | expected | spent | worst-case spend | overspend | over@upper |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `expected` | `accept_all` | 37 | 21 | 135.0 | 109.2 | 158,566.5 | 480,922.3 | 0 | 3 |
| `expected` | `reward_sort` | 40 | 22 | 263.0 | 234.5 | 157,602.4 | 519,916.0 | 0 | 3 |
| `expected` | `value_cost_greedy` | 52 | 27 | 257.0 | 255.8 | 160,040.8 | 675,890.8 | 0 | 3 |
| `expected` | `knapsack_dp` | 52 | 27 | 257.0 | 255.8 | 160,040.8 | 675,890.8 | 0 | 3 |
| `upper` | `accept_all` | 11 | 5 | 30.0 | 30.0 | 43,062.4 | 142,976.9 | 0 | 0 |
| `upper` | `reward_sort` | 11 | 8 | 104.0 | 101.4 | 48,407.2 | 142,976.9 | 0 | 0 |
| `upper` | `value_cost_greedy` | 11 | 8 | 104.0 | 101.4 | 48,407.2 | 142,976.9 | 0 | 0 |
| `upper` | `knapsack_dp` | 11 | 8 | 104.0 | 101.4 | 48,407.2 | 142,976.9 | 0 | 0 |

## Conclusion

**Winner: `expected` / `reward_sort`.** It collects 263.0 realised reward over all three synthetic budgets with 0 measured overspend events, accepts 40 tasks and spends 157,602.4 cost units.

Best per basis: `expected` / `reward_sort` reaches 263.0 reward (40 tasks, 157,602.4 spent); `upper` / `reward_sort` reaches 104.0 reward (11 tasks, 48,407.2 spent). The expected basis is the only one that can use the whole practical capacity; the upper basis leaves most of the budget unplanned whenever the p90 bound is far above typical spend.

`reward_sort` wins this one seeded realisation, but its expected value is 234.5 versus 255.8 for `value_cost_greedy`. The shipped default stays `value_cost_greedy`: realised reward is a single noisy draw, while `sum(v_i)` is the lower-variance planning signal, and the simpler greedy policy is the reproducible choice required by spec 12.6.

The `expected` basis wins this measurement and is the default. It is not a hard guarantee: the plan only promises `sum(expected_cost) <= budget - reserve`, while a single task may consume up to (or beyond) its upper bound. The ledger still enforces per-call safety and stops *new* work, but an already-started call can overshoot. The `over@upper` column makes that risk explicit.

**Risk reported honestly:** 12 of 12 expected-basis scenarios would exceed the global budget if every accepted task consumed its full `upper_cost` (the worst-case spend reaches 519.9% of the budget). The measured `spent` and `overspend` columns use observed (expected) cost, so they stay at zero; `over@upper` is the conservative stress view. In production the ledger prevents *new* calls past the limit, but a call already in flight can still overshoot, so the expected basis trades planning headroom for throughput.

Why the other policies trail: `accept_all` has no reward signal and fills capacity in input order; `reward_sort` ignores cost, so it lands on high-reward items in this draw but carries a lower expected value and can exhaust the cap on a few expensive tasks; the greedy and knapsack policies both use `v_i / c_i` or the knapsack objective and therefore concentrate spend where expected value is highest. On this set `value_cost_greedy` and `knapsack_dp` selected the same accepted sets, so the simpler greedy policy remains the default.

Every policy respects `sum(c_i) <= budget - planning_reserve`: `overspend = 0` for all 24 measured scenarios. That is an invariant of the planner, not a claim that actual usage always stays below `upper_cost`.

## Caveats

- Rewards and confidence labels are synthetic because the lorne trace contains usage only; they are documented and identical across policies and bases, so the comparison is fair but not a claim about the official prize distribution.
- The reported reward is one seeded realisation of the solve outcomes. The `expected` column is the planner's `sum(v_i)` and is the lower-variance signal.
- Costs are observed lorne runs, not competition tasks. The 20k/50k/100k scenarios are for relative policy comparison only.
- `over@upper` assumes every accepted task consumes its full `upper_cost`. That is a stress bound, not a forecast; the `upper` basis keeps even that stress view within budget by construction, while `expected` does not.
