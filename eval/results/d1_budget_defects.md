# D1 budget defects (R3 hardening B)

Read-only findings for Developer 1. **Do not treat this file as a patch.**
Consumer tests import production `BudgetLedger` and document current behavior.
`tests/test_usage.py::test_missing_usage_normalizes_to_zero` was not weakened.

No secrets. No D1 files were modified in this slice.

---

## 1. B7 — `_ledger_release` drops `definitely_not_sent` and always refunds

**File:line:** `harness/openjiuwen_gateway.py:809-819` (`_ledger_release`).
Call site on failed `generate`: `harness/openjiuwen_gateway.py:1292-1296`.

**Expected (v5 / fail-closed):** release unused reserve only when the request is
proven not sent (`definitely_not_sent=True`). If the invoke may have reached the
proxy (timeout after send, unknown error) and usage is missing, do **not** treat
the call as free: keep the reservation or settle a conservative estimate.

**Actual:** when the ledger exposes `release` (production `BudgetLedger` does),
`_ledger_release` calls `method(reservation)` and **returns**. The
`definitely_not_sent` argument is unused on that path. `release_failed(...,
definitely_not_sent=...)` is reached only if `release` is missing.

So a timeout / unknown failure after `invoke` still refunds the whole reserve
on `BudgetLedger.release()`, even when `classify_error` set
`definitely_not_sent=False` (`harness/openjiuwen_gateway.py:545`, `:549`, `:556`,
`:560`).

**Repro (conceptual, D1 owns the fix):** construct `OpenJiuwenGateway` with a
real `BudgetLedger` and a FakeModel whose `invoke` raises a non-retryable
timeout-like error after the send path; `max_retries=0`. Observe
`ledger.reserved == 0` and `ledger.spent == 0` after the exception — a possibly
paid call is free. Do not add this as a D1 patch from the B-slice.

**Recommendation:** on `BudgetLedger`, call `release()` only when
`definitely_not_sent is True`. Otherwise `settle` a conservative estimate (the
reserved amount) or refuse to drop the reservation. Prefer a `release_failed`
path that honours the flag even when `release` exists.

---

## 2. B9 — missing usage normalizes to zero; `settle(None)` is free

**File:line:** `harness/usage.py:160-176` (`normalize_usage`).
Ledger consumer: `harness/budget.py:96-102` (`_coerce_usage` → `normalize_usage`
for `None` / mapping) and `harness/budget.py:380-418` (`settle`).

**Expected (v5 fail-closed):** unknown / missing / incomplete usage is not a
zero-cost call. Keep the reserve or charge a conservative estimate; mark the
record estimated.

**Actual:** `normalize_usage(None)` returns `TokenUsage()` with `total_cost=0.0`.
`BudgetLedger.settle(handle, None)` therefore charges **0**, restores remaining,
and closes the reservation as settled.

Measured by `tests/contract/test_budget_exhaustion.py:162` (current behavior,
**not** a v5 pass):

- `settle(handle, None)` → charged 0.0
- negative token mapping `{"prompt_tokens": -5, "completion_tokens": -3}` →
  charged 0.0 (`_as_int` rejects negatives)
- unsupported object (`object()`) → `BudgetError` ("unsupported usage object");
  reservation stays active because `_coerce_usage` runs before the lock
- duck-typed negative `input_tokens` → `ValueError` from `TokenUsage.__post_init__`,
  not `BudgetError`

Existing lock-in (do not “fix” by weakening):
`tests/test_usage.py:113` `test_missing_usage_normalizes_to_zero`.

**Repro:**

```python
from harness.budget import BudgetLedger
ledger = BudgetLedger(100.0)
h = ledger.reserve("r", 0, 10)
assert ledger.settle(h, None) == 0.0
assert ledger.spent == 0.0
```

**Recommendation:** change `settle` (and/or `normalize_usage`) together with the
unit test that currently requires zero. Fail-closed options: refuse `None` with
`BudgetError`, settle the reserved amount, or keep the reservation open.
Do not leave `test_missing_usage_normalizes_to_zero` asserting 0 if the
normalizer becomes fail-closed.

---

## 3. Gateway retries share one reserve (not per-retry)

**File:line:** `harness/openjiuwen_gateway.py:1238-1240` (`generate` docstring);
implementation `harness/openjiuwen_gateway.py:1153-1156` (`_invoke_with_retries`);
reserve once at `harness/openjiuwen_gateway.py:1263-1271`.

**Expected (v5 / global-token-budget skill):** each retry is a separate
`reserve` and a separate charge. A paid 429/5xx attempt that produced usage
must not be invisible.

