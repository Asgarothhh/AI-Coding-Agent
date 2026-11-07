# openJiuwen Challenge 2026 — budget-aware harness MVP

A stdlib-only Python 3.11 harness skeleton for the final challenge contract:

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
  llm.py                         urllib OpenAI-compatible client, retries
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
  ojiuwen_runtime.py             optional openJiuwen/workswarm adapter
tests/                           stdlib unittest suite
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

### LLM client

* `urllib.request` only; no SDK dependency.
* OpenAI-compatible `POST /v1/chat/completions`.
* Retries `408/409/425/429/5xx` and network errors with exponential backoff
  and optional `Retry-After`; client errors are not retried.
* Default model `qwen3.6-35b-a3b-noreason`, escalation model `qwen3.8-27b`
  after a configurable number of attempts.
* Usage is parsed from the response; if missing, a deterministic character
  estimate is used and marked `estimated=True`.

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

## openJiuwen integration

`harness/ojiuwen_runtime.py` exposes:

```python
runtime = build_runtime()   # None when openjiuwen/workswarm is not installed
```

The adapter is intentionally a placeholder and never a hard dependency. The
MVP harness uses its own solvers; a future integration can implement
`AgentRuntime.solve()` and route selected task types through it.

## Verification

```bash
~/.local/bin/python3.11 -m compileall -q .
~/.local/bin/python3.11 -m unittest discover -s tests -v
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --dry-run --offline
~/.local/bin/python3.11 run_agent.py --tasks-dir examples/task-dir --offline --max-workers 1
```

Expected offline result for the examples: 8 tasks, 0 infeasible, 6 solved,
2 coding tasks failed gracefully, 0 tokens, score 16.

## Notes and limitations

* Deterministic solvers intentionally return `None` rather than guess. A
  future generic LLM fallback can be plugged into the runner.
* The coding mutation search is off by default to keep the offline example
  run honest (no LLM, tests stay failing). Enable `--mutation-repair` to solve
  the two bundled coding examples deterministically.
* `solutions.json` is rewritten on every task. This is deliberate for
  durability; with 100 tasks the file remains small.
* The mock proxy is for tests only and makes no network calls.
