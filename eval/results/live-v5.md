# v5 live runs — gateway backend on the organizer proxy

Date: 2026-11-10
Scope: real, non-offline runs of `hack:latest` against the organizer OpenAI-compatible
proxy through the openJiuwen Core SDK gateway. Every number below was measured; the raw
JSON summaries are `eval/results/live-8.json` and `eval/results/live-16.json`.

## Environment

| Item | Value |
|---|---|
| Image | `hack:latest` (built from this tree, openJiuwen Core SDK pinned) |
| Rootfs | read-only (`--read-only`, `--tmpfs /tmp:rw`) |
| CPU / RAM | 4 CPU / 16 GB |
| Network | `--network host` (only for the proxy; no other egress needed) |
| Task tree | bind mount, writable (`answer.txt` is written next to `task.json`) |
| Backend | `--llm-backend gateway` → `openjiuwen-gateway` |
| Workers | `--max-workers 1` |
| Budget | `OJ_TOKEN_BUDGET=100000` (`budget_source=env:OJ_TOKEN_BUDGET`) |
| Credentials | supplied via `LLM_BASE_URL` / `LLM_API_KEY` at run time; never written to a file |

Image build for reference: cold build 1015.0 s (host PyPI ~0.17 MB/s), warm rebuild
0.4 s, image 1.38 GB on disk (292.2 MiB content), read-only offline smoke 8/8.

## Runs

| Run | Tasks | Solved | Failed | Skipped | Errors | Score | Tokens spent | Prompt | Completion | Replans |
|---|---|---|---|---|---|---|---|---|---|---|
| Official set | 8 | 8 | 0 | 0 | 0 | 20 | 461.3 | 1433 | 318 | 7 |
| Extended set | 16 | 14 | 2 | 0 | 0 | 47 | 2350.8 | 7028 | 1648 | 15 |

Both runs used `policy=value_cost_greedy`, `budget_source=env:OJ_TOKEN_BUDGET`,
`budget.total=100000`, and `llm_backend=openjiuwen-gateway`. Nothing was skipped and no
run-level errors were recorded. In the 16-task run all LLM spend landed in `coding`
(2350.8 tokens): math, data analysis and the search retrieval path stayed deterministic.

## Per-type breakdown — 16-task extended run

| Type | Total | Solved | Failed | Reward solved | Comment |
|---|---|---|---|---|---|
| math | 4 | 4 | 0 | 17 | deterministic arithmetic path |
| data_analysis | 4 | 4 | 0 | 15 | deterministic CSV aggregation |
| search | 4 | 2 | 2 | 3 | multi-hop chains fail |
| coding | 4 | 4 | 0 | 12 | all four via the LLM gateway |

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
  -e LLM_BASE_URL="$LLM_BASE_URL" \
  -e "LLM_API_KEY=$LLM_API_KEY" \
  -v "$TASKS:/data/tasks:rw" \
  hack:latest \
  python /app/run_agent.py --tasks-dir /data/tasks \
    --llm-backend gateway --max-workers 1
```

`$TASKS` points at the set under test (`examples/task-dir` for the 8 official tasks, a
copy of `examples/task-dir-extended` for the 16-task run). Credentials are passed only
through the environment; no key, token or private URL appears in this repository.

## Known failures

`search_0006` and `search_0007` remain the only failures, in both the offline and the
live extended run. They are multi-hop relation chains that the retrievers cannot
complete (for example `field 'founded' missing`). They fail gracefully: no `answer.txt`
is written for them, the failure is recorded per task in `solutions.json`, and the run
still exits with 0 errors.
