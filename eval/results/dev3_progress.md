# D3 progress

OpenSpec change: `implement-r3-solvers-and-eval`  
HEAD: `8cbe25f9b90d266458c75eaa8f4b19950e7b9572`  
Python: `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` (`3.11.16`)  
OpenSpec CLI: standalone `1.17.0`  
Date: 2026-11-12  
Change **not archived** (P1 notes remain; live lorne not re-run).

> **Historical apply block** below is HEAD `8cbe25f9b90d266458c75eaa8f4b19950e7b9572`. Current tree is HEAD `fe2ec9e053acf54ca68b00f151ec4af926c0778c` plus **uncommitted** R3 hardening (not committed). R3-04 and R3-07 remain **PARTIAL**. See **R3 hardening** at the bottom.

| R3-ID | Status | Notes |
|---|---|---|
| R3-01 | DONE | `Task.raw`, finite numeric reward, path safety. `tests.test_task` + `tests.test_task_formats` OK. |
| R3-02 | DONE | Unmatched deterministic → generic when LLM available; offline stays failed 0 tokens. Official 6 non-coding FakeLLM calls=0. |
| R3-03 | DONE | Raw keys in generic prompt; `expected`/gold redacted. 1+1; empty not solved; JSON-in-fence kept. Reviewer HIGH oracle leak closed. |
| R3-04 | PARTIAL | SEARCH/REPLACE exact / newline / trailing / indent. Fuzzy/AST matching **not** in the default ladder. Syntax `ast.parse` after all hunks (R3-05). Full-file fallback green. Tiny-fixture hunk 82 B vs full file 32 B — **no 40% saving claimed**. |
| R3-05 | DONE | Default cap 3 (`SolverOptions`, `RunOptions`, `run_agent.py`). First diagnostic in prompt. Test hashes restored. Timeout path tested via `TimeoutExpired`. |
| R3-06 | DONE | `solveability.py`; skip only on `verdict == impossible`. Empty instruction only when stripped empty (not a 12-character cutoff). Phrase / math-without-digits → not skip. |
| R3-07 | PARTIAL | Offline official-8 and lorne-10 reports written. Live lorne 10/10 is **historical** (`eval/results/external-lorne.json`), not re-run. TCA path absent. |
| R3-08 | DONE | `eval/fake_100.py` seed 2026, workers=5, 100 temp dirs not in git. Budget invariant held. Fake LLM is `RunOptions.llm` + `BudgetLedger`, not OpenJiuwenGateway (no live proxy). |

## Gate 0

- OpenSpec CLI installed standalone 1.17.0. `npx @fission-ai/openspec@1.17.0` npm 502.
- `openspec init --tools cursor` **not run**.
- Baseline: unit 330/2 skipped OK; contract 32 OK; offline 6/8 score 16; extended 12/16 score 42. See `dev3_baseline.md`.

## Verification (this apply)

```
python3.11 -m unittest tests.test_task tests.test_task_formats tests.test_solvers tests.test_generic_solver tests.test_coding tests.test_patch tests.test_triage tests.test_solveability tests.test_store -q
→ Ran 108, OK, exit 0

python3.11 -m unittest discover -s tests -q
→ Ran 370 in 10.389s, OK (skipped=2), exit 0

python3.11 -m unittest discover -s tests/contract -q
→ Ran 32 in 4.883s, OK, exit 0

python3.11 eval/fake_100.py --tasks 100 --workers 5
→ OK fake_100, exit 0; solved=59 failed=17 skipped=16 score=76.0 tokens_spent=360.0 spent+reserved=360<=100000

python3.11 run_agent.py --tasks-dir <copy>/task-dir --offline --max-workers 1
→ exit 0; tasks=8 solved=6 failed=2 skipped=0 score=16.0 tokens=0

python3.11 run_agent.py --tasks-dir <copy>/task-dir-extended --offline --max-workers 1
→ exit 0; tasks=16 solved=12 failed=4 skipped=0 score=42.0 tokens=0

python3.11 run_agent.py --tasks-dir <copy>/lorne --offline --max-workers 1
→ exit 0; tasks=10 solved=0 failed=10 score=0.0 (coding/generic without LLM)

openspec validate implement-r3-solvers-and-eval --strict
→ Change 'implement-r3-solvers-and-eval' is valid
```

Official 8 / extended 16 match Gate 0 baseline (no regression). Coding still fails offline without LLM, as expected.

Final [Review](33bd8728-675d-4466-a00c-5394d212b2c6): no CRITICAL; HIGH expected-oracle and short-instruction cutoff were fixed after review. Change stays unarchived (R3-04/R3-07 PARTIAL).

## Blockers

- D1: live proxy / gateway not used in fake_100 (no `LLM_BASE_URL` for this apply).
- D2: none remaining on the two agreed call sites.
- `/home/eq/techlb/TCA` still absent; lorne subset is `eval/external/lorne/`.
- Official регламент file still not in repo; v5 quotes used.

