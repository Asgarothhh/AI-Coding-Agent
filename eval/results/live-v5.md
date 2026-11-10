# v5 live runs — gateway backend on the organizer proxy

Date: 2026-11-10
Scope: real, non-offline runs of `hack:latest` against the organizer OpenAI-compatible
proxy through the openJiuwen Core SDK gateway. Every number below was measured; the raw
JSON summaries are `eval/results/live-8.json` and `eval/results/live-16.json` (historical
1-worker runs — their numbers match the history rows below).

## Environment

| Item | Value |
|---|---|
| Image | `hack:latest` (built from this tree, openJiuwen Core SDK pinned) |
| Rootfs | read-only (`--read-only`, `--tmpfs /tmp:rw`) |
| CPU / RAM | 4 CPU / 16 GB |
| Network | `--network host` (only for the proxy; no other egress needed) |
| Task tree | bind mount, writable (`answer.txt` is written next to `task.json`) |
| Backend | `--llm-backend gateway` → `openjiuwen-gateway` |
| Workers | `--max-workers 1` (history rows) / `--max-workers 4` (current rows) |
| Budget | `OJ_TOKEN_BUDGET=100000` (`budget_source=env:OJ_TOKEN_BUDGET`) |
| Concurrency cap | `OJ_MAX_CONCURRENT_LLM=3` (current 4-worker runs) |
| Cost basis | `--cost-basis expected` (planner default in the current runs) |
| Credentials | supplied via `LLM_BASE_URL` / `LLM_API_KEY` at run time; never written to a file |

Image build for reference: cold build 1015.0 s (host PyPI ~0.17 MB/s), warm rebuild
0.4 s, image 1.38 GB on disk (292.2 MiB content), read-only offline smoke 8/8.

## Runs

| Run | Workers | Tasks | Solved | Failed | Skipped | Errors | Score | Tokens spent | Prompt | Completion | Elapsed | Replans |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Extended set (current)** | 4 | 16 | 16 | 0 | 0 | 0 | **54** | **3421.8** | 8378 | 2584 | 89 s | — |
| **Official set (current)** | 4 | 8 | 8 | 0 | 0 | 0 | **20** | **454.3** | 1433 | 311 | 38 s | — |
| Official set (history) | 1 | 8 | 8 | 0 | 0 | 0 | 20 | 461.3 | 1433 | 318 | — | 7 |
| Extended set (history) | 1 | 16 | 14 | 2 | 0 | 0 | 47 | 2350.8 | 7028 | 1648 | — | 15 |

The two current rows are 4-worker runs with `OJ_MAX_CONCURRENT_LLM=3`, `budget=100000`,
read-only rootfs and `--cost-basis expected`; `budget.reserved=0.0` at the end of both.
The 1-worker rows are kept as history and predate the optimization round below. All runs
used `policy=value_cost_greedy`, `budget_source=env:OJ_TOKEN_BUDGET`,
`budget.total=100000`, and `llm_backend=openjiuwen-gateway`. Nothing was skipped and no
run-level errors were recorded. In the historical 16-task run all LLM spend landed in
`coding` (2350.8 tokens): math, data analysis and the search retrieval path stayed
deterministic. The current 16-task run spends 3421.8 units (prompt 8378, completion
2584) and completes in 89 s. During that run **one `429` occurred** and was retried
successfully honouring `Retry-After`; no task was affected. The current 8-task official
run saw no `429`.

## 100-task stress run

This is a **replicated stress set, not the official 100 tasks**. The 16-task extended set
was copied and replicated until 100 tasks existed, each with a unique `task_id`; every
number below was measured live through the gateway with 4 workers,
`OJ_MAX_CONCURRENT_LLM=3` and `budget=100000`.

| State | Tasks | Solved | Failed | Skipped | Errors | Score | Tokens spent | Prompt | Completion | Elapsed | Reconsidered | Final accepted | Replans | Budget remaining | Initial accepted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Before replan fix | 100 | 96 | 0 | 4 (`reject:cost`) | 0 | 328 | 15718.2 | — | — | — | — | 97 | — | 84281.8 | 97/100 |
| After replan fix | 100 | **100** | **0** | **0** | **0** | **336** | **19537.5** | 51355 | 14402 | 100 s | 3 | 100 | 96 | 80462.5 | — |

After the fix: `reserved=0.0`, no duplicate entries in `solutions.json`, all 100 tasks
solved.

Per-type breakdown after the fix:

| Type | Total | Solved | Failed |
|---|---|---|---|
| coding | 28 | 28 | 0 |
| data_analysis | 24 | 24 | 0 |
| math | 24 | 24 | 0 |
| search | 24 | 24 | 0 |

**Cause and fix:** the initial estimator expected coding cost ~3.9k units versus an actual
~500–600, so the planner rejected tasks it could have afforded; the replan now re-estimates
**all unsubmitted tasks** and can re-accept initially rejected tasks after observing actual
costs (3 tasks were reconsidered and accepted).

## Model comparison (16-task extended set)

Same 16 extended tasks, 4 workers, gateway backend, `budget=100000`, only `--model` differs:

| Model | Tasks | Solved | Failed | Score | Tokens spent | Prompt | Completion | Elapsed |
|---|---|---|---|---|---|---|---|---|
| `qwen3.6-35b-a3b-noreason` (default) | 16 | **16** | 0 | **54** | **3421.8** | 8378 | 2584 | **89 s** |
| `qwen3.8-27b` | 16 | 15 | 1 | 50 | 9251.7 | 15747 | 7677 | 119 s |

