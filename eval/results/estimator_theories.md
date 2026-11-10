# Estimator theories on the lorne held-out split

Measured by `python3.11 eval/import_lorne.py`; all numbers below are computed from the split in `eval/results/lorne_priors.json`.

- train: 78 valid rows / 13 task ids
- test: 42 valid rows / 7 task ids
- split: task id, `sha256('lorne-v1:<task_id>') % 4 == 2 -> test`
- actual cost: `tokens_in / 10 + tokens_out` from real lorne usage
- MAPE uses positive actuals only (all valid rows are positive)

| variant | what it predicts | n | MAE | MdAPE % | MAPE % | WAPE % | under@upper % |
|---|---|---:|---:|---:|---:|---:|---:|
| `global_median` | one median input/output over all train rows | 42 | 1873.5 | 37.7 | 44.4 | 43.3 | n/a |
| `per_type` | median input/output by task_type, global fallback | 42 | 1836.0 | 37.8 | 43.1 | 42.4 | n/a |
| `per_solver_type` | median input/output by (solver, task_type), global fallback | 42 | 1836.0 | 37.8 | 43.1 | 42.4 | n/a |
| `per_model` | median input/output by model label, global fallback | 42 | 1873.5 | 37.7 | 44.4 | 43.3 | n/a |
| `prompt_heuristic` | estimator heuristic path: prompt chars / chars_per_token x steps; no priors | 42 | 3540.5 | 81.5 | 77.8 | 81.7 | 78.6 |
| `estimator_priors_offline` | CostEstimator with train priors; no online correction | 42 | 2101.3 | 41.7 | 51.9 | 48.5 | 14.3 |
| `online_correction` | CostEstimator with train priors + in-memory correction after each observed row | 42 | 2190.7 | 47.3 | 52.3 | 50.6 | 16.7 |

## Reading the table

- `under@upper` is only defined for variants that produce `upper_cost`; a percentile median has no upper estimate.
- `online_correction` estimates and observes rows sequentially in `(task_id, file, model)` order, so it adapts inside the held-out run. That is the theory under test, not an unbiased offline score.
- The prompt heuristic uses only instruction/file characters and the generated step/completion priors. Lorne's agent scaffolding (tool schemas, history) is not visible to it, so underestimation is expected.
- `per_model` is identical to `global_median` and `per_type` is identical to `per_solver_type` on this dataset: only one model passes validation and the type -> solver mapping is one-to-one. These are real findings, not duplicated code paths.
- `estimator_priors_offline` is worse than `per_type` because `harness/calibration.json` only persists priors with `n >= 8`. The 6-row `search` training group is dropped, so the held-out `read-def` rows fall back to the global prior. This is the cost of the persistence threshold.
- `online_correction` does not beat the offline priors on this split: 42 test rows from 7 tasks are too few and too heterogeneous for a stable multiplicative correction.
- The split is by task id, but task families (`py-*`, `js-*`, `read-*`) appear on both sides; with 20 tasks the numbers are noisy and are not a statistical guarantee.

## Verdict (measured on this split)

- Best median variant by WAPE: `per_type` (MAE 1836.0, WAPE 42.4%); the global median is MAE 1873.5 / WAPE 43.3%.
- The prompt-only heuristic is the worst variant (MAE 3540.5, WAPE 81.7%) and under-estimates 78.6% of rows even against `upper_cost`.
- No more complex theory tested here beats the best median on this split. Online correction and external-model priors are not justified by these numbers.

