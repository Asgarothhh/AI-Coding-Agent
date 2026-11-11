# Final evaluation (D2 / R2-08)

- Generated: 2026-11-11T13:23:07+00:00
- Trace: `/tmp/out-d2.4WcpIH/trace.jsonl` (16 rows)
- Predictions: `/tmp/out-d2.4WcpIH/predictions.jsonl` (16 rows)
- Holdout: 30.0% of task ids, seed `v5`

## Primary outcome

Split: **test**, 7 row(s), 7 task(s).

| metric | value |
|---|---|
| score | 24.00 |
| solved | 7 |
| budget_used | 829.80 |
| overruns | 0 of 7 comparable (0.0%) |
| solve_rate | 7 / 7 (100.0%) |
| failed | 0 |
| skipped | 0 |
| errors | 0 |
| other statuses | 0 |
| rows without recorded cost | 0 |

## Forecast diagnostics

Diagnostic only: the estimator is a tool, not the primary criterion.

| metric | value |
|---|---|
| n | 2 |
| MAE | 3,330.10 |
| MdAPE | 1,118.72% |
| WAPE | 802.63% |
| bias (mean y_hat/y_actual) | 12.187 |
| zero-cost rows excluded | 5 |
| rows without y_hat | 0 |
| rows without y_actual | 0 |

## Interval diagnostics

| metric | value |
|---|---|
| n | 7 |
| PICP | 71.4% |
| MPIW | 2,643.69 |
| covered | 5 |
| rows without both bounds | 0 |
| rows without y_actual | 0 |
| inverted bounds excluded | 0 |

## Per-type

| type | rows | solved | score | budget_used | overruns | solve_rate |
|---|---|---|---|---|---|---|
| data_analysis | 2 | 2 | 7.00 | 0.00 | 0 | 100.0% |
| coding | 2 | 2 | 6.00 | 829.80 | 0 | 100.0% |
| search | 2 | 2 | 6.00 | 0.00 | 0 | 100.0% |
| math | 1 | 1 | 5.00 | 0.00 | 0 | 100.0% |

## Split

Split by `task_id` hash; train and test never share a task.

| part | rows | tasks |
|---|---|---|
| train | 9 | 9 |
| test | 7 | 7 |

Task overlap: 0.

## Reference (same run)

| part | score | solved | budget_used | overruns | solve_rate |
|---|---|---|---|---|---|
| all | 54.00 | 16 | 2,343.00 | 0 | 100.0% |
| train | 30.00 | 9 | 1,513.20 | 0 | 100.0% |

## Predictions inventory

| field | value |
|---|---|
| rows | 16 |
| tasks | 16 |
| mean expected_cost | 936.25 |
| mean upper_cost | 3,249.47 |
| theories | calibration: 4, deterministic: 12 |
| models | qwen3.6-35b-a3b-noreason: 16 |

## Warnings

- none
