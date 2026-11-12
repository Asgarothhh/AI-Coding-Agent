# D3 solvers/eval report (R3-01…R3-08)

**Date:** 2026-11-12  
**HEAD:** `8cbe25f9b90d266458c75eaa8f4b19950e7b9572`  
**Python:** `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` (`Python 3.11.16`)  
**Unit after High fixes:** 370 tests, OK, skipped=2  
**OpenSpec:** `1.17.0` (`openspec validate implement-r3-solvers-and-eval --strict` → valid)  
**Seed (synthetic):** `2026`  
**Live LLM:** not run in this apply.

> **Historical apply block** below is HEAD `8cbe25f9b90d266458c75eaa8f4b19950e7b9572` (verification baseline). Current tree is HEAD `fe2ec9e053acf54ca68b00f151ec4af926c0778c` plus **uncommitted** R3 hardening (not committed). New measured numbers: `eval/results/r3_hardening_regression.md`. R3-04 and R3-07 remain **PARTIAL**. Live LLM still not run.

Hashes of untouched fixtures (after runs on **copies**):

| Tree | sha256 of files |
|---|---|
| `examples/task-dir` | `288f89754791a0139594de989f15b73402e4caf9e8fe7422bfa40b249b5b6f3b` |
| `eval/external/lorne` | `f9f9edd4e9bb14508993fdaa0da5725f7a259f14fee6a1c0332b403217070724` |

## Commands and exit codes

| Command | Exit | Result |
|---|---|---|
| `python3.11 -m unittest discover -s tests` | 0 | 369 tests, 2 skipped, 10.324s |
| `python3.11 -m unittest discover -s tests/contract` | 0 | 32 tests, 4.883s |
| `python3.11 eval/fake_100.py --tasks 100 --workers 5` | 0 | see synthetic |
| `python3.11 run_agent.py --tasks-dir <copy>/task-dir --offline --max-workers 1` | 0 | 6/8 score 16 |
| `python3.11 run_agent.py --tasks-dir <copy>/task-dir-extended --offline --max-workers 1` | 0 | 12/16 score 42 |
| `python3.11 run_agent.py --tasks-dir <copy>/lorne --offline --max-workers 1` | 0 | 0/10 score 0 |
| `openspec validate implement-r3-solvers-and-eval --strict` | 0 | valid |

Logs: `eval/results/dev3_offline8.log`, `dev3_offline16.log`, `dev3_lorne_offline.log`.  
JSON: `dev3_official8.json`, `dev3_extended16.json`, `dev3_lorne_offline.json`, `fake_100.json`.  
JSONL: `eval/results/dev3_tasks.jsonl` (usage=`null` — offline, no LLM).

## Official 8 (offline copy, smoke)

| task_id | status | solver |
|---|---|---|
| math_0000 | solved | math |
| math_0004 | solved | math |
| search_0001 | solved | search |
| search_0005 | solved | search |
| data_analysis_0002 | solved | data_analysis |
| data_analysis_0006 | solved | data_analysis |
| coding_0003 | failed | coding |
| coding_0007 | failed | coding |

- tasks=8 planned=8 solved=**6** failed=2 skipped=0 score=**16.0** tokens_spent=0.0
- Matches Gate 0 baseline and documented offline-without-mutation-repair figure.
- Coding fails without LLM: expected, not a regression.

## Extended 16 (offline copy)

- tasks=16 solved=**12** failed=4 skipped=0 score=**42.0** tokens_spent=0.0
- Failed: coding_0003, coding_0007, coding_0008, coding_0009.
- Same as Gate 0 baseline.

## lorne-10

### Offline this apply (copy of `eval/external/lorne`)

| task_id | status | solver | note |
|---|---|---|---|
| lorne_py-argparse-flag | failed | coding | no LLM |
| lorne_py-docstring-ast | failed | coding | no LLM |
| lorne_py-empty-list | failed | coding | no LLM |
| lorne_py-fix-add | failed | coding | no LLM |
| lorne_py-fix-import | failed | coding | no LLM |
| lorne_py-rename-func | failed | coding | no LLM |
| lorne_py-trace-bug | failed | coding | no LLM |
| lorne_py-write-test | failed | coding | no LLM |
| lorne_read-const | failed | generic | no LLM |
| lorne_read-def | failed | generic | no LLM |

- solved=0 failed=10 score=0.0 tokens_spent=0.0 usage=null
- Status: **offline FAIL expected**. Not used as PASS.

### Live historical (do not treat as this-apply PASS)

Source: `eval/results/external-lorne.json` (timestamp 2026-11-11, `OJ_TOKEN_BUDGET`, model `qwen3.6-35b-a3b-noreason`).

- tasks=10 solved=**10** failed=0 score=**18.0** tokens_spent=1310.5
- Status: **historical**. Not re-run (no D1 live proxy in this apply).

## Synthetic 100 (`eval/fake_100.py`)

- `synthetic: true`, seed=2026, workers=5, budget=100000
- solved=59 failed=17 skipped=16 score=76.0
- tokens_spent=360.0 (prompt 600, completion 300); spent+reserved=360 ≤ 100000
- fake_llm_calls=68; two-run projection matched; 100 temp dirs **not** in git
- Mix: math, coding pass/fail/repair, unknown type, invalid JSON, empty kb, poem/uncertain, existing answer, network/wget impossible, connection-error
- Invalid JSON dirs skipped by loader; empty `answer.txt` still written by `_ensure_answers`
- Fake LLM injects via `RunOptions.llm` into the same `run()` + `BudgetLedger` as runtime. It does **not** wrap `OpenJiuwenGateway` (would need a live/mock proxy). Missing gateway usage is therefore not exercised here.

