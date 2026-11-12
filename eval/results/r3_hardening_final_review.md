# R3 hardening final review (A+B+C+D)

**Verdict: PASS-with-notes**

Independent reviewer: [Reviewer](615c83da-2260-413a-ac62-a00ae1c03170).  
Date: 2026-11-12. HEAD `fe2ec9e053acf54ca68b00f151ec4af926c0778c` plus uncommitted D3 hardening. **Not committed. OpenSpec not archived.**

No **CRITICAL** or **HIGH** findings. Slice A accounts 100/100 IDs with the eight named invalid JSON skips. Slice B hits production `BudgetLedger` and documents D1 gaps without patching them. Slice C is an honest **BLOCKED** (0 live calls). Slice D logs match the claimed regression numbers. OpenSpec `implement-r3-solvers-and-eval` is valid, Complete, and **not archived**. R3-04 and R3-07 remain **PARTIAL**.

Prior slice reviews: [Reviewer A](2590a0d3-a3da-410c-9214-cb1f86068d8c) PASS-with-notes; [Reviewer B](b6fdedcf-f2e4-4784-af4f-53228f67b3d5) PASS-with-notes (MEDIUM dump later fixed). Architect map: [Architect](8d44258d-8509-4a5e-9927-d0a0d6ccc0f4).

---

## Findings

### CRITICAL

None.

### HIGH

None.

### MEDIUM

None open.

**Prior Reviewer-B MEDIUM — `_dump` on read-only FS: FIXED.**  
`tests/contract/test_budget_exhaustion.py:34-42` and `tests/contract/test_budget_concurrency.py:31-39` wrap `mkdir`/`write_text` in `try/except (OSError, PermissionError)` and return. This reviewer re-ran the 22 targeted tests in a read-only sandbox: **22 OK, exit 0**. Snapshots still exist under `eval/results/budget_stress/` from writable implementer runs.

### LOW (open, non-blocking)

**LOW-1 (Reviewer A) — `test_missing_id` overlaps missing-terminal**  
- Where: `tests/test_fake_100.py:103-115` vs `:148-166`  
- Both load the missing ID into `loaded_ids` and omit the runner row.  
- Recommendation: assert `invalid` when the ID is absent from `loaded_ids`; add one extra-runner-id case.

**LOW-2 (Reviewer A) — unloaded IDs always get `load_skipped:invalid_json`**  
- Where: `eval/fake_100.py:29` (`INVALID_LOAD_REASON`), `:299-308`  
- Correct for seed 2026 bucket 4. Misleading if a later generator adds a non-JSON skip.  
- Recommendation: gate the reason on `kind == "invalid_json"` or pass through the loader exception.

**LOW-3 (residual) — contract tests still rewrite `eval/results/budget_stress/*.json` on a writable tree.** Best-effort I/O no longer fails the suite.

### Prior A/B notes — status

| Note | Source | Status |
|---|---|---|
| `_dump` ERROR on read-only FS after assertions | Reviewer B MEDIUM | **FIXED** (readonly 22 OK) |
| LOW-1 missing-id overlaps missing-terminal | Reviewer A | **OPEN** |
| LOW-2 hardcoded `load_skipped:invalid_json` | Reviewer A | **OPEN** |
| B2 `assertGreaterEqual(len(factory.models), 0)` tautology | Reviewer B LOW | **FIXED** (`len(factory.models)==1`, `invoke_calls==0`, `reserved==0`, `spent==0`) |

---

## Checklist

| Block | Status | Evidence |
|---|---|---|
| **A 100 IDs** | **DONE** | Four `tasks.jsonl` are 100 unique `syn_000`…`syn_099`. Invalid: `syn_004/016/028/040/052/064/076/088`, `raw_status=null`, `reason=load_skipped:invalid_json`, bucket 4. Cause is generate → `load_tasks` JSON skip + old writer hole, **not** budget/timeout. Summary from records: `generated=100 loaded=92 invalid=8 solved=59 failed=17 skipped=16 error=0`; `59+17+16+0+8=100`. `score=76.0`. 3× workers=5 seed 2026 + workers=1 agree. Historical `fake_100.json` / `fake_100_baseline.json` sha256 `055a41300681e80160ba9538159773ea5eace5eab0e0fe004d1f20ffea235a79`. |
| **B Budget** | **DONE (D1 defects remain)** | Tests import `harness.budget.BudgetLedger`. No D1 edits. B2 invoke_calls=0. B3/B4/B13 Barrier, cap 5 not raised. B8 no double-charge. B10 spent 101.0 not 141.0. B7/B9/B11 in `d1_budget_defects.md`. |
| **C Live Gateway** | **BLOCKED** | `openjiuwen_gateway_live_smoke.md`: `live_calls=0`, usage `null`. `LLM_BASE_URL`/`LLM_API_KEY` unset. `sdk_smoke.py` not run. Generic `expected` redaction still in `tests/test_generic_solver.py:263-273`. No secrets in artifacts. |
| **D Regression** | **DONE** | Unit **392 OK, skipped=2, exit 0**; contract **47 OK**; offline 8 **6/8 score 16.0 tokens 0**; 16 **12/16 score 42.0**; lorne **0/10** expected. OpenSpec valid, not archived. |
| **OpenSpec** | applied, valid, **not archived** | `openspec validate implement-r3-solvers-and-eval --strict` valid. `list --archived` none. |
| **R3-04** | **PARTIAL** | No AST/fuzzy in default ladder. |
| **R3-07** | **PARTIAL** | No fresh live Lorne. C BLOCKED is not live Lorne 10/10. |

---

## Commands actually run (final reviewer)

```
git rev-parse HEAD
# fe2ec9e053acf54ca68b00f151ec4af926c0778c

git diff --name-only -- harness/budget.py harness/usage.py harness/openjiuwen_gateway.py \
  harness/llm.py run_agent.py harness/solvers harness/patch.py harness/planner.py \
  harness/policy.py openspec/
# empty

$PY -m unittest tests.test_fake_100 tests.contract.test_budget_exhaustion \
  tests.contract.test_budget_concurrency -q
# Ran 22 tests in 0.047s  OK  exit=0

openspec validate implement-r3-solvers-and-eval --strict
# valid  exit=0

openspec list --archived
# No archived changes found.

sha256sum eval/results/fake_100.json eval/results/fake_100_baseline.json
# 055a41300681e80160ba9538159773ea5eace5eab0e0fe004d1f20ffea235a79  (both)
```

`$PY` = `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11`. Live LLM was not run. `openspec archive` was not run. No commit.

---

## Unverified (explicit)

- Full `unittest discover` not re-run by the final reviewer (D logs + targeted 22 OK).
- Offline 8/16/lorne `run_agent.py` not re-run by the final reviewer (D logs).
- Live gateway path (creds unset).
- Docker / image size / lockfiles (out of scope, untouched).

D1 B7/B9/B11 remain for Developer 1. Do not archive the OpenSpec change while R3-04/R3-07 are PARTIAL.
