# openJiuwen Challenge 2026 — budget-aware harness MVP

A Python 3.11 harness with a stdlib-only core and an optional openJiuwen
model layer. The final image installs `openjiuwen==0.1.20.post1`
(`requirements-openjiuwen.txt`). The official production path is
`--llm-backend gateway` (the default after the compliance fix): production LLM
calls go through `OpenJiuwenGateway`. `auto` remains compatible and uses
openJiuwen when present, otherwise the stdlib urllib client, but it is **not**
the production path.

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
tests/                           stdlib unittest suite (392 tests at baseline 60febbd; the
                                 compliance fix adds tests — see the final verification output)
tests/contract/                  47 contract tests at baseline 60febbd (same caveat)
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
* Infeasible tasks, failed coding tasks, and budget/time-skipped tasks get an
  **empty** `answer.txt` (R3 placeholder), never a made-up answer. An empty file
  is not counted as solved. They do not get "no answer.txt".
* The `expected` field in `task.json` is **never read by production code**;
  only the test-suite reads it as ground truth.

### Budget

There is exactly **one global budget** and **no per-task cap**.

The **official** budget variable is **`BUDGET`**. The organizers confirmed that
the budget is passed in `BUDGET` and that it may differ from 100k, so the
official run command must pass `-e BUDGET` (see Docker below). Do not hard-code
100k as the official budget.

`BudgetProvider` resolves the total once at startup with the priority
`--budget` > `OJ_TOKEN_BUDGET` > `BUDGET`. `OJ_TOKEN_BUDGET` remains supported
for local runs and older commands, but it is **not** the official variable.
Without a value and without `--offline`, the CLI exits with a configuration
error before any LLM call. With `--offline` only, `100000` is a **local test
assumption** (`budget_source=offline-default`) and a warning is printed.

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
4. At most `--max-iterations` (default 3) LLM attempts.
5. Return `DONE` only when all tests pass. On any failure the placeholder stays
   an empty `answer.txt` (R3), so the task is not counted as solved.

Test files are never patched; if they change on disk they are restored from
the snapshot before the solver gives up.

### LLM backends

The `gateway` backend is the v5 production path and the **default**
(`--llm-backend gateway`). `stdlib`, `openjiuwen` and `auto` remain compatible
transports; all implement the same
`chat(messages, *, model, max_tokens, temperature, attempt) -> LLMResponse`
contract.

**gateway** (`harness/openjiuwen_gateway.py`, `OpenJiuwenGateway`)

* v5 production path: every production LLM call goes through
  `OpenJiuwenGateway`, built on the `openjiuwen` Core SDK.
* The allowlist contains four aliases: `qwen3.8-27b`, `qwen3.8-27b-noreason`,
  `qwen3.6-35b-a3b`, `qwen3.6-35b-a3b-noreason`. The official message of
  28.10 names only `qwen3.8-27b` and `qwen3.6-35b-a3b`; whether the
  `-noreason` aliases are allowed is **confirm with organizers** (the proxy
  accepts them, but "accepted" is not "allowed"). The official default model is
  the base alias `qwen3.6-35b-a3b`.
* No silent fallback. An unavailable model, gateway error, or SDK import
  failure raises `LLMError`; the gateway never switches model or endpoint on
  its own.
* Selected with `--llm-backend gateway` (default). `auto` remains compatible
  and keeps the existing resolution order, but is not production.
* Live smoke and live runs are **historical**: `scripts/sdk_smoke.py` reached
  the organizer proxy for all four aliases on 2026-11-10, and pre-R3 container
  runs solved 8/8 official tasks. The 16/16 extended numbers are historical
  and were **not re-verified after R3** (see Verification and `## NOT VERIFIED`).
  Credentials were supplied via the environment at run time only; nothing is
  committed.

**stdlib** (`harness/llm.py`, `LLMClient`)

