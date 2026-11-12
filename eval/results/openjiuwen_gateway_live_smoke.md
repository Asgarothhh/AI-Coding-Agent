# Slice C — live OpenJiuwen gateway smoke (generic)

Date: 2026-11-12
Python: `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11`
Sanitized companion: `eval/results/openjiuwen_gateway_live_smoke.json`

## Status

**BLOCKED** — live run was not started.

Reason (no secret values): in the real user process environment (not the sandbox),
`LLM_BASE_URL` is **unset** and `LLM_API_KEY` is **unset**. The presence check
was:

```
$PY -c "import os; print('LLM_BASE_URL', 'set' if (os.environ.get('LLM_BASE_URL') or '').strip() else 'unset'); print('LLM_API_KEY', 'set' if (os.environ.get('LLM_API_KEY') or '').strip() else 'unset')"
```

Output: `LLM_BASE_URL unset` / `LLM_API_KEY unset`.

No `.env` in the working copy. `LLMConfig.from_env` reads only those two
process-env names. Per the slice C gate, a missing credential is a hard stop:
do not invent a live success, do not hit the proxy, do not run
`scripts/sdk_smoke.py`.

Transport: **not run**. Semantic: **not run**.

## Intended production path (not executed)

| Item | Intended value |
|---|---|
| Entrypoint | `run_agent.py` → generic solver (`task_type=mystery` → unknown → `harness.solvers.generic`) |
| `--llm-backend` | `gateway` (mandatory; `auto` would use `OpenJiuwenLLMClient`) |
| `backend_name` if LIVE | `openjiuwen-gateway` |
| Model | `qwen3.6-35b-a3b-noreason` (`--model-policy fixed`) |
| Workers / tokens / budget | `--max-workers 1 --max-tokens 32 --budget 50 --max-iterations 1` |
| Task | one temp dir, not under `examples/`: `gw_smoke_001`, `check=exact_str`, reward 1, instruction asks for exactly `OPENJIUWEN_SMOKE_OK`, **no** `expected` field |
| Generic retry | `range(2)` in `generic.py` left untouched; 1 call intended, 2 if format retry fires |

Command that was **not** run:

```
$PY run_agent.py --tasks-dir <tmp-smoke> --out <tmp-out> \
  --llm-backend gateway --model-policy fixed \
  --model qwen3.6-35b-a3b-noreason --max-workers 1 --max-tokens 32 \
  --budget 50 --max-iterations 1
```

## Evidence (what was and was not observed)

| Field | Value |
|---|---|
| status | BLOCKED |
| production entrypoint | `run_agent.py` + generic solver (intended; not invoked) |
| `llm_backend` / `backend_name` | not observed (run not started). LIVE would require `openjiuwen-gateway` |
| model id | intended `qwen3.6-35b-a3b-noreason`; unused |
| proxy answered | unknown / not probed (no HTTP, no SDK, no smoke script) |
| answer empty? | n/a — no `answer.txt` |
| equals `OPENJIUWEN_SMOKE_OK`? | n/a |
| `expected` in task.json | n/a — temp task was not created |
| `expected` in prompt traces | n/a |
| usage | `null` / usage unavailable — never substitute 0 for a missing live fact |
| ledger spent / reserved / remaining | n/a |
| live_calls | 0 |
| `scripts/sdk_smoke.py` | **not run** (default would hit 4 models) |
| generic.py retry loop | **not patched** |

## Leak scan

Not applicable: no `--out` directory, no logs, no process dump. The presence
check printed only `set`/`unset`. Values of `LLM_BASE_URL` / `LLM_API_KEY` were
never printed (`env` / `printenv` were not used).

## Why this is not a live failure

A live **FAIL** would mean the gateway path ran and the proxy/model answered
wrongly. Here the gate never opened: both contest credentials are absent from
the user environment, so a LIVE or transport-LIVE claim would be fabricated.
Re-run this slice after exporting `LLM_BASE_URL` and `LLM_API_KEY` (do not
commit them).