## Patch size (measured, not claimed)

Tiny coding fixture `def add` (32 bytes original, 32 bytes full-file fix):

- SEARCH/REPLACE hunk including markers: **82 bytes**
- Saving vs full file on this fixture: **negative** (markers dominate)
- Do not claim “40% cheaper”. Unique hunks help on larger files; not measured on lorne live in this apply.

Fuzzy unique match (ratio ≥ 0.92, uniqueness gap) is implemented; AST-based matching is **not**.

## Matrix

See `eval/results/dev3_progress.md`. Change not archived.

---

## R3 hardening (2026-11-12) — additional measured run

**HEAD:** `fe2ec9e053acf54ca68b00f151ec4af926c0778c` plus uncommitted hardening (**not committed**).  
**Python:** `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` (`Python 3.11.16`)  
**OpenSpec:** `1.17.0` — `openspec validate implement-r3-solvers-and-eval --strict` → valid; `openspec list --archived` → none.  
**Live LLM:** still not run. Slice C **BLOCKED** (`eval/results/openjiuwen_gateway_live_smoke.md`).  
**R3-04:** PARTIAL. **R3-07:** PARTIAL. The historical numbers and matrix above are a baseline; they were not deleted.

Run ID: `r3hard-D-2026-11-12T0034Z`. Full table: `eval/results/r3_hardening_regression.md`.

### Commands (hardening D, exit codes)

| Command | Exit | Result |
|---|---|---|
| `$PY -m unittest discover -s tests -q` | 0 | Ran 392, OK, skipped=2, 10.289s |
| `$PY -m unittest discover -s tests/contract -q` | 0 | Ran 47, OK, 4.839s |
| `$PY -m unittest tests.test_fake_100 tests.contract.test_budget_exhaustion tests.contract.test_budget_concurrency tests.test_generic_solver -q` | 0 | Ran 34, OK, 0.058s |
| `$PY run_agent.py --tasks-dir /tmp/r3hard-task-dir --offline --max-workers 1 --out /tmp/r3hard-out8` | 0 | 6/8 score 16.0 tokens 0.0 |
| `$PY run_agent.py --tasks-dir /tmp/r3hard-task-dir-extended --offline --max-workers 1 --out /tmp/r3hard-out16` | 0 | 12/16 score 42.0 tokens 0.0 |
| `$PY run_agent.py --tasks-dir /tmp/r3hard-lorne --offline --max-workers 1 --out /tmp/r3hard-out-lorne` | 0 | 0/10 score 0.0 (expected without LLM) |
| `openspec validate implement-r3-solvers-and-eval --strict` | 0 | valid, not archived |

Logs (new names; historical `dev3_offline*.log` / `dev3_official8.json` / `fake_100.json` not overwritten): `eval/results/r3_hard_offline8.log`, `r3_hard_offline16.log`, `r3_hard_lorne.log`, `r3_hard_unit.log`, `r3_hard_contract.log`, `r3_hard_targeted.log`, `r3_hard_openspec.log`.

This-run fixture TREE_HASH (sorted sha256 listing; before==after):

| Tree | files | TREE_HASH |
|---|---:|---|
| `examples/task-dir` | 88 | `7ccb9a4c5caf998de60f985902fd11abbd0eafe011ec5e015f9f12554ae49e1f` |
| `examples/task-dir-extended` | 195 | `78ed850e5157a1af0d6a5e390ed93006ea93c44e671a9a22df486aaf5b93d634` |
| `eval/external/lorne` | 41 | `02edb31ce908b283e091638ce347a5e2f5a81f8f0b52678417bde3f319d95ccc` |

The hashes at the top of this file are the **historical** apply hashes (different method). Both sets describe untouched originals; D proved before==after with the method in `r3_hardening_regression.md`.

### R3-08 (100/100 accounting)

Historical synthetic section above is the 92-loaded writer (`fake_100.json`: solved=59 failed=17 skipped=16 score=76, `tasks=100`). After slice A the report layer accounts all 100 IDs: 8 invalid JSON (`syn_004/016/028/040/052/064/076/088`) + 92 loaded. Cited, not re-run: `eval/results/fake_100_reconciliation.md` and `fake_100_run{1,2,3,w1}/`.

### Status matrix (hardening)

| Block | Status | Proof |
|---|---|---|
| A 100 IDs | DONE | jsonl 100/100, eight invalid named |
| B Budget | DONE (D1 defects remain) | consumer tests + defect report |
| C Live Gateway | BLOCKED | creds unset |
| D Regression | DONE | this run, no baseline miss |
| Reviewer A/B | PASS-with-notes | LOW/MEDIUM dump fixed |
| OpenSpec | applied, valid, not archived | |
| R3-04 | PARTIAL | no AST/fuzzy |
| R3-07 | PARTIAL | no fresh live Lorne |

### Links

- `eval/results/r3_hardening_initial_state.md`
- `eval/results/fake_100_reconciliation.md`
- `eval/results/budget_stress_report.md`
- `eval/results/d1_budget_defects.md`
- `eval/results/openjiuwen_gateway_live_smoke.md`
- `eval/results/r3_hardening_regression.md`
- `eval/results/r3_hardening_final_review.md` (PASS-with-notes; no CRITICAL/HIGH)
