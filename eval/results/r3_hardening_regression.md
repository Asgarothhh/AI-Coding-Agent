# R3 hardening slice D — full regression

Date: 2026-11-12  
Run ID: `r3hard-D-2026-11-12T0034Z`  
HEAD: `fe2ec9e053acf54ca68b00f151ec4af926c0778c` (`feat(r3): implement solvers and eval OpenSpec change`)  
Working tree: HEAD plus **uncommitted** hardening (A/B/C artifacts and tests). **Not committed.**  
Python: `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` → `Python 3.11.16`  
OpenSpec: `/home/asgaroth/.local/bin/openspec` → `1.17.0` (`npx` not used)  
Live LLM: **not run**. Slice C remains **BLOCKED** (`eval/results/openjiuwen_gateway_live_smoke.md`).  
fake_100: **not re-run** into `run1`/`run2`/`run3` (already complete 100/100). Cited below.

Fixtures `examples/task-dir`, `examples/task-dir-extended`, and `eval/external/lorne` were **not mutated**. Offline runs used `/tmp` copies. `--out` also under `/tmp`.

## Command table

| command | Python/version | run ID | exit code | passed/failed/skipped | baseline | actual | artifact | notes |
|---|---|---|---:|---|---|---|---|---|
| `$PY --version` | 3.11.16 | r3hard-D | 0 | n/a | 3.11.16 | 3.11.16 | — | `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` |
| `$PY -m unittest discover -s tests -q` | 3.11.16 | r3hard-D | **0** | Ran **392**, OK, failed=0, skipped=**2** (10.289s) | 370 OK, 2 skipped | 392 OK, 2 skipped | `eval/results/r3_hard_unit.log` | Count grew +22 (7 `test_fake_100` + 15 budget contract). Failures stay 0. Skips stay 2 (justified below). |
| `$PY -m unittest discover -s tests/contract -q` | 3.11.16 | r3hard-D | **0** | Ran **47**, OK, failed=0, skipped=0 (4.839s) | 32 OK | 47 OK | `eval/results/r3_hard_contract.log` | +15 from `test_budget_exhaustion` + `test_budget_concurrency`. Reconfirm exit 0. |
| `$PY -m unittest tests.test_fake_100 tests.contract.test_budget_exhaustion tests.contract.test_budget_concurrency tests.test_generic_solver -q` | 3.11.16 | r3hard-D | **0** | Ran **34**, OK, failed=0, skipped=0 (0.058s) | n/a (new A/B + existing generic) | 34 OK | `eval/results/r3_hard_targeted.log` | Loader logs for the eight invalid syn_* IDs are expected. Generic `llm_error: proxy unavailable` is a passing-test stderr line, not a skip. Reconfirm exit 0. |
| `$PY run_agent.py --tasks-dir /tmp/r3hard-task-dir --offline --max-workers 1 --out /tmp/r3hard-out8` | 3.11.16 | r3hard-D | **0** | solved=**6** failed=2 skipped=0 / tasks=8 | 6/8 score 16 tokens 0 | 6/8 score **16.0** tokens_spent **0.0** | `eval/results/r3_hard_offline8.log` | Copy of `examples/task-dir`. Coding_0003/0007 failed without LLM (expected). |
| `$PY run_agent.py --tasks-dir /tmp/r3hard-task-dir-extended --offline --max-workers 1 --out /tmp/r3hard-out16` | 3.11.16 | r3hard-D | **0** | solved=**12** failed=4 skipped=0 / tasks=16 | 12/16 score 42 | 12/16 score **42.0** tokens_spent **0.0** | `eval/results/r3_hard_offline16.log` | Copy of `examples/task-dir-extended`. Failed: coding_0003/0007/0008/0009. |
| `$PY run_agent.py --tasks-dir /tmp/r3hard-lorne --offline --max-workers 1 --out /tmp/r3hard-out-lorne` | 3.11.16 | r3hard-D | **0** | solved=**0** failed=10 skipped=0 / tasks=10 | 0/10 expected without LLM | 0/10 score **0.0** tokens_spent **0.0** | `eval/results/r3_hard_lorne.log` | **Not a defect.** Coding 8 + generic 2, no LLM. |
| fake_100 3× seed 2026 + w1 (cited, not re-run) | 3.11.16 | A (prior) | 0 | generated=100 loaded=92 invalid=8 solved=59 failed=17 skipped=16 error=0 score=76 | 59/17/16/76 + 8 invalid; 100/100 accounted | same on run1/run2/run3/w1 | `eval/results/fake_100_reconciliation.md`, `fake_100_run{1,2,3,w1}/` | Historical `fake_100.json` remains 92-state baseline (tasks=100, no invalid bucket). |
| `openspec --version` | n/a | r3hard-D | **0** | 1.17.0 | 1.17.0 | 1.17.0 | `eval/results/r3_hard_openspec.log` | Standalone binary, not npx. |
| `openspec list --all` | n/a | r3hard-D | **0** | implement-r3-solvers-and-eval Complete; add-token-estimator 24/40 | Complete, not archived | Complete, not archived | `eval/results/r3_hard_openspec.log` | `openspec archive` **not** run. |
| `openspec list --archived` | n/a | r3hard-D | **0** | No archived changes found | none | none | `eval/results/r3_hard_openspec.log` | |
| `openspec status --all` | n/a | r3hard-D | **0** | both changes 4/4 artifacts | 4/4 | 4/4 | `eval/results/r3_hard_openspec.log` | |
| `openspec status --change implement-r3-solvers-and-eval` | n/a | r3hard-D | **0** | 4/4 proposal/specs/design/tasks | Complete | Complete | `eval/results/r3_hard_openspec.log` | |
| `openspec validate implement-r3-solvers-and-eval --strict` | n/a | r3hard-D | **0** | valid | valid | `Change 'implement-r3-solvers-and-eval' is valid` | `eval/results/r3_hard_openspec.log` | Not archived. |

