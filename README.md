# openJiuwen Challenge 2026 — budget-aware harness MVP

A Python 3.11 harness with a stdlib-only core and an optional openJiuwen
model layer. The final image installs `openjiuwen==0.1.20.post1`
(`requirements-openjiuwen.txt`). v5 sends production LLM calls through
`OpenJiuwenGateway` (`--llm-backend gateway`); `auto` remains compatible and
uses openJiuwen when present, otherwise the stdlib urllib client.

```bash
python /app/run_agent.py --tasks-dir /data/tasks
```

The container is expected to be read-only, the tasks directory writable,
`HOME=/tmp`, no internet, and only an LLM proxy via `LLM_BASE_URL` /
`LLM_API_KEY`. The harness only writes inside the tasks directory, the
output directory (`$OUT` or `/tmp/openjiuwen-out` by default), and `/tmp`.

## Quick start

```bash
~/.local/bin/python3.11 run_agent.py \
  --tasks-dir examples/task-dir \
  --dry-run --offline

~/.local/bin/python3.11 run_agent.py \
  --tasks-dir examples/task-dir \
  --offline --max-workers 1
```

`--dry-run` prints the ordered plan as JSON and writes no answers.
`--offline` disables every LLM call; the six deterministic official examples
are still solved with **zero tokens**.

## Layout

```
run_agent.py                     CLI entry point
entrypoint.sh                    exec "$@", normalises HOME, no side effects
harness/
  task.py                        Task dataclass, BOM-safe load_tasks()
  usage.py                       normalised token usage records
  budget.py                      BudgetProvider + atomic reserve/settle/release ledger
  features.py                    task features for the cost estimator
  estimator.py                   calibrated per-type median cost estimator
  calibration.json               frozen calibration data
  policy.py                      selection policies + planning reserve
  planner.py                     deterministic-first plan over policy decisions
  llm.py                         stdlib urllib client + backend factory
  ojiuwen_llm.py                 openJiuwen Model adapter (async -> sync)
  openjiuwen_gateway.py          v5 production gateway on the openJiuwen Core SDK
  triage.py                      zero-LLM infeasibility scoring
  solvers/
    base.py                      SolveResult / SolverContext
    math.py                      chain parser + safe arithmetic evaluator
    data_analysis.py             deterministic CSV aggregation
    search.py                    markdown KB retrieval + relation chains
    coding.py                    test execution, hash protection, repair loops
  store.py                       atomic answer.txt + solutions.json
  runner.py                      orchestration, ThreadPoolExecutor, summary
  trace.py                       JSONL trace
  mock_proxy.py                  local OpenAI-compatible proxy for tests
  ojiuwen_runtime.py             optional DeepAgent adapter (build_agent/run_query)
Dockerfile                       python:3.11-slim image with openjiuwen pinned
scripts/build.sh                 build hack:latest + openjiuwen-hack:latest
scripts/run_local.sh             offline hardened smoke run
scripts/sdk_smoke.py             live gateway smoke (credentials via env)
requirements.txt                 runtime SDK pin (openjiuwen==0.1.20.post1)
requirements-openjiuwen.txt      openjiuwen==0.1.20.post1
tests/                           stdlib unittest suite (250 tests, 2 skipped)
tests/contract/                  32 contract tests (paths, budget, estimator, policy, parallel, time limit, E2E)
examples/task-dir/               eight official examples
examples/task-dir-extended/      16 tasks (8 official + 8 additional)
```

## Contract details implemented

### Answers

* `answer.txt` is written next to `task.json` **and** into `workspace/` when
  it exists.
* Content is one plain-text line with no explanation.
* Writes are atomic: temp file in the destination directory, `flush`,
  `fsync`, `os.replace`, directory `fsync`.
* Infeasible tasks, failed coding tasks, and budget/time-skipped tasks get no
  `answer.txt`.
* The `expected` field in `task.json` is **never read by production code**;
  only the test-suite reads it as ground truth.

### Budget

There is exactly **one global budget** and **no per-task cap**.

`BudgetProvider` resolves the total once at startup with the priority
`--budget` > `OJ_TOKEN_BUDGET` > `BUDGET`. Without a value and without
`--offline`, the CLI exits with a configuration error before any LLM call.
With `--offline` only, `100000` is a **local test assumption**
(`budget_source=offline-default`) and a warning is printed.