* `urllib.request` only; no SDK dependency.
* OpenAI-compatible `POST /v1/chat/completions`.
* Retries `408/409/425/429/5xx` and network errors with exponential backoff
  and optional `Retry-After`; client errors are not retried.
* Default model `qwen3.6-35b-a3b` (base alias; the pre-fix default was
  `qwen3.6-35b-a3b-noreason`), escalation model `qwen3.8-27b` after a
  configurable number of attempts. An explicit `LLM_MODEL` / `--model` wins.
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

`build_llm_client(config, backend="gateway")` selects the transport:

* `gateway` — `OpenJiuwenGateway` through `GatewayChatAdapter`; no fallback;
* `stdlib` — always `LLMClient`;
* `openjiuwen` — `OpenJiuwenLLMClient`, or `LLMError` when the package is not
  installed;
* `auto` — openJiuwen when `importlib.util.find_spec("openjiuwen")` succeeds,
  otherwise stdlib (backward-compatible, **not** the production path).

The chosen backend is printed on stderr and echoed in the final JSON summary as
`llm_backend`.

Example openJiuwen configuration:

```bash
export LLM_BASE_URL="$LLM_PROXY_URL"
export LLM_API_KEY   # value supplied via the environment, never committed
export OJ_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'
python run_agent.py --tasks-dir /data/tasks --llm-backend gateway
```

### Model selection

`--model-policy` decides which model the run asks for:

* `fixed` (**default**, official run) — always use `--model` (or
  `$LLM_MODEL`, else the built-in default `qwen3.6-35b-a3b`, the base alias),
  ignoring the stats file. An explicit `LLM_MODEL` / `--model` has priority.
* `metrics` — internal experiments only. `harness/model_router.py` reads
  `--model-stats` (default `harness/model_stats.json`), keeps only allowlisted
  models with measured rows (`tasks > 0`), and picks the maximum
  `score / cost` (ties: more solved, then lower cost). It is **not** used for
  the official run and must not be described as the production default.

Candidates are the four aliases in the gateway allowlist: `qwen3.8-27b`,
`qwen3.8-27b-noreason`, `qwen3.6-35b-a3b`, `qwen3.6-35b-a3b-noreason`. The
organizer message of 28.10 names only the two base aliases; the `-noreason`
variants are **confirm with organizers** and are not the official default.

The historical metrics winner was `qwen3.6-35b-a3b-noreason`
(54 / 3421.8 = 0.0158) on the pre-R3 tree. That measurement is **historical**;
it is not a reason to override the judge's fixed model. An explicit `--model`
is the fallback: it is used as-is under `fixed`, and when the stats file is
missing, malformed or empty the metrics policy returns that model with a
human-readable reason instead of inventing one. There is still no silent
substitution on a gateway error — the selection is made once, before the run,
and logged.

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

### Predictions and final evaluation

`<OUT>/predictions.jsonl` is diagnostic only: one fsynced JSON line per task,
written once from the initial estimates **before** planning, so replans and
per-iteration re-estimation never add duplicate rows. Fields: `task_id`,
`task_type`, `solver`, `model`, `expected_cost`, `upper_cost`, `confidence`,
`drivers`, `theory`, `ts`.

`trace.jsonl` carries the audit join: `y_hat` (predicted cost), `y_actual`
(actual cost), `theory` (the estimator fallback branch behind the prediction),
`estimator_confidence` and `model`. That makes every forecast checkable
against what the run actually spent.

`eval/run_eval.py` joins the two files by `task_id`, splits by task id, and
reports the primary outcome first:

```bash
python3.11 eval/run_eval.py --run <OUT> --holdout 0.3 --out eval/results/final.md
```

Live 16-task evidence is limited to the raw summary in
`eval/results/live-16.json`: **14/16 solved, score 47, 2350.8 tokens** (historical
1-worker run). There is **no raw 4-worker R3 summary** in the repository; the
earlier 16/16 / score 54 rows (3421.8 units) are historical pre-R3 runs and are
**not re-verified**. The current live R3 run is **NOT VERIFIED** — do not quote
16/16 as the current result.