`$PY` = `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11`. No command failed. No traceback. `sdk_smoke.py` and live LLM were not run.

## Status matrix

| Block | Status | Proof |
|---|---|---|
| A 100 IDs | **DONE** | `eval/results/fake_100_reconciliation.md`; run1/run2/run3/w1 each `wc -l tasks.jsonl == 100`; `generated=100 loaded=92 invalid=8`; IDs `syn_004/016/028/040/052/064/076/088`; `solved+failed+skipped+error+invalid=100`. Historical `fake_100.json` / `fake_100_baseline.json` untouched 92-state totals (59/17/16 score 76). |
| B Budget | **DONE (D1 defects remain)** | `eval/results/budget_stress_report.md`, `eval/results/d1_budget_defects.md`, `tests/contract/test_budget_exhaustion.py`, `tests/contract/test_budget_concurrency.py`. 15 new tests in this discover. B7/B9/B11 documented, not patched. |
| C Live Gateway | **BLOCKED** | creds unset — `eval/results/openjiuwen_gateway_live_smoke.md` |
| D Regression | **DONE** | this run: unit 392/0 fail/2 skip exit 0; contract 47 exit 0; offline 6/8 score 16; 12/16 score 42; lorne 0/10; OpenSpec valid not archived; fixtures hash-matched |
| Reviewer A/B | **PASS-with-notes** | LOW/MEDIUM dump fixed (A/B reviews already completed; no CRITICAL/HIGH left open for those slices) |
| OpenSpec | applied, valid, **not archived** | `openspec list --archived` → none; validate --strict → valid |
| R3-04 | **PARTIAL** | no AST/fuzzy in default ladder |
| R3-07 | **PARTIAL** | no fresh live Lorne |

## Unit skips (stay 2, justified)

Inspect of skip decorators (no execute) found exactly two:

- `test_ojiuwen_llm.RealOpenJiuwenImportTests.test_build_runtime_returns_adapter` — `openjiuwen is not installed` (`@unittest.skipUnless(_has_openjiuwen(), ...)`)
- `test_ojiuwen_llm.RealOpenJiuwenImportTests.test_explicit_backend_constructs_client` — same

These are the historical two skips. No new skip was introduced by A/B tests. Contract discover reports no skips.

## Offline JSON summaries (this run)

### 8-task copy (`/tmp/r3hard-task-dir`)

- tasks=8 planned=8 solved=**6** failed=2 skipped=0 score=**16.0** tokens_spent=**0.0** errors=0 `llm_backend=offline`
- solved: math_0000, math_0004, search_0001, search_0005, data_analysis_0002, data_analysis_0006
- failed: coding_0003, coding_0007 (no LLM)

### 16-task copy (`/tmp/r3hard-task-dir-extended`)

- tasks=16 planned=16 solved=**12** failed=4 skipped=0 score=**42.0** tokens_spent=**0.0** errors=0 `llm_backend=offline`
- failed: coding_0003, coding_0007, coding_0008, coding_0009

### lorne copy (`/tmp/r3hard-lorne`)

- tasks=10 planned=10 solved=**0** failed=10 skipped=0 score=**0.0** tokens_spent=**0.0** errors=0 `llm_backend=offline`
- coding (8): argparse-flag, docstring-ast, empty-list, fix-add, fix-import, rename-func, trace-bug, write-test — `tests fail and no LLM is available`
- generic (2): read-const, read-def — `no LLM is available for this task type`
- Expected without LLM. Not a regression.