Every LLM call reserves its worst case, then settles against actual usage:

```
cost = prompt_tokens / 10 + completion_tokens
```

Reasoning tokens are completion tokens and are not counted twice; cache
accounting is ignored. `BudgetLedger` implements atomic `reserve`, `settle`
and `release` under one lock and maintains `spent + reserved <= total` for
every issued reservation. A paid response may settle above its estimate; the
excess is charged and never hidden, and later reservations see the reduced
budget. Overspending stops new tasks but never crashes an already paid
response. Parallel sessions share the single ledger safely.

The summary exposes `budget_source` and a `budget` block: `total`, `spent`,
`reserved`, `remaining`, `prompt_tokens`, `completion_tokens`, `stopped`.

### Triage

Scoring is deterministic and never calls an LLM. It raises the infeasibility
score for:

* missing referenced files (`kb/`, `*.csv`, `mylib.py`, ...);
* a missing or empty `kb/` knowledge base;
* coding tasks with no `test_*.py` / `*_test.py`;
* URLs, downloads, web search, or internet requirements;
* contradictory / insufficient-data markers;
* CSV column or row-count mismatches;
* math instructions without numeric data.

`--triage-threshold` defaults to `0.6`. A task at or above the threshold is
skipped and recorded with reasons.

### Planning

`harness/estimator.py` estimates cost from `harness/features.py` plus the
frozen `harness/calibration.json`. Fallback medians are tried in order:
`(solver, type, model)` -> `(solver, type)` -> global -> prompt heuristic.
Confidence is `high` at >=20 samples, `medium` at 8–19, `low` below 8.
Deterministic tasks cost zero. An in-memory online correction exists but is
**off by default** because it made held-out estimates worse.

Held-out lorne set (42 rows, split by `task_id`):

| Theory | MAE | MAPE |
|---|---:|---:|
| per-type median (chosen) | 1836 | 43.1% |
| global / per-model | 1873 | 44.4% |
| prompt heuristic | 3540 | 77.8% |
| online correction | 2191 | 52.3% |

The per-type median also has WAPE 42.4%. It wins, so it is the default.

`harness/policy.py` selects tasks under the global budget. The default is
`value_cost_greedy`: value is `reward * P_solve`, cost is the planned cost under
`--cost-basis expected` (the default) with `--cost-basis upper` available for
conservative upper-bound planning, and it plans against `remaining` minus a planning
reserve of 5% (10% when low-confidence tasks dominate). `--planner-reserve` overrides
the reserve. Available policies: `accept_all`, `reward_sort`, `value_cost_greedy`,
`knapsack_dp`. Planning makes no LLM or network calls.

Simulation at 20k / 50k / 100k budgets:

| Policy | Total reward | Overspend | Note |
|---|---:|---:|---|
| `value_cost_greedy` | 250.0 across the three scenarios | 0 | chosen default |
| `knapsack_dp` | 250.0 | 0 | identical selected set |
| `reward_sort` | lower expected value | 0 | — |

### Deterministic solvers

* **math**: sequential parser for the official chain templates
  (`starts with N`, `split equally into K (floor)`, `doubled`,
  `ships X per day for D days`, `receives D deliveries of X`) plus a
  restricted `ast`-based arithmetic evaluator (no `eval`, bounded exponents).
* **data_analysis**: CSV filters for product/region/month range and
  sum/count/average/min/max/revenue aggregation.
* **search**: markdown `Key: value` retrieval, entity lookup by document
  title/filename, and relation chains such as
  `person -> Employer -> company -> Headquarters`.

These solvers cost **0 tokens** and are covered by tests against all six
official non-coding examples.

### Coding solver

1. Run every test file and snapshot their SHA-256 hashes.
2. Optionally (`--mutation-repair`) try a bounded deterministic search over
   tiny mutations: `+=`/`-=`, `<`/`>`, `==`/`!=`, parity, `sorted`/`reversed`,
   `+`/`-`.
3. Otherwise ask the configured LLM for a full-file JSON patch, apply it,
   verify the test hashes are unchanged, and rerun the tests.
4. At most `--max-iterations` (default 6) LLM attempts.
5. Return `DONE` only when all tests pass. On any failure no `answer.txt` is
   written.