Both rows are live, non-offline runs. The default row is the current 16-task run from the
Runs table above.

The single `qwen3.8-27b` failure is `coding_0008` — the model did not return a usable
full-file patch, so the task failed gracefully instead of being solved.

**Conclusion:** the default stays `qwen3.6-35b-a3b-noreason`. The stronger model is not
worth the cost or latency — it spends 2.7× the tokens (9251.7 vs 3421.8 units) and takes
34% longer (119 s vs 89 s) for a lower score (50 vs 54) — and it is less reliable on the
full-file patch format (`coding_0008` failed).

## Per-type breakdown — 16-task extended run (current, 4 workers)

| Type | Total | Solved | Failed | Reward solved | Comment |
|---|---|---|---|---|---|
| math | 4 | 4 | 0 | 17 | deterministic arithmetic path |
| data_analysis | 4 | 4 | 0 | 15 | deterministic CSV aggregation |
| search | 4 | 4 | 0 | 10 | multi-hop chains now resolve (`search_0006` → Lisbon, `search_0007` → Granite AI) |
| coding | 4 | 4 | 0 | 12 | all four via the LLM gateway |

Historical 1-worker extended run, for comparison: search was 2/4 (reward 3), score 47.
After the optimization round the search subset is 4/4 and the score is 54.

## SDK smoke (`scripts/sdk_smoke.py`, inside the image)

All four allowed model aliases answered OK through the Core SDK:

| Model | Latency, ms | Input tokens | Output tokens | Reasoning tokens |
|---|---|---|---|---|
| `qwen3.8-27b` | 2468.3 | 56 | 27 | 23 |
| `qwen3.8-27b-noreason` | 287.7 | 32 | 2 | — |
| `qwen3.6-35b-a3b` | 1194.2 | 30 | 64 | — |
| `qwen3.6-35b-a3b-noreason` | 235.3 | 32 | 2 | — |

Reasoning is reported separately by the proxy and is not double-counted: the cost
formula remains `output_tokens + input_tokens / 10`, with reasoning included in the
output side only.

## Optimization round

Four changes moved the extended set from **14/16 (score 46–47)** to **16/16 (score 54)**:

| Change | Effect | Measured delta |
|---|---|---|
| Multi-hop search (`harness/solvers/search.py`): repeated links, reverse relation order, `founded` filter | `search_0006` → Lisbon, `search_0007` → Granite AI | search 2/4 → 4/4; extended **14/16 → 16/16** |
| Generic solver for unknown task types (`harness/solvers/generic.py` + runner wiring) | Unknown types are solved live through the LLM instead of failing | live proof below |
| Planner cost basis `expected` by default (`--cost-basis expected`) | More tasks accepted under the same budget | 52 accepted vs 11 on the simulation |
| 429 handling: LLM semaphore 3 (`OJ_MAX_CONCURRENT_LLM=3`) + retries | 4 workers no longer trip the proxy rate limit | 16/16 at 4 workers, 1 × 429 retried OK |

Measured deltas, same task set, same budget:

| State | Offline | Live |
|---|---|---|
| Before the round | 14/16, score 46 | 14/16 (historical 1-worker run, score 47) |
| After the round | 14/16, score 46 (`coding_0008`/`0009` fail without an LLM) | **16/16, score 54** |

The offline extended set intentionally stays at 14/16: the two remaining failures are
coding tasks that require generated fixes, which offline mode does not do. The live run
closes that gap through the LLM.

### Generic-task proof (live)

A synthetic task with an **unknown type** was added to a scratch tree to prove the generic
solver path end to end:

| Item | Value |
|---|---|
| Task id | `logic_0001` |
| Type | `logic` (not a known solver type) |
| Instruction | "Read notes.txt ... answer with the codeword only" |
| Expected | `ORCHID` |
| Backend | `gateway` |
| Result | **solved**, `answer.txt` contains `ORCHID` |
| Cost | 19.8 units |

This is the only evidence for the generic path; it is a live, non-offline run.

## Command shape (redacted)

```bash
docker run --rm \
  --network host \
  --read-only \
  --tmpfs /tmp:rw,size=2g \
  -e HOME=/tmp \
  --cpus 4 --memory 16g \
  --cap-drop ALL --security-opt no-new-privileges \
  -e OJ_TOKEN_BUDGET=100000 \
  -e OJ_MAX_CONCURRENT_LLM=3 \
  -e LLM_BASE_URL="$LLM_BASE_URL" \
  -e "LLM_API_KEY=$LLM_API_KEY" \
  -v "$TASKS:/data/tasks:rw" \
  hack:latest \
  python /app/run_agent.py --tasks-dir /data/tasks \
    --llm-backend gateway --max-workers 4 --cost-basis expected
```

`$TASKS` points at the set under test (`examples/task-dir` for the 8 official tasks, a
copy of `examples/task-dir-extended` for the 16-task run). Credentials are passed only
through the environment; no key, token or private URL appears in this repository.

## Known failures

After the optimization round there are **no live failures**. The only remaining failures
are offline-only: `coding_0008` and `coding_0009` (multi-function bugs) need generated
fixes, so the offline extended set stays at 14/16, score 46. They fail gracefully: no
`answer.txt` is written for them, the failure is recorded per task in `solutions.json`,
and the run still exits with 0 errors.

The two historically failing search tasks are fixed: `search_0006` → Lisbon and
`search_0007` → Granite AI now resolve through multi-hop relation chains (repeated
links, reverse relation order, `founded` filter).