## fake_100 (cited, not re-run)

See `eval/results/fake_100_reconciliation.md`.

| run | workers | generated | loaded | invalid | solved | failed | skipped | error | score | complete |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `fake_100_run1` | 5 | 100 | 92 | 8 | 59 | 17 | 16 | 0 | 76.0 | true |
| `fake_100_run2` | 5 | 100 | 92 | 8 | 59 | 17 | 16 | 0 | 76.0 | true |
| `fake_100_run3` | 5 | 100 | 92 | 8 | 59 | 17 | 16 | 0 | 76.0 | true |
| `fake_100_run_w1` | 1 | 100 | 92 | 8 | 59 | 17 | 16 | 0 | 76.0 | true |

Invalid IDs: `syn_004`, `syn_016`, `syn_028`, `syn_040`, `syn_052`, `syn_064`, `syn_076`, `syn_088`.  
Invariant: `59+17+16+0+8=100`.  
Historical `eval/results/fake_100.json` (and `fake_100_baseline.json`) still records the 92-loaded summary (`tasks=100`, solved=59 failed=17 skipped=16 score=76 tokens_spent=360) without an `invalid` bucket.

## Fixture hashes (before == after)

Method: `(cd DIR && find . -type f | sort | xargs -r sha256sum | sha256sum)`. Per-file listings: `eval/results/r3_hard_hashes_before_{taskdir,extended,lorne}.txt`. `cmp` of before vs after listings: **MATCH** for all three trees. Original trees contain **0** `answer.txt`; copies under `/tmp` received answers as expected.

| Tree | files | TREE_HASH before | TREE_HASH after | match |
|---|---:|---|---|---|
| `examples/task-dir` | 88 | `7ccb9a4c5caf998de60f985902fd11abbd0eafe011ec5e015f9f12554ae49e1f` | same | yes |
| `examples/task-dir-extended` | 195 | `78ed850e5157a1af0d6a5e390ed93006ea93c44e671a9a22df486aaf5b93d634` | same | yes |
| `eval/external/lorne` | 41 | `02edb31ce908b283e091638ce347a5e2f5a81f8f0b52678417bde3f319d95ccc` | same | yes |

Note: historical `dev3_report.md` listed different combined hashes (`288f8975…` / `f9f9edd4…`) from a prior hashing method. This D run proves **this-run before==after**, not a re-hash of that older algorithm.

## OpenSpec (not archived)

```
openspec --version
1.17.0

openspec list --all
  implement-r3-solvers-and-eval     ✓ Complete
  add-token-estimator               24/40 tasks

openspec list --archived
  No archived changes found.

openspec validate implement-r3-solvers-and-eval --strict
  Change 'implement-r3-solvers-and-eval' is valid
```

`openspec archive` was **not** invoked. R3-04 / R3-07 stay PARTIAL in D3 reports.

## Regression vs baseline

| Check | Result |
|---|---|
| Unit failures | 0 (no regression). Count 370→392 expected. |
| Unit skips | 2 (no growth). Same two `test_ojiuwen_llm` skipUnless. |
| Contract failures | 0. Count 32→47 expected. |
| Offline 8 | match 6/8 score 16 tokens 0 |
| Offline 16 | match 12/16 score 42 |
| Lorne offline | match 0/10 (expected) |
| fake_100 | cited 100/100; not re-run |
| Live LLM | still not run; C BLOCKED |
| OpenSpec | valid, Complete, not archived |
| Fixtures | unchanged |

## Linked artifacts

- `eval/results/r3_hardening_initial_state.md`
- `eval/results/fake_100_reconciliation.md`
- `eval/results/budget_stress_report.md`
- `eval/results/d1_budget_defects.md`
- `eval/results/openjiuwen_gateway_live_smoke.md`
- `eval/results/r3_hardening_regression.md` (this file)
- Logs (`*.log` is gitignored; files exist on disk): `r3_hard_unit.log`, `r3_hard_contract.log`, `r3_hard_targeted.log`, `r3_hard_offline8.log`, `r3_hard_offline16.log`, `r3_hard_lorne.log`, `r3_hard_openspec.log`

## Scope notes

- Did not edit D1 product files, solvers, planner, or OpenSpec delta/proposal/tasks checkboxes.
- Did not commit. Did not archive. Did not run live LLM.
- Historical logs `dev3_offline*.log` / `dev3_official8.json` / `fake_100.json` / `fake_100_baseline.json` were not overwritten.