Test files are never patched; if they change on disk they are restored from
the snapshot before the solver gives up.

### LLM backends

The `gateway` backend is the v5 production path. `stdlib` and `openjiuwen`
remain compatible transports; all three implement the same
`chat(messages, *, model, max_tokens, temperature, attempt) -> LLMResponse`
contract.

**gateway** (`harness/openjiuwen_gateway.py`, `OpenJiuwenGateway`)

* v5 production path: every production LLM call goes through
  `OpenJiuwenGateway`, built on the `openjiuwen` Core SDK.
* Only four models are allowed: `qwen3.8-27b`, `qwen3.8-27b-noreason`,
  `qwen3.6-35b-a3b`, `qwen3.6-35b-a3b-noreason`.
* No silent fallback. An unavailable model, gateway error, or SDK import
  failure raises `LLMError`; the gateway never switches model or endpoint on
  its own.
* Selected with `--llm-backend gateway`. `auto` remains compatible and keeps
  the existing resolution order.
* Live smoke and live runs are **verified**: `scripts/sdk_smoke.py` reached the
  organizer proxy for all four aliases, and the container solved 8/8 official
  tasks and 16/16 extended tasks through this backend (see Verification and
  `eval/results/live-v5.md`). Credentials were supplied via the environment at
  run time only; nothing is committed.

**stdlib** (`harness/llm.py`, `LLMClient`)

* `urllib.request` only; no SDK dependency.
* OpenAI-compatible `POST /v1/chat/completions`.
* Retries `408/409/425/429/5xx` and network errors with exponential backoff
  and optional `Retry-After`; client errors are not retried.
* Default model `qwen3.6-35b-a3b-noreason`, escalation model `qwen3.8-27b`
  after a configurable number of attempts.
* Usage is parsed from the response; if missing, a deterministic character
  estimate is used and marked `estimated=True`.

**openjiuwen** (`harness/ojiuwen_llm.py`, `OpenJiuwenLLMClient`)

* Lazy import of `openjiuwen.core.foundation.llm.Model`; the stdlib harness
  and tests do not require the SDK.
* Builds `Model` from `ModelClientConfig(client_provider="OpenAI", ...)` and
  `ModelRequestConfig(model_name=..., temperature=..., max_tokens=...)`.
* One persistent background event loop per client (`loop.run_forever()`).
  Each `Model` is built on that loop, every `invoke` is submitted via
  `asyncio.run_coroutine_threadsafe`, and the same loop is reused for all
  calls and models.
* `close()` stops the loop and closes cached model resources when an async
  `aclose`/`close` exists; it is idempotent. Concurrent calls from the runner's
  thread pool are safe because all work is serialized onto the same loop.
* Maps `usage_metadata`: `prompt_tokens = input_tokens`,
  `completion_tokens = output_tokens`. If `output_tokens == 0` and
  `reasoning_tokens > 0`, reasoning tokens become the completion count.
  Reasoning is not added twice.
* Same default/escalation model policy as the stdlib client.
* Honours `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
  `LLM_ESCALATION_MODEL`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`, `LLM_TIMEOUT`,
  `LLM_VERIFY_SSL` (default true), and optional `OJ_EXTRA_BODY` (JSON object).
* `api_base` is passed to the SDK verbatim; the SDK does **not** append
  `/v1`.
* The API key is never logged; `repr(client)` omits it.
* Import or API failures raise `LLMError` with a clear message.

**openjiuwen logging.** Before the first `Model` is created, the adapter calls
`configure_log_config` from `openjiuwen.core.common.logging.log_config` (the
version-specific API in 0.1.20.post1) and selects:

* `log_path=/tmp/openjiuwen-logs` (created on demand);
* `level=WARNING`;
* file output for `common`, `interface` and `performance` logs;
* no stdout console handler — any stdout stream handler is removed and
  WARNING/ERROR records are mirrored to stderr.

This prevents writes to the read-only `/app/logs` and keeps stdout as a single
JSON object. Log files may be 0 bytes at WARNING when nothing is logged, which
is normal. The JSON summary is printed before the LLM client is closed, and
shutdown diagnostics go to stderr.

`build_llm_client(config, backend="auto")` selects the transport:

* `gateway` — `OpenJiuwenGateway` through `GatewayChatAdapter`; no fallback;
* `stdlib` — always `LLMClient`;
* `openjiuwen` — `OpenJiuwenLLMClient`, or `LLMError` when the package is not
  installed;
* `auto` — openJiuwen when `importlib.util.find_spec("openjiuwen")` succeeds,
  otherwise stdlib (backward-compatible, not the v5 production path).

The chosen backend is printed on stderr and echoed in the final JSON summary as
`llm_backend`.

Example openJiuwen configuration:

```bash
export LLM_BASE_URL="$LLM_PROXY_URL"
export LLM_API_KEY   # value supplied via the environment, never committed
export OJ_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'
python run_agent.py --tasks-dir /data/tasks --llm-backend gateway
```

### Runner

* `ThreadPoolExecutor` with at most **5** parallel sessions
  (`--max-workers`, default 4).
* Before each task: time-limit and budget check.
* Tasks already queued when the budget/time limit hits are recorded as
  skipped, without answers.
* Writes `trace.jsonl` and `solutions.json` incrementally with `fsync`.
* Final JSON summary on stdout includes solved/skipped/failed/errors, score,
  tokens spent, prompt/completion totals, stop reason, per-type stats,
  `budget_source`, `budget`, `policy`, `planner` and `estimator`.
  `planner` covers accepted/rejected/reasons/reserve/planned_cost/
  expected_reward/coverage; `estimator` covers model/total/variant/confidence;
  `budget` covers total/spent/reserved/remaining/prompt_tokens/
  completion_tokens/stopped.

## CLI

```
--tasks-dir PATH          required
--out PATH                directory or solutions.json path ($OUT / /tmp/openjiuwen-out)
--budget FLOAT            total budget; --budget > $OJ_TOKEN_BUDGET > $BUDGET (required unless --offline)
--reserve FLOAT           keep untouched (default 0)
--policy CHOICE           accept_all | reward_sort | value_cost_greedy | knapsack_dp
                          (default value_cost_greedy)
--cost-basis CHOICE       expected | upper (default expected: plan on the expected
                          cost and rely on the ledger for hard safety)
--planner-reserve FLOAT   fraction of total, or absolute above 1
                          (default 5%, 10% when low-confidence dominates)
--max-workers INT         parallel sessions, capped at 5 (default 4)
--time-limit FLOAT        wall clock seconds (default 3300)
--triage-threshold FLOAT  default 0.6
--max-iterations INT      coding LLM attempts (default 6)
--mutation-repair         enable deterministic coding mutation search
--test-timeout FLOAT      per test file (default 60)
--max-tokens INT          completion cap per LLM call (default 1024)
--llm-backend CHOICE      auto | stdlib | openjiuwen | gateway
                          (default auto; gateway is the v5 production path)
--model STR               default model
--escalation-model STR    escalation model
--dry-run                 plan + JSON summary, no solving
--mock-llm                start the local mock proxy
--offline                 no LLM calls at all
--verbose / -v            progress on stderr
```

`--mock-llm` and `--offline` are mutually exclusive.

## Output files

* `<task>/answer.txt` and `<task>/workspace/answer.txt` (when workspace exists)
* `<OUT>/solutions.json` — every solution/skip, rewritten and fsynced after
  each task
* `<OUT>/trace.jsonl` — one JSON line per task with
  `task_id`, `type`, `reward`, `status`, `solver`, `tokens`, `duration`,
  `error`, `notes`

## Docker

Build the image (both tags) and run the offline smoke test:

```bash
bash scripts/build.sh       # builds openjiuwen-hack:latest, tags hack:latest
bash scripts/run_local.sh   # hardened offline smoke run
```

The image is `python:3.11-slim-bookworm` with `openjiuwen==0.1.20.post1`.
Measured size: **1.38 GB** on disk (**292.2 MiB** content); cold build: **1015 s** on
this host (PyPI ~0.17 MB/s, warm rebuild 0.4 s) — well under the 4 GB image limit, but
15 s over the 1000 s cold-build limit here.

`scripts/run_local.sh` uses the same hardening flags as the final contract
(4 CPUs, 16 GB, read-only root, `/tmp` tmpfs 2 GB, `--cap-drop ALL`,
`no-new-privileges`), with `--network none` and `--user "$(id -u):$(id -g)"` so
it can write to the user-owned example directory. With `--mutation-repair` the
offline smoke solves 8/8 for score 20 with **0 tokens**.