**Actual:** one reservation is created before `_invoke_with_retries`. Transient
retries reuse that reservation. Success settles once; an exhausted retry chain
calls `_ledger_release` once. Intermediate attempts are not reserved or settled.

**Repro:** gateway `max_retries>=1`, FakeModel that fails once with a retryable
error then succeeds. Ledger events are a single `reserve` then one `settle`
(already covered on FakeLedger in `tests/test_openjiuwen_gateway.py`, not a
per-retry ledger cycle).

**Recommendation:** reserve/settle each attempt, or at least settle any attempt
that returned usage. If the proxy account forbids overlapping in-flight
reserves, release then re-reserve between attempts so spent still tracks
attempts that were billed.

---

## 4. B11 — settle actual > reserve can make `remaining < 0`; no invariant event

**File:line:** `harness/budget.py:380-418` (`settle` adds `actual` even when it
exceeds the reservation and the total). Snapshot fields:
`harness/budget.py:512-522` — `total/spent/reserved/remaining/prompt_tokens/completion_tokens/stopped` only.

**Expected:** either keep `spent + reserved <= total` after settle, or emit an
explicit invariant-violation event when a paid response overshoots the global
budget so operators can see it. Subsequent `reserve` must still observe the
true remaining (this last part already works).

**Actual:** overshoot is allowed (response cannot be un-sent). With
`total=20`, `reserve("b11", 0, 10)`, settle usage cost 50:

- `spent=50.0`
- `reserved=0.0`
- `remaining=-30.0`
- `stopped=True` (because remaining ≤ constructor `reserve` default 0)
- `snapshot()` has **no** `invariant_violation` field or event

Measured: `tests/contract/test_budget_exhaustion.py:239` and
`eval/results/budget_stress/b11_remaining_negative.json`.

The older unit test `tests/test_budget.py:178` uses `total=100`, settle 95,
`remaining=5` — it proves “next reserve sees truth” but does **not** cover
negative remaining.

**Repro:**

```python
from harness.budget import BudgetLedger
ledger = BudgetLedger(20.0)
h = ledger.reserve("b11", 0, 10)
ledger.settle(h, {"prompt_tokens": 0, "completion_tokens": 50})
assert ledger.remaining == -30.0
```

**Recommendation:** keep charging the paid actual (cannot un-send), but surface
an invariant event / trace flag when `remaining < 0`. Consider clamping the
**reported** remaining for summaries without hiding spent. Do not silently
drop the extra actual.

---

## 5. fake_100 / generic / coding use `check` + `charge` unless LLM backend is gateway

**File:line:**

- Generic: `harness/solvers/generic.py:508` `ctx.budget.check(...)`,
  `harness/solvers/generic.py:543` `ctx.budget.charge(...)`
- Coding: `harness/solvers/coding.py:596` `check`, `harness/solvers/coding.py:609` `charge`
- Runner switch: `harness/runner.py:369-372` — `gateway_accounting` only when
  `llm.backend_name == "openjiuwen-gateway"`; then `_GatewaySolverLedger.charge`
  (`harness/runner.py:114-137`) is a no-op on the ledger (gateway already
  reserved/settled). Otherwise solvers talk to `BudgetLedger.check`/`charge`
  directly.
- fake_100: `eval/fake_100.py` injects `RoutingFakeLLM` (no
  `backend_name == "openjiuwen-gateway"`), so the E2E 100-task mock uses
  check+charge, not gateway reserve/settle.

**Expected:** every LLM call, including retries, goes through
`OpenJiuwenGateway` + `BudgetLedger.reserve`/`settle`. Advisory `check`+`charge`
must not be the production accounting path.

**Actual:** production default `--llm-backend auto` in `harness/llm.py` does not
select the gateway. fake_100 and offline/fake LLM runs never hit
`OpenJiuwenGateway`, so the ledger public reserve API is unused for those
solvers. `charge` mutates spent without a matching reservation, so a parallel
`reserve` from another path could race against post-hoc charges (mitigated in
practice because only one accounting style is active per run).

**Repro:** run `eval/fake_100.py` or generic/coding with a non-gateway `LLMClient`.
Inspect solver frames: `check` then `chat` then `charge`. No `reserve`/`settle`.

**Recommendation:** production path `--llm-backend gateway` (already required
for v5). Keep `check`+`charge` only as legacy for stdlib/fake clients. When
gateway accounting is on, solvers must not double-charge (the
`_GatewaySolverLedger` shim already tries this — keep it, and do not route
fake_100 through it unless the fake client pretends to be the gateway).
