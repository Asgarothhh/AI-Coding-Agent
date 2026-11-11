# D3 solvers/eval report (R3-01…R3-08)

**Date:** 2026-11-12  
**HEAD:** `8cbe25f9b90d266458c75eaa8f4b19950e7b9572`  
**Python:** `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` (`Python 3.11.16`)  
**Unit after High fixes:** 370 tests, OK, skipped=2  
**OpenSpec:** `1.17.0` (`openspec validate implement-r3-solvers-and-eval --strict` → valid)  
**Seed (synthetic):** `2026`  
**Live LLM:** not run in this apply.

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