The image uses `entrypoint.sh` as its `ENTRYPOINT`: it normalises `HOME` and
`exec "$@"`, so signals and the exit code reach the judge. The command after
the image is therefore the Python invocation itself.

The final run command shape (the network name is organizer-specific):

```bash
TASKS=/path/to/tasks

docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,size=2g \
  -e HOME=/tmp \
  --cpus 4 \
  --memory 16g \
  -e BUDGET="$BUDGET" \
  -e LLM_BASE_URL="$LLM_BASE_URL" \
  -e "LLM_API_KEY=$LLM_API_KEY" \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -v "$TASKS:/data/tasks:rw" \
  hack:latest python /app/run_agent.py --tasks-dir /data/tasks
```

* `--network hack-net` is organizer-specific; add it only if the event
  environment requires that network.
* A budget must be supplied (`BUDGET` or `OJ_TOKEN_BUDGET`); the harness does
  not invent one outside `--offline`.
* No `/app/logs` tmpfs is required: openJiuwen logs go to
  `/tmp/openjiuwen-logs` at WARNING.
* stdout is one JSON object; the `LLM backend:` line and any diagnostics go to
  stderr.

**Permissions caveat.** The image runs as root, but `--cap-drop ALL` removes
`CAP_DAC_OVERRIDE`. `/data/tasks` must therefore be writable by uid 0
(for example `0777`, or owned by root) or the run must use a matching
`--user`. Otherwise `answer.txt` cannot be created in the task directories.

## openJiuwen integration

### Model layer

`--llm-backend gateway` is the v5 production path and uses
`harness/openjiuwen_gateway.py` on the openJiuwen Core SDK. `--llm-backend
auto` (default) remains backward compatible: it uses `harness/ojiuwen_llm.py`
when openJiuwen is installed, otherwise stdlib. The final image installs:

```text
openjiuwen==0.1.20.post1
```

from `requirements-openjiuwen.txt`. No local installation is required for the
stdlib tests; the adapter imports the SDK lazily.

### DeepAgent path (next iteration)

`harness/ojiuwen_runtime.py` is an optional, import-guarded DeepAgent adapter
for openJiuwen 0.1.20.post1. It is **not wired into the default runner yet**;
the deterministic/stdlib solvers remain the production path.

```python
from harness.ojiuwen_runtime import build_runtime, build_agent, run_query

runtime = build_runtime()          # None when openjiuwen is absent
agent = build_agent(system_prompt="...", tools=[], max_rounds=20)
output, usage = await run_query(
    query="...",
    system_prompt="...",
    tools=[],
    run_id="task-1",
)
# usage: input_tokens / output_tokens / prompt_tokens / completion_tokens /
#        reasoning_tokens / total_tokens / model_calls
```

The adapter uses `create_deep_agent`, `AgentCard`, `create_agent_session`,
`TaskCompletionRail`, `MaxRoundsEvaluator` and `TimeoutEvaluator`. A custom
rail accumulates usage on `AgentCallbackEvent.AFTER_MODEL_CALL`; `run_query`
returns the final output plus that usage dict. A future runner integration can
implement `DeepAgentRuntime.solve()` and route selected task types through the
same `harness.store` and `harness.budget` abstractions.

API note: the DeepAgent path follows the signatures verified in the
0.1.20.post1 wheel. It is import-guarded and has not been exercised end to end.
Live model-layer verification is v5 (see Verification): the gateway path is
proven with real proxy calls, while the v4 table is historical.
`DeepAgentRuntime.solve()` is still not wired into the default runner.

## Extended task set

`examples/task-dir-extended/` holds **16 tasks** (816 KB) extracted from
`task-dir.rar`: the **8 official** examples plus **8 additional** tasks. The set is
purely additive — `examples/task-dir` remains the frozen 8-task contract baseline
and is never modified by extended-set work.

Measured offline run (on a copy, `--max-workers 1`):

| type | total | solved | reward solved | comment |
| --- | --- | --- | --- | --- |
| math | 4 | 4 | 17 | deterministic arithmetic path |
| data_analysis | 4 | 4 | 15 | deterministic CSV aggregation |
| search | 4 | 4 | 10 | relation chains (`search_0006` → Lisbon, `search_0007` → Granite AI) |
| coding | 4 | 2 | 4 | multi-function bugs need the LLM |

