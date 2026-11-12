# R3 hardening B — BudgetLedger consumer stress

Consumer-only verification of the production public API
`harness.budget.BudgetLedger.reserve` / `settle` / `release` / `snapshot` / `stop`.
D1 implementation files were not edited. OpenSpec was not archived. No commit.

## Production class

All new tests import `BudgetLedger` from `harness.budget` (`__module__ == "harness.budget"`).
They do not copy the ledger. Identity is asserted in:

- `tests/contract/test_budget_exhaustion.py:47`
- `tests/contract/test_budget_concurrency.py:38`

## Concurrency sync and runner cap

- Concurrent bursts use `threading.Barrier` (with a wait timeout). **No `time.sleep`** is used for synchronization.
- Production runner cap `harness/runner.py:77` `MAX_PARALLEL_SESSIONS = 5` was **read, not raised**. B13 uses 8 ledger threads on purpose and asserts the cap is still 5.

## Commands

```
PY=/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11
cd /home/asgaroth/Projects/AI-Coding-Agent
$PY -m unittest tests.contract.test_budget_exhaustion tests.contract.test_budget_concurrency -v
```

Actual result (2026-11-12):

```
Ran 15 tests in 0.033s
OK
```

- **Ran:** 15
- **OK:** 15
- **failed:** 0
- **skipped:** 0
- **exit code:** 0

Verbose methods: 10 exhaustion (including B2 spy + import identity) + 5 concurrency (including cap + import identity).

`git diff --name-only` after these tests does **not** include
`harness/budget.py`, `harness/usage.py`, `harness/openjiuwen_gateway.py`, or `run_agent.py`.

## B1–B13 matrix

