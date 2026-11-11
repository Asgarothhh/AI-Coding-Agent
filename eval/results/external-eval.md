# External evaluation — six public datasets, live gateway

Date: 2026-11-11
Scope: real, non-offline runs of the packaged agent against **six public datasets**
converted by `eval/import_datasets.py` and `eval/import_lorne_tasks.py`. The task
fixtures live under `eval/external/`; the raw run summaries are the six
`eval/results/external-*.json` files.

This is a **robustness check of the generic path and the coding loop**, not a
leaderboard result and not a claim about the official 100 tasks. See the
contamination caveat at the end.

## Purpose

The official sample only contains the organizers' four task families. This run
answers two narrower questions:

1. Does the **generic LLM solver** handle task types it was never specialised
   for — grade-school math, table lookup, multi-hop context QA?
2. Does the **coding solver** (deterministic tests + LLM repair loop) work on
   third-party code bases — function-implementation (MBPP),
   bug-fixing (QuixBugs) and small developer tasks (lorne)?

## Environment and command shape

Every run used the same container, gateway backend and budget; only the task
directory changed. Credentials came from the environment and are redacted here.

| Item | Value |
|---|---|
| Image | `hack:latest` built from this tree (openJiuwen Core SDK pinned) |
| Rootfs | read-only (`--read-only`, `--tmpfs /tmp:rw`), `HOME=/tmp` |
| CPU / RAM | 4 CPU / 16 GB |
| Task tree | bind mount per dataset, writable (`answer.txt` next to `task.json`) |
| Backend | `--llm-backend gateway` → `openjiuwen-gateway` |
| Model | `qwen3.6-35b-a3b-noreason` |
| Workers | `--max-workers 4` (`3` for the QuixBugs rerun) |
| LLM concurrency cap | `OJ_MAX_CONCURRENT_LLM=3` |
| Budget | `OJ_TOKEN_BUDGET=100000` (`budget_source=env:OJ_TOKEN_BUDGET`) |
| Cost basis / policy | `--cost-basis expected`, `policy=value_cost_greedy` |
| Coding timeout | `--test-timeout 10` for MBPP, QuixBugs and lorne |
| Credentials | `LLM_BASE_URL` / `LLM_API_KEY` from the environment; never written to a file |

Redacted command shape:

```bash
docker run --rm --read-only --tmpfs /tmp:rw \
  --cpus 4 --memory 16g \
  -e HOME=/tmp \
  -e OJ_TOKEN_BUDGET=100000 \
  -e OJ_MAX_CONCURRENT_LLM=3 \
  -e LLM_BASE_URL \
  -e LLM_API_KEY \
  -v "$PWD/eval/external/<dataset>:/data/tasks:rw" \
  hack:latest \
  python /app/run_agent.py --tasks-dir /data/tasks \
    --max-workers 4 --cost-basis expected [--test-timeout 10]
```

## Results

Each row is one isolated run. `score` is the sum of `reward` over solved tasks;
`tokens` is the harness cost unit (`output_tokens + input_tokens / 10`).
Overall: **75/75 tasks solved, score 188, 14989.6 units**.

| Dataset | Tasks | Solved | Failed | Score | Tokens | Elapsed |
|---|---:|---:|---:|---:|---:|---:|
| GSM8K | 15 | 15 | 0 | 45 | 349.6 | 68 s |
| WikiTableQuestions | 15 | 15 | 0 | 45 | 992.0 | 6 s |
| HotpotQA | 10 | 10 | 0 | 30 | 804.1 | 64 s |
| MBPP | 15 | 15 | 0 | 30 | 4611.0 | 25 s |
| QuixBugs | 10 | 10 | 0 | 20 | 6922.4 | 52 s |
| lorne | 10 | 10 | 0 | 18 | 1310.5 | 68 s |
| **Total** | **75** | **75** | **0** | **188** | **14989.6** | **~5 min** |

Non-coding generic path: **40/40 solved, 2145.7 tokens**. Coding path:
**35/35 solved, 12843.9 tokens**. No task was skipped, no run-level errors were
recorded, `budget.reserved=0.0` at the end of every run, and no run exhausted
the 100000-unit budget.

> Roll-up note: the six saved summaries sum to **14989.6** tokens
> (349.6 + 992.0 + 804.1 + 4611.0 + 6922.4 + 1310.5); the per-run JSON is the
> source of truth. The QuixBugs row is a rerun after the fenced-code fallback.

## Per-dataset notes

### GSM8K — 15/15, score 45, 349.6 tokens, 68 s

All tasks are `gsm8k`, deliberately unknown to the deterministic solvers, so
they go through `harness/solvers/generic.py`. The answer is extracted from the
model prose and checked with `exact_int`. Prompt 2936 / completion 56: these are
short, single-shot answers, ~23 units per task. No retries and no replan
fallbacks beyond the normal per-task attempts.

### WikiTableQuestions — 15/15, score 45, 992.0 tokens, 6 s