```bash
python3.11 run_agent.py --tasks-dir examples/task-dir-extended --offline --mutation-repair --max-workers 1
```

Result: **16 tasks, 14 solved, 0 skipped, 2 failed gracefully, score 46, 0 tokens**.

Measured live run (4 workers, gateway, read-only container): **16/16 solved, score 54,
3421.8 units, 89 s**. So the current state is **offline 14/16, score 46** and
**live 16/16, score 54** — the live gap is exactly the two coding tasks that need the LLM.

In the offline run only the two coding failures remain; both need the LLM path:

* `coding_0008`, `coding_0009` — multi-function bugs that require generated fixes
  rather than the local repair loop.

Triage fix: instructions that say "no internet and no LLM calls" are now read as a
**prohibition**, not as a requirement. Previously such phrasing could mark a task
infeasible; after the fix all 16 extended tasks are planned and attempted.

Search fix: markdown retrieval now follows multi-hop relation chains, so the
search subset is **4/4** offline (reward 10) instead of 2/4.

## External evaluation

Six public datasets were converted into harness fixtures under `eval/external/`
and run live through the same gateway path (4 workers,
`OJ_MAX_CONCURRENT_LLM=3`, `OJ_TOKEN_BUDGET=100000`, `--cost-basis expected`,
model `qwen3.6-35b-a3b-noreason`; the coding datasets used `--test-timeout 10`).
Command shape, per-dataset notes and the redacted container invocation are in
`eval/results/external-eval.md`; the fixtures and their counts are described in
[`eval/external/README.md`](eval/external/README.md).

| Dataset | Tasks | Solved | Failed | Score | Tokens | Elapsed |
|---|---:|---:|---:|---:|---:|---:|
| GSM8K | 15 | 15 | 0 | 45 | 349.6 | 68 s |
| WikiTableQuestions | 15 | 15 | 0 | 45 | 992.0 | 6 s |
| HotpotQA | 10 | 10 | 0 | 30 | 804.1 | 64 s |
| MBPP | 15 | 15 | 0 | 30 | 4611.0 | 25 s |
| QuixBugs | 10 | 10 | 0 | 20 | 6922.4 | 52 s |
| lorne | 10 | 10 | 0 | 18 | 1310.5 | 68 s |
| **Total** | **75** | **75** | **0** | **188** | **14989.6** | ~5 min |

The generic solver solved **40/40** math, table and multi-hop tasks (GSM8K,
WikiTableQuestions, HotpotQA); the coding loop solved **35/35** on MBPP,
QuixBugs and lorne — **75/75 tasks in total**. The QuixBugs row is a rerun
(3 workers) after the fenced-code fallback fixed `ext_quixbugs_8`, which had
previously failed with "LLM did not return a usable full-file patch".

**Caveat:** these are public datasets and are likely contaminated by model
training. The numbers are robustness evidence that the generic and coding paths
work on third-party task shapes, not a guarantee on the official 100 tasks.
Raw summaries: `eval/results/external-{gsm8k,wikitable,hotpotqa,mbpp,quixbugs,lorne}.json`.

## Tests

The full suite is **250 stdlib unittest tests**, of which **2 are skipped** on
the host because they require a real `openjiuwen` import. **32 contract tests**
live under `tests/contract/`.

```bash
~/.local/bin/python3.11 -m unittest discover -s tests -v
~/.local/bin/python3.11 -m unittest discover -s tests/contract -v
```

Contract coverage includes:

* task path/loading and answer-path handling;
* budget formula, no per-task cap, global budget only, exhaustion, zero budget;
* estimator fallback order, confidence bands, calibration and zero-cost tasks;
* policy selection, planning reserve and deterministic-first ordering;
* parallel workers and thread safety;
* `time_limit == 0` (no task attempted) and positive deadlines;
* end-to-end example runs.

The host unit tests for the openJiuwen clients use injected fake models and
import guards. The v5 live gateway smoke was also run for real against the
organizer proxy: all four aliases answered, and the container runs are recorded
in Verification and `eval/results/live-v5.md`.

## Verification

Local commands:

```bash
~/.local/bin/python3.11 -m compileall -q .
~/.local/bin/python3.11 -m unittest discover -s tests -v
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --dry-run --offline
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --offline --llm-backend stdlib --max-workers 1
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --offline --mutation-repair --max-workers 1
~/.local/bin/python3.11 -m unittest discover -s tests/contract -v
# scripts/sdk_smoke.py  # live: needs LLM_BASE_URL / LLM_API_KEY in env; all 4 aliases OK on 2026-11-10
```

Verified v5 results:

| Run | Result | Tokens spent | Notes |
|---|---|---|---|
| Offline examples | 8 tasks, 0 infeasible, 6 solved, 2 coding failed gracefully | 0 | score 16 |
| Offline + `--mutation-repair` | 8/8 solved | 0 | score 20 |
| Extended offline set | 16 tasks, 14 solved, 2 failed gracefully | 0 | score 46 |

v5 live runs (gateway backend, read-only container, budget from
`OJ_TOKEN_BUDGET=100000`). The current rows use 4 workers,
`OJ_MAX_CONCURRENT_LLM=3` and `--cost-basis expected`; the earlier 1-worker runs are
kept in the table below as history. Details and the redacted command are in
`eval/results/live-v5.md`; the raw summaries are `eval/results/live-8.json` and
`eval/results/live-16.json` (historical 1-worker runs).

| Live run | Result | Tokens spent | Notes |
|---|---|---|---|
| **100-task stress** (replicated extended set, 4 workers) | **100/100 solved**, 0 failed, 0 skipped, 0 errors | **19537.5** (prompt 51355, completion 14402) | **score 336**, 100 s; **not the official 100 tasks** — the 16-task extended set replicated with unique ids; `reconsidered=3`, `final_accepted=100`, `replans=96`, `budget.reserved=0.0`, budget remaining 80462.5 |
| Extended 16 tasks (4 workers) | **16/16 solved**, 0 failed, 0 skipped, 0 errors | **3421.8** (prompt 8378, completion 2584) | **score 54**, 89 s; `OJ_MAX_CONCURRENT_LLM=3`, `--cost-basis expected`, `budget.reserved=0.0`; one `429` retried successfully via `Retry-After` |
| Extended 16 tasks, `--model qwen3.8-27b` (4 workers) | 15/16 solved, 1 failed, 0 skipped, 0 errors | **9251.7** (prompt 15747, completion 7677) | score 50, 119 s; `coding_0008` failed ("LLM did not return a usable full-file patch"); same 16 tasks, 4 workers, gateway, `budget=100000` |
| Official 8 tasks (4 workers) | **8/8 solved**, 0 failed, 0 skipped, 0 errors | **454.3** (prompt 1433, completion 311) | **score 20**, 38 s; no `429` |
| Official 8 tasks (history, 1 worker) | 8 solved, 0 failed, 0 skipped, 0 errors | 461.3 (prompt 1433, completion 318) | score 20; `policy=value_cost_greedy`, `budget_source=env:OJ_TOKEN_BUDGET`, `llm_backend=openjiuwen-gateway` |
| Extended 16 tasks (history, 1 worker) | 14 solved, 2 failed, 0 skipped, 0 errors | 2350.8 (prompt 7028, completion 1648) | score 47; pre-search-fix run (search 2/4 at that time); coding 4/4 via LLM, data_analysis 4/4, math 4/4 |

**100-task stress (replicated set, not the official 100 tasks).** The 16-task extended set
was copied and replicated with unique `task_id`s until 100 tasks existed, then run live with
4 workers, `OJ_MAX_CONCURRENT_LLM=3` and `budget=100000`. Result: **100/100 solved, score
336, 19537.5 units (prompt 51355, completion 14402), 100 s, `reconsidered=3`,
`final_accepted=100`, `replans=96`, 0 failed / 0 skipped / 0 errors**. Per type: coding
28/28, data_analysis 24/24, math 24/24, search 24/24. Before the replan fix the same set
gave 96/100 (4 `reject:cost`), score 328, 15718.2 units. The initial estimator expected
coding cost ~3.9k versus an actual ~500–600, so the replan now re-estimates all unsubmitted
tasks and can re-accept tasks it rejected earlier. This is a stress test of planning and
budget behaviour, not a claim about the official task set.

