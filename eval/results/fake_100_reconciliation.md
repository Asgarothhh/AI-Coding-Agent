# fake_100 100-ID reconciliation (R3 hardening slice A)

Report-layer fix only. Historical `eval/results/fake_100.json` and `eval/results/fake_100_baseline.json` were not overwritten. `load_tasks` skip semantics and runtime `SolveResult` were not changed.

Python: `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11`. HEAD at implementation: `fe2ec9e`.

## Cause of the previous 8 missing IDs

`--tasks 100` generates `syn_000`…`syn_099` with `bucket = index % 12`. Bucket 4 writes `{not json` via `_write_invalid`. `harness.task.load_tasks` logs `JSONDecodeError` and continues, so the runner summary covers only the 92 loaded tasks. The old writer copied `solved`/`failed`/`skipped` from that summary and set `"tasks": args.tasks` (=100), discarding `results`. The eight generated IDs never appeared in the report.

Confirmed invalid IDs for seed 2026, count=100:

`syn_004`, `syn_016`, `syn_028`, `syn_040`, `syn_052`, `syn_064`, `syn_076`, `syn_088`

This is generate → load skip + report hole, not budget, timeout, or a lost worker.

## Invariant after fix

Each generated ID gets one terminal record in `tasks.jsonl` (manifest order):

- `report_status=invalid` when the generated ID is absent from `load_tasks` (`reason=load_skipped:invalid_json`)
- otherwise the runner status if it is in `{solved, failed, skipped, error}`
- missing/duplicate/unknown/incomplete cases are `problems`; missing terminal does **not** publish a sixth status (`not_started` is not used)

When `complete` is true:

`solved + failed + skipped + error + invalid == generated == tasks_requested`

Runner `errors` is not added into that sum (`error` is already a status). Runtime `score` stays the runner solved-reward total. `checked_score` is an independent checker: sum of `reward` where `expected is not None` and `strip(answer) == strip(expected)`.

CLI writes `--out-dir/{manifest.json,tasks.jsonl,summary.json}`. Default `--out-dir` is a new timestamped directory under `eval/results/`. It never writes `eval/results/fake_100.json`. Exit 1 if `problems` is non-empty, the budget invariant is broken, or the run is incomplete.

## Runs (seed 2026)

| run | workers | exit | solved | failed | skipped | error | invalid | score | checked_score | spent+reserved | complete |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `fake_100_run1` | 5 | 0 | 59 | 17 | 16 | 0 | 8 | 76.0 | 76.0 | 360.0+0.0 | true |
| `fake_100_run2` | 5 | 0 | 59 | 17 | 16 | 0 | 8 | 76.0 | 76.0 | 360.0+0.0 | true |
| `fake_100_run3` | 5 | 0 | 59 | 17 | 16 | 0 | 8 | 76.0 | 76.0 | 360.0+0.0 | true |
| `fake_100_run_w1` | 1 | 0 | 59 | 17 | 16 | 0 | 8 | 76.0 | 76.0 | 360.0+0.0 | true |

Each `tasks.jsonl` has `wc -l == 100` unique IDs. `invalid_ids` match the eight listed above. `by_status` sums to 100. `generated=100`, `loaded=92`, `problems=[]`, `errors=[]`. `fake_llm_calls=68`. Historical runtime totals (59/17/16, score 76, spent 360) are unchanged; the eight IDs are now counted as `invalid` instead of dropped.

`tree_hash` differs across runs because the temp task tree includes `_out` timestamps. It is not a determinism key.

## Determinism

The three `workers=5` runs matched on `score`, `checked_score`, `by_status`, and per-id `report_status`. The `workers=1` control matched the same outcomes. No workers=5 flake was observed; default `--workers` remains 5.

## Commands and exit codes

```
PY=/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11
cd /home/asgaroth/Projects/AI-Coding-Agent
$PY -m unittest tests.test_fake_100 -v
# Ran 7 tests in 0.028s  OK  exit 0

$PY eval/fake_100.py --tasks 100 --workers 5 --seed 2026 --out-dir eval/results/fake_100_run1
# OK fake_100  exit 0  wc -l tasks.jsonl == 100

$PY eval/fake_100.py --tasks 100 --workers 5 --seed 2026 --out-dir eval/results/fake_100_run2
# OK fake_100  exit 0  wc -l tasks.jsonl == 100

$PY eval/fake_100.py --tasks 100 --workers 5 --seed 2026 --out-dir eval/results/fake_100_run3
# OK fake_100  exit 0  wc -l tasks.jsonl == 100

$PY eval/fake_100.py --tasks 100 --workers 1 --seed 2026 --out-dir eval/results/fake_100_run_w1
# OK fake_100  exit 0  wc -l tasks.jsonl == 100
```

Unit tests (no full 100-task execute): generate+load ID formula; missing ID; duplicate runner ID; unknown status; missing terminal; summary counts vs manual; complete happy path 3+1+1+1=6.