Final evaluation (`eval/results/final.md`, holdout 0.3, split by `task_id`) —
computed on the historical pre-R3 trace, not on a current R3 run:

| Block | Metric | Value |
|---|---|---:|
| Primary (test split) | tasks | 7 |
| | score | 24 |
| | solved | 7/7 |
| | budget_used | 829.8 |
| | overruns | 0 |
| Forecast | n | 2 |
| | MAE | 3330.10 |
| | MdAPE | 1118.72% |
| | WAPE | 802.63% |
| | bias | 12.187 |
| | zero-cost excluded | 5 |
| Interval | PICP | 71.4% |
| | MPIW | 2643.69 |

Honest reading: the primary outcome is **perfect on the held-out test split**,
but the forecast diagnostics are poor — the estimator overestimates heavily
(bias 12.2), MdAPE and WAPE are three-digit, and PICP 71.4% misses the nominal
interval target. Forecast n is only 2 because 5 of 7 test tasks were
deterministic/zero-cost and are excluded. MAPE therefore remains a
**diagnostic** metric, not an optimisation target, and the estimator still
needs live recalibration before its predictions are trusted for tight
planning.

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
--max-iterations INT      coding LLM attempts (default 3)
--mutation-repair         enable deterministic coding mutation search
--test-timeout FLOAT      per test file (default 60)
--max-tokens INT          completion cap per LLM call (default 1024)
--llm-backend CHOICE      auto | stdlib | openjiuwen | gateway
                          (default gateway: the production path through
                          openJiuwen Core; auto is compatible, not production)
--model STR               default/fallback model (default: $LLM_MODEL or
                          qwen3.6-35b-a3b)
--model-policy CHOICE     fixed | metrics (default fixed: always --model;
                          metrics: internal experiments only, best measured
                          score/cost from --model-stats)
--model-stats PATH        measured stats JSON (default harness/model_stats.json)
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
  `error`, `notes`, and the forecast audit fields `y_hat`, `y_actual`,
  `theory`, `estimator_confidence`, `model`
* `<OUT>/predictions.jsonl` — one diagnostic line per task with the initial
  estimate: `task_id`, `task_type`, `solver`, `model`, `expected_cost`,
  `upper_cost`, `confidence`, `drivers`, `theory`, `ts`

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
`no-new-privileges`) with `--network none`. It copies `examples/task-dir` to a
temporary directory and runs `chmod -R a+rwX` on that copy so uid 0 (without
`CAP_DAC_OVERRIDE`) can write `answer.txt`; there is **no `--user` flag** in the
script. With `--mutation-repair` the offline smoke solves 8/8 for score 20 with
**0 tokens**.

Because of that `chmod`, this smoke run does **not** prove anything about the
judge's real mount. A **separate natural-permission test** runs the same
container against a copy left with host permissions (foreign uid, `0755`) and is
**expected to FAIL**: the harness cannot create `answer.txt` and reports every
task as failed. That failure is the current honest status of the real-mount
permission risk, not a regression in the smoke test.

The organizers allow **8 logical cores**. Local tests use `--cpus 4`
conservatively; this is a deliberate margin, not an unconfirmed limit. GPU and
local neural models are forbidden; CPU encoders/tokenizers are allowed. Network
access is only to the LLM API; runtime downloads are forbidden.

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
* The official budget variable is `BUDGET` and it may differ from 100k; the
  command above passes `-e BUDGET`. `OJ_TOKEN_BUDGET` is still accepted by the
  harness but is not the official variable. A budget must be supplied outside
  `--offline`; the harness does not invent one.
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

