# Budget policy theories on a reproducible task set

Measured by `python3.11 eval/simulate_policies.py`; every number below is computed from the same task set for every policy.

## Setup

- task set: lorne held-out test split (42 observed runs) from `eval/results/lorne_priors.json`
- tasks: 42; rewards from a fixed SHA-256 schedule (1.0, 2.0, 3.0, 5.0, 8.0, 13.0)
- costs: observed lorne usage `tokens_in / 10 + tokens_out` (or documented synthetic costs)
- upper estimate: observed cost x 1.15; actual spend uses the observed cost
- confidence labels: synthetic SHA-256 cycle over high/medium/low, only to exercise the `P_solve` heuristic; a real run uses estimator confidence
- solve outcomes: deterministic `sha256('policy-sim-v1:outcome:<task_id>')` vs `P_solve`
- budgets: 20,000, 50,000, 100,000 cost units (**synthetic scenarios**, not official competition budgets — spec 12.5)
- time cap: 3,600 s per scenario; planning reserve: 5% of total (10% when most estimates are low confidence)
- no per-task cap and no per-task token budget; only the global budget is enforced

## Measured results

| budget | policy | accepted | coverage | solved | reward | expected | spent | tokens in | tokens out | overspend |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 20,000 | `accept_all` | 4 | 9.5% | 2 | 14.0 | 11.7 | 16,004.4 | 128,794 | 3,125 | 0 |
| 20,000 | `reward_sort` | 3 | 7.1% | 2 | 26.0 | 31.2 | 14,644.8 | 119,278 | 2,717 | 0 |
| 20,000 | `value_cost_greedy` | 7 | 16.7% | 4 | 47.0 | 43.4 | 16,244.5 | 137,945 | 2,450 | 0 |
| 20,000 | `knapsack_dp` | 7 | 16.7% | 4 | 47.0 | 43.4 | 16,244.5 | 137,945 | 2,450 | 0 |
| 50,000 | `accept_all` | 10 | 23.8% | 6 | 47.0 | 29.7 | 40,618.6 | 327,666 | 7,852 | 0 |
| 50,000 | `reward_sort` | 9 | 21.4% | 7 | 91.0 | 67.6 | 39,850.9 | 315,499 | 8,301 | 0 |
| 50,000 | `value_cost_greedy` | 13 | 31.0% | 7 | 76.0 | 78.6 | 40,588.7 | 330,557 | 7,533 | 0 |
| 50,000 | `knapsack_dp` | 13 | 31.0% | 7 | 76.0 | 78.6 | 40,588.7 | 330,557 | 7,533 | 0 |
| 100,000 | `accept_all` | 19 | 45.2% | 11 | 74.0 | 66.3 | 82,265.0 | 659,320 | 16,333 | 0 |
| 100,000 | `reward_sort` | 20 | 47.6% | 10 | 120.0 | 107.8 | 82,287.0 | 659,810 | 16,306 | 0 |
| 100,000 | `value_cost_greedy` | 25 | 59.5% | 14 | 127.0 | 117.5 | 81,760.1 | 671,441 | 14,616 | 0 |
| 100,000 | `knapsack_dp` | 25 | 59.5% | 14 | 127.0 | 117.5 | 81,760.1 | 671,441 | 14,616 | 0 |

## Aggregate across the three budgets

| policy | accepted | solved | reward | expected | spent | overspend |
|---|---:|---:|---:|---:|---:|---:|
| `accept_all` | 33 | 19 | 135.0 | 107.7 | 138,888.0 | 0 |
| `reward_sort` | 32 | 19 | 237.0 | 206.6 | 136,782.7 | 0 |
| `value_cost_greedy` | 45 | 25 | 250.0 | 239.5 | 138,593.3 | 0 |
| `knapsack_dp` | 45 | 25 | 250.0 | 239.5 | 138,593.3 | 0 |

## Conclusion

**Winner: `value_cost_greedy`.** It collects 250.0 realised reward over all three synthetic budgets, versus 250.0 for `knapsack_dp`. It accepts 45 tasks in total for 138,593.3 spent cost units and keeps the overspend count at 0.

`knapsack_dp` selected exactly the same accepted set on this task set: the same reward (250.0) for the same spend (138,593.3). The `v / c` order is already optimal at these caps, so the DP adds complexity without a measured gain. Per spec 12.6 the simpler default stays.

`reward_sort` behaves as expected: it concentrates the cap on high-reward items and accepts fewer tasks. It wins one single realisation (50k) but its expected value is lower at every budget, so it is not a reliable default. `accept_all` ignores reward and fills capacity in input order; it is the cheapest baseline, not a contender.

Why the losers trail: `accept_all` has no reward signal and fills capacity in input order; `reward_sort` ignores cost, so it can exhaust the cap on a small number of expensive items; the greedy and knapsack policies both use `v_i / c_i` or the knapsack objective and therefore concentrate spend where expected value is highest. Every policy respects `sum(upper_cost) <= budget - planning_reserve`, which is why the overspend column is zero by construction as well as by measurement.

## Caveats

- Rewards and confidence labels are synthetic because the lorne trace contains usage only; they are documented and identical across policies, so the comparison is fair but not a claim about the official prize distribution.
- The reported reward is one seeded realisation of the solve outcomes. The `expected` column is the planner's `sum(v_i)` and is the lower-variance signal.
- Costs are observed lorne runs, not competition tasks. The 20k/50k/100k scenarios are for relative policy comparison only.
- The upper estimate is 15% above the observed cost. That makes the overspend count zero by construction; it is a budget-invariant check, not evidence that real estimates always upper-bound actual usage.