| ID | Scenario | Where covered | Result | Notes |
|---|---|---|---|---|
| B1 | Exact fit of one output=10 (cost 10); second reserve exhausted | Existing: `tests/test_budget.py:155` `ReservationTests.test_strict_boundary_allows_exact_fit_only` (reserve, no settle). New sequential reserve+settle then second reserve: `tests/contract/test_budget_exhaustion.py:65` | EXISTING + PASS | New test settles the first request, then `BudgetExhausted` on the second. Gateway/send not involved. |
| B2 | Gateway/send not invoked when reserve does not fit | Existing FakeLedger(`reservation=None`): `tests/test_openjiuwen_gateway.py:487`. New public `OpenJiuwenGateway` + real `BudgetLedger(1.0)` + minimal FakeModel: `tests/contract/test_budget_exhaustion.py:319` | PASS | Spy **not skipped**. `invoke_calls=0`; `_get_model` constructed 1 FakeModel before reserve (not a send). Snapshot: `eval/results/budget_stress/b2_gateway_no_invoke.json`. |
| B3 | 5 threads, Barrier(5), cost 10, total=20 → K=2 | New: `tests/contract/test_budget_concurrency.py:50` | PASS | Exactly 2 granted, 3 `BudgetExhausted`. Left reserved: `reserved=20`, `spent=0`, `spent+reserved<=total`. |
| B4 | 5 threads, cost 2, total=5; remainder 1 cannot fit a third | New: `tests/contract/test_budget_concurrency.py:97` | PASS | Exactly 2 granted, 3 exhausted. After join: `reserved=4`, `remaining=1`, `spent+reserved<=5`. Snapshot also taken inside each worker via `snapshot()`. |
| B5 | Two sequential settles with distinct actuals; spent = sum of formula costs; reserved→0 | Existing: `tests/test_budget.py:164` `test_settle_returns_unused_reserve`. New: `tests/contract/test_budget_exhaustion.py:93` | EXISTING + PASS | Costs 4.0 + 9.0 = 13.0; reserved 0. |
| B6 | Reserve then release; no settle; spent=0; remaining restored | Existing: `tests/test_budget.py:193`. New: `tests/contract/test_budget_exhaustion.py:119`. Contract: `tests/contract/test_budget_contract.py:248` | EXISTING + PASS | Public `release` only. |
| B7 | Timeout after send / unknown usage must not be free | **Not a consumer test.** Gateway `_ledger_release` | DEFECT | `harness/openjiuwen_gateway.py:809-819` ignores `definitely_not_sent` when `release()` exists and always refunds. See `eval/results/d1_budget_defects.md` §1. D1 not patched. |
| B8 | Double settle / double release raise; spent not charged twice | Existing: `tests/test_budget.py:203`. New: `tests/contract/test_budget_exhaustion.py:135`. Contract: `tests/contract/test_budget_contract.py:248` | EXISTING + PASS | Second settle leaves `spent=5.0`. |
| B9 | Missing / malformed usage at settle | Existing lock-in: `tests/test_usage.py:113` `test_missing_usage_normalizes_to_zero` (do not weaken). New consumer: `tests/contract/test_budget_exhaustion.py:162` | DEFECT | `ledger.settle(handle, None)` charges **0** (current fail-open, **not** a v5 fail-closed pass). Negative mapping also charges 0. Unsupported object raises `BudgetError`. Duck-typed negative tokens raise `ValueError` from `TokenUsage` before the lock. |
| B10 | Nested `completion_tokens_details.reasoning_tokens` already included in `completion_tokens` | Existing normalizer: `tests/test_usage.py:42`. New ledger settle: `tests/contract/test_budget_exhaustion.py:211` | EXISTING + PASS | Spent 101.0 (`10/10 + 100`), not 141. |
| B11 | Actual usage cost > reserve; next reserve sees truth; remaining may go `< 0` | Existing (remaining stays ≥0): `tests/test_budget.py:178`, `tests/contract/test_budget_contract.py:234`. New overspend-of-total: `tests/contract/test_budget_exhaustion.py:239` | EXISTING + DEFECT | `total=20`, reserve 10, settle 50 → `remaining=-30`, `stopped=True`, **no** `invariant_violation` event on `snapshot()`. |
| B12 | `stop()` then further `reserve` raises `BudgetExhausted`; spent unchanged | Existing sticky stop via `charge`: `tests/test_budget.py:117`, `tests/contract/test_budget_contract.py:278`. New public `stop()`: `tests/contract/test_budget_exhaustion.py:269` | EXISTING + PASS | Spent stayed 10.0 after stop. |
| B13 | 8 concurrent reserves, K=3, still atomic above runner cap 5 | New: `tests/contract/test_budget_concurrency.py:147`. Existing 16×K=1: `tests/test_budget.py:260`. Mixed reserve/settle/release: `tests/test_budget.py:288` | PASS | 3 granted, 5 exhausted, `reserved=3`. `MAX_PARALLEL_SESSIONS` still 5. |

Related contract already present (not duplicated wholesale): `tests/contract/test_budget_contract.py` `LedgerContractTests` (lines 224–274).

## Snapshots

Sanitized JSON (no URLs, keys, or headers) under `eval/results/budget_stress/`:

- `b1_exact_fit.json`
- `b2_gateway_no_invoke.json`
- `b3_five_threads_k2.json`
- `b4_remainder_one.json`
- `b5_distinct_actuals.json`
- `b6_release_no_settle.json`
- `b8_double_settle_release.json`
- `b9_settle_none_fail_open.json`
- `b10_reasoning_once.json`
- `b11_remaining_negative.json`
- `b12_stop.json`
- `b13_eight_threads_k3.json`

## D1 gaps (not patched)

Documented for Developer 1 in `eval/results/d1_budget_defects.md`:

1. B7 `_ledger_release` always `release()` on `BudgetLedger`.
2. B9 `normalize_usage(None)` / `settle(None)` is free.
3. Gateway retries share one reserve (`generate` docstring ~1238–1240).
4. B11 settle actual>total can make `remaining < 0` with no invariant event.
5. fake_100 / generic / coding use `budget.check`+`charge` unless the LLM backend is `openjiuwen-gateway`.