`--llm-backend gateway` is the v5 production path and the default. It uses
`harness/openjiuwen_gateway.py` on the openJiuwen Core SDK. `--llm-backend
auto` remains backward compatible but is not production: it uses
`harness/ojiuwen_llm.py` when openJiuwen is installed, otherwise stdlib. The
final image installs:

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
Live model-layer verification is historical v5 (see Verification): the gateway
path was proven with real proxy calls before R3, and the v4 table is older
history. `DeepAgentRuntime.solve()` is still not wired into the default runner.

**Compliance risk — confirm with organizers.** On 11.11 at 19:25 the team asked
whether using openJiuwen only as a model client/library is enough, or whether
the agent's main loop must be built on openJiuwen agents/workflows. There is no
answer yet. Our runtime calls the Core SDK `Model` through `OpenJiuwenGateway`,
but the main loop, planner and solvers are our own. The official harness report
requires `JIUWENSWARM_*` env or `jiuwenswarm`/`openjiuwen` in `process_tree`;
registration already passed, but the final `run_agent.py` is started directly.
This is an **unresolved compliance risk**, not a proven violation. Do not fake
env values or the process tree; get written confirmation from the organizers.

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

The only raw live summary in the repository is `eval/results/live-16.json`:
**14/16 solved, score 47, 2350.8 units** (historical 1-worker run, before the
search fix). A later 4-worker run reported **16/16, score 54, 3421.8 units,
89 s**; that row is **historical pre-R3** and the current R3 live run is
**NOT VERIFIED** (no raw summary). Current verified state is therefore
**offline 14/16, score 46**; live 16/16 is historical evidence, not a current
result. The remaining live gap is exactly the two coding tasks that need the
LLM.

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

The suite at baseline `60febbd` was **392 stdlib unittest tests** (2 skipped on
the host because they require a real `openjiuwen` import) plus **47 contract
tests** under `tests/contract/`. The compliance fix adds tests, so the final
count may be 393+48 or different: the authoritative number is the final
verification output, not this baseline.

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
import guards. The gateway smoke was run for real against the organizer proxy
before R3: all four aliases answered on 2026-11-10. Those live runs are
historical; the current R3 live run is **NOT VERIFIED**.

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

Measured offline results:

| Run | Result | Tokens spent | Notes |
|---|---|---|---|
| Offline examples | 8 tasks, 0 infeasible, 6 solved, 2 coding failed gracefully | 0 | score 16 |
| Offline + `--mutation-repair` | 8/8 solved | 0 | score 20 |
| Extended offline set + `--mutation-repair` | 16 tasks, 14 solved, 2 failed gracefully | 0 | score 46 |

v5 live runs (gateway backend, read-only container). **All rows below are
historical and predate R3**; they were not re-verified on the current tree. The
raw summaries are `eval/results/live-8.json` and `eval/results/live-16.json`
(1-worker runs). Details and the redacted command are in
`eval/results/live-v5.md`.

| Live run | Result | Tokens spent | Notes |
|---|---|---|---|
| **100-task stress** (replicated extended set, 4 workers) | **100/100 solved**, 0 failed, 0 skipped, 0 errors | **19537.5** (prompt 51355, completion 14402) | **score 336**, 100 s; **not the official 100 tasks** — the 16-task extended set replicated with unique ids; `reconsidered=3`, `final_accepted=100`, `replans=96`, `budget.reserved=0.0`, budget remaining 80462.5; historical |
| Extended 16 tasks (4 workers, historical pre-R3) | 16/16 solved, 0 failed, 0 skipped, 0 errors | 3421.8 (prompt 8378, completion 2584) | score 54, 89 s; not re-verified on R3; no raw summary in the repo |
| Extended 16 tasks, `--model qwen3.8-27b` (4 workers, historical) | 15/16 solved, 1 failed, 0 skipped, 0 errors | 9251.7 (prompt 15747, completion 7677) | score 50, 119 s; `coding_0008` failed ("LLM did not return a usable full-file patch"); historical |
| Official 8 tasks (4 workers, historical) | 8/8 solved, 0 failed, 0 skipped, 0 errors | 454.3 (prompt 1433, completion 311) | score 20, 38 s; historical pre-R3 |
| Official 8 tasks (history, 1 worker) | 8 solved, 0 failed, 0 skipped, 0 errors | 461.3 (prompt 1433, completion 318) | score 20; `policy=value_cost_greedy`, `budget_source=env:OJ_TOKEN_BUDGET`, `llm_backend=openjiuwen-gateway` |
| Extended 16 tasks (history, 1 worker) | **14 solved**, 2 failed, 0 skipped, 0 errors | **2350.8** (prompt 7028, completion 1648) | score 47; `eval/results/live-16.json`; pre-search-fix run (search 2/4 at that time) |

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

