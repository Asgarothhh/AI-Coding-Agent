# openJiuwen Challenge 2026 — budget-aware harness MVP

A Python 3.11 harness with a stdlib-only core and an optional openJiuwen
model layer. The final image installs `openjiuwen==0.1.20.post1`
(`requirements-openjiuwen.txt`); `--llm-backend auto` uses it when present and
falls back to the stdlib urllib client otherwise.

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
harness/
  task.py                        Task dataclass, BOM-safe load_tasks()
  budget.py                      thread-safe cost accounting + hard stop
  llm.py                         stdlib urllib client + backend factory
  ojiuwen_llm.py                 openJiuwen Model adapter (async -> sync)
  triage.py                      zero-LLM infeasibility scoring
  planner.py                     heuristic cost estimate + deterministic-first plan
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
requirements.txt                 runtime SDK pin (openjiuwen==0.1.20.post1)
requirements-openjiuwen.txt      openjiuwen==0.1.20.post1
tests/                           stdlib unittest suite
tests/contract/                  29 contract tests (paths, budget, planner, parallel, time limit, E2E)
examples/task-dir/               eight official examples
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

One budget covers all sessions. The proxy does not report remaining budget,
so every response is charged locally:

```
cost = prompt_tokens / 10 + completion_tokens
```

Reasoning tokens are reported as completion tokens and therefore count as
output. Cache accounting is ignored. Before a call the coding solver checks
that `remaining > reserve` and that the per-task cap would not be exceeded;
after the response it charges the actual usage. Overspending stops new tasks
but never crashes a paid-for response. The budget is guarded by an
`RLock`, so the parallel sessions share it safely.

Defaults: total `$BUDGET` or `100000`, per-task cap `3000`, reserve `0`.

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

`harness/planner.py` estimates cost from task type, instruction length, and
file count. `priority = reward / estimated_cost`. The plan orders:

1. planned deterministic tasks (`math`, `search`, `data_analysis`);
2. planned LLM-dependent tasks (`coding`);
3. infeasible/skipped tasks.

The planner has no LLM dependency and performs no network calls.

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

Two interchangeable clients implement the same
`chat(messages, *, model, max_tokens, temperature, attempt) -> LLMResponse`
contract.

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

* `stdlib` — always `LLMClient`;
* `openjiuwen` — `OpenJiuwenLLMClient`, or `LLMError` when the package is not
  installed;
* `auto` — openJiuwen when `importlib.util.find_spec("openjiuwen")` succeeds,
  otherwise stdlib.

The chosen backend is printed on stderr and echoed in the final JSON summary as
`llm_backend`.

Example openJiuwen configuration:

```bash
export LLM_BASE_URL="http://proxy/v1"
export LLM_API_KEY="..."
export OJ_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'
python run_agent.py --tasks-dir /data/tasks --llm-backend auto
```

### Runner

* `ThreadPoolExecutor` with at most **5** parallel sessions
  (`--max-workers`, default 4).
* Before each task: time-limit and budget check.
* Tasks already queued when the budget/time limit hits are recorded as
  skipped, without answers.
* Writes `trace.jsonl` and `solutions.json` incrementally with `fsync`.
* Final JSON summary on stdout includes solved/skipped/failed/errors, score,
  tokens spent, prompt/completion totals, budget snapshot, stop reason, and
  per-type stats.

## CLI

```
--tasks-dir PATH          required
--out PATH                directory or solutions.json path ($OUT / /tmp/openjiuwen-out)
--budget FLOAT            total budget ($BUDGET / 100000)
--reserve FLOAT           keep untouched (default 0)
--per-task-cap FLOAT      per-task cap (default 3000)
--max-workers INT         parallel sessions, capped at 5 (default 4)
--time-limit FLOAT        wall clock seconds (default 3300)
--triage-threshold FLOAT  default 0.6
--max-iterations INT      coding LLM attempts (default 6)
--mutation-repair         enable deterministic coding mutation search
--test-timeout FLOAT      per test file (default 60)
--max-tokens INT          completion cap per LLM call (default 1024)
--llm-backend CHOICE      auto | stdlib | openjiuwen (default auto)
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
Measured size: **1.39 GB**; cold build: **~183 s**; well below the 4 GB limit.

`scripts/run_local.sh` uses the same hardening flags as the final contract with
`--network none` and `--user "$(id -u):$(id -g)"` so it can write to the
user-owned example directory. Offline smoke result: 8 tasks, 6 solved, 2 coding
failed gracefully, **0 tokens**.

The final run command shape (the network name is organizer-specific):

```bash
TASKS=/path/to/tasks

docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,size=2g \
  -e HOME=/tmp \
  -e BUDGET=100000 \
  -e LLM_BASE_URL="$LLM_BASE_URL" \
  -e LLM_API_KEY="$LLM_API_KEY" \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  -v "$TASKS:/data/tasks:rw" \
  --entrypoint python \
  hack:latest /app/run_agent.py --tasks-dir /data/tasks
```

* `--network hack-net` is organizer-specific; add it only if the event
  environment requires that network.
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

`--llm-backend auto` (default) uses `harness/ojiuwen_llm.py` when openJiuwen
is installed. The final image installs:

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
0.1.20.post1 wheel. It is import-guarded and has not been exercised end to end;
the model layer and its logging were verified in the container against the live
proxy, but `DeepAgentRuntime.solve()` is still not wired into the default runner.

## Tests

The full suite is **113 stdlib unittest tests**, of which **2 are skipped** on
the host because they require a real `openjiuwen` import. **29 contract tests**
live under `tests/contract/`.

```bash
~/.local/bin/python3.11 -m unittest discover -s tests -v
~/.local/bin/python3.11 -m unittest discover -s tests/contract -v
```

Contract coverage includes:

* task path/loading and answer-path handling;
* budget formula, per-task cap, global exhaustion, zero budget;
* planner ordering with 100 tasks / 20 infeasible;
* parallel workers and thread safety;
* `time_limit == 0` (no task attempted) and positive deadlines;
* end-to-end example runs.

The host unit tests for the openJiuwen client use injected fake models; the
real SDK path is exercised in the container (see Verification).

## Verification

Local commands:

```bash
~/.local/bin/python3.11 -m compileall -q .
~/.local/bin/python3.11 -m unittest discover -s tests -v
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --dry-run --offline
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --offline --llm-backend stdlib --max-workers 1
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --offline --mutation-repair --max-workers 1
```

Verified results:

| Run | Result | Tokens spent | Notes |
|---|---|---|---|
| Offline examples | 8 tasks, 0 infeasible, 6 solved, 2 coding failed gracefully | 0 | score 16 |
| Offline + `--mutation-repair` | 8/8 solved | 0 | score 20 |
| Live NeuralDeep via stdlib client | 8/8 solved | 1100.5 (prompt 2175, completion 883) | score 20, ~6 s |
| Live container, explicit `--llm-backend openjiuwen --max-workers 1` | 8/8 solved | 454.3 (prompt 1433, completion 311) | score 20, ~5.3 s; stdout single JSON; stderr only `LLM backend: openjiuwen` |
| Live container, exact official invocation (`--tasks-dir` only; default auto, max-workers=4) | 8/8 solved | 1162.7 (prompt 2857, completion 877) | score 20, 16.9 s; `llm_backend=openjiuwen`, requested `auto`; 4 proxy calls; `coding_0003` needed 3 repair iterations; concurrency safe, no event-loop error |

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
* The live openJiuwen SDK is verified in the container against NeuralDeep,
  including concurrent calls and the exact auto / 4-worker invocation. Host
  unit tests still use injected fakes and import guards because the SDK is not
  installed on the host.
* The DeepAgent path is implemented but deliberately not wired into the
  default runner; `DeepAgentRuntime.solve()` raises `NotImplementedError`.
* The openJiuwen logging configuration uses the `configure_log_config` API and
  keys specific to `openjiuwen==0.1.20.post1`; re-check it when upgrading.
* The `expected` field is never read by production code; only tests read it as
  ground truth.
* Image size is 1.39 GB, cold build ~183 s, and the final run limit is 1 hour.