`table_qa`, generic path. Each task ships `workspace/table.csv` and asks the
model to answer from that table only; `expected` is the first normalized answer.
Prompt 9380 / completion 54 — the tables dominate input cost, output is a cell
value. This is the cheapest family per task (~66 units) and the fastest run.

### HotpotQA — 10/10, score 30, 804.1 tokens, 64 s

`context_qa`, generic path. Ten distractor paragraphs are concatenated into
`workspace/context.md`; the model must pick the supporting facts and return the
short answer. The multi-hop questions are the slowest of the generic families
(64 s) because each answer needs the whole distractor context in the prompt
(7531 prompt / 51 completion units).

### MBPP — 15/15, score 30, 4611.0 tokens, 25 s

`coding`. Workspaces contain `NotImplementedError` stubs and generated
`unittest` tests; upstream reference code is never copied into a workspace. The
coding solver writes a full-file patch and reruns the tests. All 15 passed, most
after one LLM iteration; 4611 tokens (16230 prompt / 2988 completion) is the
price of full-file patches and test output in the repair loop.

### QuixBugs — 10/10, score 20, 6922.4 tokens, 52 s

`coding`. Workspaces contain the buggy program and the sampled upstream tests.
All ten tasks solved after the fenced-code fallback (see below) on a rerun with
`--max-workers 3`. Two programs (`bitcount`, `find_first_in_sorted`) do not
terminate on the buggy code, which is why this dataset (and the other coding
runs) used `--test-timeout 10`; otherwise a hang would consume the per-test 60 s
default. QuixBugs is still the most expensive family per task, but the total
more than halved from 11626.8 to 6922.4 units and the run time from 195 s to
52 s, because the task that used to burn 6304.7 units on a rejected reply now
solves in 1288.3 units.

### lorne — 10/10, score 18, 1310.5 tokens, 68 s

10 converted lorne tasks: 8 `coding` (flag handling, docstrings, imports,
renames, traces, tests) and 2 `file_lookup` (read a definition / constant).
Both families solved; coding spent 1238.2 units, file lookup 72.3. The developer
tasks are much smaller than MBPP/QuixBugs, so 8 coding tasks cost 1310.5 in
total.

## Fenced-code fallback (previously the single failure)

The earlier QuixBugs run scored 9/10: `ext_quixbugs_8` ended `failed`
(solver `coding`, 6304.7 units, 166.8 s) with

```
LLM did not return a usable full-file patch
```

The model's reply wrapped the program in a markdown code fence instead of
emitting a bare full file, so the patch extractor rejected it and the coding
loop refused to guess. The extractor now also accepts a fenced code block as a
full-file patch. On the rerun the same task solves in 1288.3 units and 8.7 s
(`tests pass after 2 LLM iteration(s)`), and the suite is **75/75**. This was a
model-output-format issue, not a harness crash; the accepted text is still
validated by the shipped tests before `answer.txt` is written, so the fallback
cannot turn a wrong program into a solved task.

## Contamination caveat

GSM8K, WikiTableQuestions, HotpotQA, MBPP and QuixBugs are public datasets that
have been available for years and are very likely present in the training data
of current language models; lorne is an internal developer-task set and is less
exposed, but is still not held out. These numbers are therefore **robustness
evidence** — that the generic and coding paths work on real third-party task
shapes — and **not** an unbiased estimate of accuracy on unseen data. They say
nothing guaranteed about the official 100 tasks, whose distribution and
difficulty are unknown.

## What this proves

* The generic solver is not a stub: with a plain prompt and answer extraction it
  solves **40/40** math, table and multi-hop tasks without any task-type-specific
  code (GSM8K 15/15, WikiTableQuestions 15/15, HotpotQA 10/10).
* The coding loop generalises beyond the bundled examples: **35/35** on
  function implementation (MBPP 15/15), bug fixing (QuixBugs 10/10) and small
  developer tasks (lorne 8/8 coding + 2/2 file lookup).
* The planner/ledger stays within a 100000-unit budget across six runs with no
  overspend and no per-task cap; total spend was 14989.6 units.
* The harness fails safely and recovers from format variation: the one
  unusable model reply is now absorbed by the fenced-code fallback and verified
  by the same tests, instead of becoming a failed task.

## Reproduction

The converted fixtures are committed under `eval/external/`; see
[`eval/external/README.md`](../external/README.md) for counts, layout and how to
rerun a dataset. The six summaries in this directory are the exact outputs of
the runs above:

| Dataset | Summary |
|---|---|
| GSM8K | `external-gsm8k.json` |
| WikiTableQuestions | `external-wikitable.json` |
| HotpotQA | `external-hotpotqa.json` |
| MBPP | `external-mbpp.json` |
| QuixBugs | `external-quixbugs.json` |
| lorne | `external-lorne.json` |

Live runs need organizer credentials; the summaries record only aggregate
counts, token usage and per-task status, with no prompts, keys or endpoints.