The gateway path was **proven with real proxy calls before R3**: `scripts/sdk_smoke.py`
answered for all four aliases (`qwen3.8-27b` 2468.3 ms in56/out27/reasoning23;
`qwen3.8-27b-noreason` 287.7 ms in32/out2; `qwen3.6-35b-a3b` 1194.2 ms in30/out64;
`qwen3.6-35b-a3b-noreason` 235.3 ms in32/out2), and the official task set solved
8/8 through the same backend. That is historical evidence, not a current R3
result. Credentials were supplied only through `LLM_BASE_URL` / `LLM_API_KEY`
at run time and are **not committed**; the runs used the organizer proxy, so
they are reproducible only with organizer credentials.

The official default model is the base alias **`qwen3.6-35b-a3b`** with
`--model-policy fixed`; an explicit `LLM_MODEL` / `--model` wins. The old
`qwen3.6-35b-a3b-noreason` metrics winner is history. The earlier comparison on
16 extended tasks (`--model qwen3.8-27b` scored 50 vs 54 and cost 9251.7 vs
3421.8) is historical pre-R3 data; it is not used to override the judge's fixed
model.

The v4 live-run table below is **historical**. Those runs used the v4
stdlib/openjiuwen path, not the v5 gateway, and were **not re-verified in v5**.
It is kept for reference only.

| v4 historical run | Result | Tokens spent | Notes |
|---|---|---|---|
| Live NeuralDeep via stdlib client | 8/8 solved | 1100.5 (prompt 2175, completion 883) | score 20, ~6 s |
| Live container, explicit `--llm-backend openjiuwen --max-workers 1` | 8/8 solved | 454.3 (prompt 1433, completion 311) | score 20, ~5.3 s; stdout single JSON; stderr only `LLM backend: openjiuwen` |
| Live container, exact official invocation (`--tasks-dir` only; default auto, max-workers=4) | 8/8 solved | 1162.7 (prompt 2857, completion 877) | score 20, 16.9 s; `llm_backend=openjiuwen`, requested `auto`; 4 proxy calls; `coding_0003` needed 3 repair iterations; concurrency safe, no event-loop error |

Historical v5 live result: **8/8 official tasks for score 20** (454.3 units, 38 s)
and **16/16 extended tasks for score 54** (3421.8 units, 89 s), with 0 errors,
both at `--max-workers 4`, measured **before R3**. The current R3 live run is
**NOT VERIFIED**; the only raw 16-task summary is 14/16, 2350.8. Credentials are
not committed; the runs used the organizer proxy with `LLM_BASE_URL` /
`LLM_API_KEY` from the environment.

The exact official invocation is just:

```bash
python /app/run_agent.py --tasks-dir /data/tasks
```

with `BUDGET`, `LLM_BASE_URL` and `LLM_API_KEY` supplied by the environment.
The backend and model policy are the defaults (`gateway`, `fixed`,
`qwen3.6-35b-a3b`); `LLM_MODEL`/`--model` overrides the default model.

## Submission

Official deadline: **13.11.2026 23:59 MSK**. Send the repository link to
**openjiuwen_org@nsu.ru** — as a reply to the key email or as a separate email.
The repository may be **public or private**. The internal freeze at 12.11 18:00
MSK is an internal milestone, not the official deadline.

Checklist before sending:

- [ ] `git rev-parse HEAD` recorded; the submitted commit is the one you tested.
- [ ] Repository URL is final and accessible to the organizers (public, or
      private with the organizers' access).
- [ ] Tests: baseline `60febbd` = 392 unit + 47 contract; **run the final suite
      after the fix and paste the actual counts** (may be 393+48 or other).
- [ ] Offline example run with `--mutation-repair`: 8/8, score 20, 0 tokens.
- [ ] Extended offline set with `--mutation-repair`: 14/16, score 46 (known
      coding failures are documented).
- [ ] Docker image builds and is under 4 GB; the official command passes
      `-e BUDGET`.
- [ ] No secrets in the tree or history; no `git push` without the owner's
      approval.
- [ ] The email is sent **before 13.11.2026 23:59 MSK**.

Email template (replace `<FINAL_COMMIT>` **after** the final push):

```text
Кому: openjiuwen_org@nsu.ru
Тема: Решение команды «Стивенсон» — openJiuwen Challenge 2026

Здравствуйте!

Направляем решение команды «Стивенсон» для openJiuwen Challenge 2026.

Команда: Стивенсон
Registration ID: 5555bec9277725f88092706dc0ae47d7
Репозиторий: https://github.com/Asgarothhh/AI-Coding-Agent
Commit: <FINAL_COMMIT>

Репозиторий: публичный (доступен без дополнительных приглашений).
Точка входа: python /app/run_agent.py --tasks-dir /data/tasks
Бюджет передаётся через переменную окружения BUDGET; LLM_BASE_URL и
LLM_API_KEY — через окружение. Модель по умолчанию — qwen3.6-35b-a3b,
политика fixed.

С уважением,
команда «Стивенсон»
```

The email has **not** been sent yet. Do not claim it was sent; `<FINAL_COMMIT>`
must be replaced with the commit that is actually pushed and tested.

## NOT VERIFIED

These items are not proven on the current tree and must not be reported as PASS:

* **Live R3 16/16.** The current R3 live run is not confirmed. The only raw
  16-task summary is `eval/results/live-16.json` = 14/16, score 47, 2350.8
  tokens (historical 1-worker). Earlier 16/16 / score 54 / 3421.8 rows are
  historical pre-R3.
* **RAM.** No live measurement of peak RSS against the 16 GB limit; only the
  container flag is set.
* **Real mount permissions.** The judge's real `/data/tasks` ownership and mode
  are unknown. `run_local.sh` masks this with `chmod -R a+rwX`; the separate
  natural-permission test is expected to FAIL on a foreign uid `0755` mount.
* **Two vs four models.** The organizer message of 28.10 names only
  `qwen3.8-27b` and `qwen3.6-35b-a3b`; the local spec lists four aliases. Whether
  the `-noreason` aliases are allowed is **confirm with organizers**. This is
  not a proven violation.
* **Exact harness report for the final submission.** The official harness
  report expects `JIUWENSWARM_*` env or `jiuwenswarm`/`openjiuwen` in
  `process_tree`. Registration passed, but `run_agent.py` is started directly.
  Whether using openJiuwen only as a model client/library is enough (vs building
  the main loop on openJiuwen agents/workflows) was asked on 11.11 at 19:25 and
  has **no answer**; **confirm with organizers**. Do not fake env or process
  tree.
* **Post-fix defaults.** This README documents the official post-fix defaults
  (`gateway`, `fixed`, base `qwen3.6-35b-a3b`). On the baseline `60febbd` tree
  argparse still defaults to `auto`/`metrics`; until that fix lands, pass
  `--llm-backend gateway --model-policy fixed --model qwen3.6-35b-a3b`
  explicitly.
* **Final test count.** 392 unit + 47 contract is the baseline `60febbd`; the
  compliance fix adds tests, so use the final verification output.
* **Cold build time on the judge's network.** The local cold build was measured
  on this host; the judge's throughput is unknown.

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