The gateway path is **proven with real proxy calls**: `scripts/sdk_smoke.py` answered
for all four allowed aliases (`qwen3.8-27b` 2468.3 ms in56/out27/reasoning23;
`qwen3.8-27b-noreason` 287.7 ms in32/out2; `qwen3.6-35b-a3b` 1194.2 ms in30/out64;
`qwen3.6-35b-a3b-noreason` 235.3 ms in32/out2), and the official task set solved 8/8
through the same backend. Credentials were supplied only through
`LLM_BASE_URL` / `LLM_API_KEY` at run time and are **not committed**; the runs used the
organizer proxy, so they are reproducible only with organizer credentials.

The default model stays `qwen3.6-35b-a3b-noreason`: on the same 16 extended tasks
`--model qwen3.8-27b` scored lower (50 vs 54), cost more than twice the tokens (9251.7 vs
3421.8) and took longer (119 s vs 89 s), and it failed `coding_0008` on the full-file patch
format — so the stronger model is not worth the cost or latency.

The v4 live-run table below is **historical**. Those runs used the v4
stdlib/openjiuwen path, not the v5 gateway, and were **not re-verified in v5**.
It is kept for reference only.

| v4 historical run | Result | Tokens spent | Notes |
|---|---|---|---|
| Live NeuralDeep via stdlib client | 8/8 solved | 1100.5 (prompt 2175, completion 883) | score 20, ~6 s |
| Live container, explicit `--llm-backend openjiuwen --max-workers 1` | 8/8 solved | 454.3 (prompt 1433, completion 311) | score 20, ~5.3 s; stdout single JSON; stderr only `LLM backend: openjiuwen` |
| Live container, exact official invocation (`--tasks-dir` only; default auto, max-workers=4) | 8/8 solved | 1162.7 (prompt 2857, completion 877) | score 20, 16.9 s; `llm_backend=openjiuwen`, requested `auto`; 4 proxy calls; `coding_0003` needed 3 repair iterations; concurrency safe, no event-loop error |

Measured v5 live result: **8/8 official tasks for score 20** (454.3 units, 38 s) and
**16/16 extended tasks for score 54** (3421.8 units, 89 s), with 0 errors, both at
`--max-workers 4`. The live gateway also retries `429`/`5xx` with `Retry-After` behind an
LLM semaphore of 3 (`OJ_MAX_CONCURRENT_LLM=3`), so one `429` during the 16-task run was
retried without losing the task, and the planner now uses `--cost-basis expected` by
default. Credentials are not committed; the runs used the organizer proxy with
`LLM_BASE_URL` / `LLM_API_KEY` from the environment.

The exact official invocation is just:

```bash
python /app/run_agent.py --tasks-dir /data/tasks
```

with `BUDGET`, `LLM_BASE_URL` and `LLM_API_KEY` supplied by the environment.

## Notes and limitations

* Deterministic solvers intentionally return `None` rather than guess. A
  future generic LLM fallback can be plugged into the runner.
* The coding mutation search is off by default to keep the offline example
  run honest (no LLM, tests stay failing). Enable `--mutation-repair` to solve
  the two bundled coding examples deterministically.
* `solutions.json` is rewritten on every task. This is deliberate for
  durability; with 100 tasks the file remains small.
* The mock proxy is for tests only and makes no network calls.
* The v4 live openJiuwen/NeuralDeep runs in the historical table are kept for
  reference and were not re-verified in v5. Host unit tests use injected fakes
  and import guards. v5 live runs used the real gateway against the organizer
  proxy; credentials came from the environment and are not committed.
* The DeepAgent path is implemented but deliberately not wired into the
  default runner; `DeepAgentRuntime.solve()` raises `NotImplementedError`.
* The openJiuwen logging configuration uses the `configure_log_config` API and
  keys specific to `openjiuwen==0.1.20.post1`; re-check it when upgrading.
* The `expected` field is never read by production code; only tests read it as
  ground truth.
* Image size is **1.38 GB** on disk (**292.2 MiB** content) and a cold build takes
  **1015 s** on this host (PyPI throughput ~0.17 MB/s; warm rebuild 0.4 s). That is
  well under the 4 GB image limit but 15 s over the 1000 s cold-build limit on this
  host, so the cold-build limit remains a residual risk.