## Do not

- Archive this change while R3-04/R3-07 are PARTIAL.
- Archive `add-token-estimator`.
- Commit unless the owner asks.

---

## R3 hardening (2026-11-12)

**HEAD:** `fe2ec9e053acf54ca68b00f151ec4af926c0778c` plus uncommitted hardening artifacts/tests (**not committed**).  
**Python:** `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` (`3.11.16`)  
**OpenSpec:** standalone `1.17.0` — `implement-r3-solvers-and-eval` valid, Complete, **not archived**.  
**Live LLM:** still not run. Slice C **BLOCKED** (creds unset).  
**R3-04:** PARTIAL (no AST/fuzzy). **R3-07:** PARTIAL (no fresh live Lorne). Unchanged.

Evidence: `eval/results/r3_hardening_regression.md` (run ID `r3hard-D-2026-11-12T0034Z`).

### R3-08 note (accounting, not a new fake_100 execute)

Report layer now accounts **100/100**. Invalid JSON IDs (seed 2026): `syn_004`, `syn_016`, `syn_028`, `syn_040`, `syn_052`, `syn_064`, `syn_076`, `syn_088`.  
`generated=100`, `loaded=92`, `invalid=8`, `solved=59`, `failed=17`, `skipped=16`, `error=0`, score=76. Invariant `59+17+16+0+8=100`. Three workers=5 runs + workers=1 control, all exit 0.  
Historical `eval/results/fake_100.json` (and `fake_100_baseline.json`) remains the **92-loaded** summary (`tasks=100` with no `invalid` bucket). See `eval/results/fake_100_reconciliation.md`. Slice D did **not** re-run fake_100 into run1/run2/run3.

The historical matrix above is unchanged. Hardening statuses:

| Block | Status | Proof |
|---|---|---|
| A 100 IDs | DONE | `fake_100_reconciliation.md`; run1/2/3/w1 jsonl 100 |
| B Budget | DONE (D1 defects remain) | `budget_stress_report.md`, `d1_budget_defects.md` |
| C Live Gateway | BLOCKED | creds unset — `openjiuwen_gateway_live_smoke.md` |
| D Regression | DONE | `r3_hardening_regression.md` |
| Reviewer A/B | PASS-with-notes | LOW/MEDIUM dump fixed |
| OpenSpec | applied, valid, not archived | `openspec list --archived` empty; validate --strict valid |
| R3-04 | PARTIAL | no AST/fuzzy |
| R3-07 | PARTIAL | no fresh live Lorne |

### D verification (this hardening, copies in /tmp)

```
$PY=/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11  (Python 3.11.16)

$PY -m unittest discover -s tests -q
→ Ran 392 in 10.289s, OK (skipped=2), exit 0
  (historical apply: 370 OK, 2 skipped — count grew by A/B tests; failures 0; skips stay 2)

$PY -m unittest discover -s tests/contract -q
→ Ran 47 in 4.839s, OK, exit 0
  (historical apply: 32 OK)

$PY -m unittest tests.test_fake_100 tests.contract.test_budget_exhaustion tests.contract.test_budget_concurrency tests.test_generic_solver -q
→ Ran 34 in 0.058s, OK, exit 0

$PY run_agent.py --tasks-dir /tmp/r3hard-task-dir --offline --max-workers 1 --out /tmp/r3hard-out8
→ exit 0; tasks=8 solved=6 failed=2 skipped=0 score=16.0 tokens_spent=0.0

$PY run_agent.py --tasks-dir /tmp/r3hard-task-dir-extended --offline --max-workers 1 --out /tmp/r3hard-out16
→ exit 0; tasks=16 solved=12 failed=4 skipped=0 score=42.0 tokens_spent=0.0

$PY run_agent.py --tasks-dir /tmp/r3hard-lorne --offline --max-workers 1 --out /tmp/r3hard-out-lorne
→ exit 0; tasks=10 solved=0 failed=10 skipped=0 score=0.0 (expected without LLM)

openspec validate implement-r3-solvers-and-eval --strict
→ Change 'implement-r3-solvers-and-eval' is valid; list --archived: none
```

Official 8 / extended 16 / lorne-offline match the historical Gate 0 numbers. Fixture trees hash-matched before vs after (see regression report). Historical logs `dev3_offline*.log` / `dev3_official8.json` / `fake_100.json` were not overwritten.

### Links

- `eval/results/r3_hardening_initial_state.md`
- `eval/results/fake_100_reconciliation.md`
- `eval/results/budget_stress_report.md`
- `eval/results/d1_budget_defects.md`
- `eval/results/openjiuwen_gateway_live_smoke.md`
- `eval/results/r3_hardening_regression.md`
- `eval/results/r3_hardening_final_review.md` (PASS-with-notes; no CRITICAL/HIGH)
