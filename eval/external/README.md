# External dataset tasks

Small, self-contained conversion samples of five public datasets into the
openJiuwen harness task contract (`task.json` + optional `workspace/`).  These
fixtures exist for local evaluation and regression runs only.  They are not
the full datasets and must not be treated as a benchmark leaderboard.

## Counts

| Directory | Upstream dataset | Tasks | Task type | Harness path | Reward |
|---|---|---:|---|---|---:|
| `mbpp/` | MBPP (sanitized) | 15 | `coding` | deterministic tests + LLM repair | 2 |
| `quixbugs/` | QuixBugs | 10 | `coding` | deterministic tests + LLM repair | 2 |
| `gsm8k/` | GSM8K | 15 | `gsm8k` | generic LLM solver | 3 |
| `wikitable/` | WikiTableQuestions | 15 | `table_qa` | generic LLM solver | 3 |
| `hotpotqa/` | HotpotQA (distractor) | 10 | `context_qa` | generic LLM solver | 3 |

Total: **65 tasks**.

`gsm8k`, `table_qa` and `context_qa` are deliberately unknown to the
deterministic solvers.  The runner therefore routes them to `harness/solvers/
generic.py`, which needs an LLM endpoint.  `coding` tasks are routed to
`harness/solvers/coding.py` and are solved by repairing the code until the
shipped tests pass.

## How to run

```bash
# One dataset
python3.11 run_agent.py --tasks-dir eval/external/mbpp

# Plan only (no LLM, no answers written)
python3.11 run_agent.py --tasks-dir eval/external/mbpp --offline --dry-run

# Coding datasets without a model: expected failures, useful as a negative check
python3.11 run_agent.py --tasks-dir eval/external/quixbugs --offline --max-workers 1

# LLM-path datasets without a model: expected failures (generic solver has no LLM)
python3.11 run_agent.py --tasks-dir eval/external/gsm8k --offline --max-workers 1
python3.11 run_agent.py --tasks-dir eval/external/wikitable --offline --max-workers 1
python3.11 run_agent.py --tasks-dir eval/external/hotpotqa --offline --max-workers 1
```

With a configured `LLM_BASE_URL` / `LLM_API_KEY`, use the same command without
`--offline`.  No task contacts the network on its own; only the harness LLM
client does, and only when `--offline` is absent.

### One known triage false positive

`ext_gsm8k_8` quotes a word problem that contains the standalone word
"download".  `harness/triage.py` treats that word as a network requirement and
skips the task at the default `--triage-threshold 0.6`, even though the task is
pure arithmetic.  The question is kept verbatim rather than edited.  To plan
all 15 GSM8K tasks, disable that heuristic for the local sample:

```bash
python3.11 run_agent.py --tasks-dir eval/external/gsm8k --offline --dry-run --triage-threshold 1.0
```

All other four datasets plan in full at the default threshold.

## Measured live runs

Every dataset was run end to end through the gateway path on 2026-11-11:
4 workers, `OJ_MAX_CONCURRENT_LLM=3`, `OJ_TOKEN_BUDGET=100000`,
`--cost-basis expected`, model `qwen3.6-35b-a3b-noreason`; the coding datasets
used `--test-timeout 10` because two QuixBugs programs hang on the buggy code.
The QuixBugs row is a rerun with 3 workers after the fenced-code fallback.
The counts table above covers the five public samples (65 tasks); `lorne/` adds
10 tasks converted by `eval/import_lorne_tasks.py`, hence 75 here.

| Dataset | Tasks | Solved | Failed | Score | Tokens | Elapsed |
|---|---:|---:|---:|---:|---:|---:|
| `gsm8k/` | 15 | 15 | 0 | 45 | 349.6 | 68 s |
| `wikitable/` | 15 | 15 | 0 | 45 | 992.0 | 6 s |
| `hotpotqa/` | 10 | 10 | 0 | 30 | 804.1 | 64 s |
| `mbpp/` | 15 | 15 | 0 | 30 | 4611.0 | 25 s |
| `quixbugs/` | 10 | 10 | 0 | 20 | 6922.4 | 52 s |
| `lorne/` | 10 | 10 | 0 | 18 | 1310.5 | 68 s |
| **Total** | **75** | **75** | **0** | **188** | **14989.6** | ~5 min |

The generic solver handles all 40 math/table/multi-hop tasks; the coding loop
handles all 35 coding tasks. The QuixBugs row is a rerun after the fenced-code
fallback: `ext_quixbugs_8` previously failed with "LLM did not return a usable
full-file patch" because the reply wrapped the program in a markdown fence; the
extractor now accepts a fenced block and the suite is 75/75. The accepted patch
is still checked by the shipped tests, so the fallback only fixes the
output-format rejection, not correctness. Two QuixBugs tasks (`bitcount`,
`find_first_in_sorted`) can only pass after a real fix because their buggy code
does not terminate. See `eval/results/external-eval.md` for the full report and
the raw summaries in `eval/results/external-*.json`.

These are public datasets and may be contaminated by model training; treat the
numbers as robustness evidence, not as an estimate for the official 100 tasks.

## Task layout

```
eval/external/<dataset>/
  _source/<sample>.jsonl          # exact upstream sample used for conversion
  ext_<dataset>_<n>/
    task.json                     # task_id, task_type, instruction, reward, expected, check
    workspace/                    # task inputs; the solver works here
```

* `mbpp`: `workspace/mylib.py` contains `NotImplementedError` stubs for the
  functions exercised by the tests; `workspace/test_mylib.py` is generated from
  `test_list` and `test_imports`.  The upstream reference `code` is never copied
  into a workspace.
* `quixbugs`: `workspace/<program>.py` is the buggy program and
  `workspace/test_<program>.py` is all sampled tests as `unittest` assertions.
  The upstream `correct_code` is never copied into a workspace.
* `gsm8k`: `workspace/` is empty; the instruction carries the word problem and
  asks for the final integer.
* `wikitable`: `workspace/table.csv` is the sampled table; the instruction asks
  for the answer using only that table.
* `hotpotqa`: `workspace/context.md` contains the ten distractor paragraphs as
  headed sections; the instruction asks for the answer using only that file.

## Conversion notes and ambiguity

* Task ids are `ext_<dataset>_<n>` with `n` starting at 1.  Numbering follows a
  deterministic sort, not the source file order: MBPP by numeric `task_id`,
  QuixBugs by `program`, WikiTableQuestions by `id`, HotpotQA by `id`; GSM8K
  keeps source order because the sample has no id.
* MBPP sample rows have no `entry_point`.  Function names are inferred from
  non-builtin call names in `test_list`, with top-level `def` names from the
  reference code used only as a fallback.  Stub arity is the largest positional
  arity observed in the test calls; keyword/starred calls fall back to
  `*args, **kwargs`.  `test_imports` is empty in this sample, but the converter
  preserves it if present.
* QuixBugs source sample has 12 programs; the brief asks for 10.  The two
  alphabetically last programs (`knapsack`, `kth`) are not converted.
* Two converted QuixBugs programs (`bitcount`, `find_first_in_sorted`) do not
  terminate under the buggy code: they need a real fix, not just a different
  assertion.  The coding solver's per-test timeout (60 s by default) turns the
  hang into a normal failed task; pass `--test-timeout 5` for a fast negative
  run.
* GSM8K `expected` is the integer after the final `####` marker, with thousands
  separators removed.
* WikiTableQuestions `expected` is the first answer, normalized with Unicode
  NFKC and whitespace collapsing.  Table headers with embedded newlines or
  duplicate column names are written to CSV verbatim.
* HotpotQA `expected` is the upstream answer string, stripped but otherwise
  unchanged.

## Regenerating

The converter reads only the verified samples in `/tmp/external-datasets` and
never downloads anything:

```bash
python3.11 eval/import_datasets.py --src /tmp/external-datasets --out eval/external
```

Regeneration removes only `ext_<dataset>_*` task directories and recreates
`_source/`; `README.md`, `LICENSES.md` and unrelated directories such as
`lorne/` are left alone.

## Answer leakage

`expected` lives in `task.json` as required by the harness contract.  Reference
solutions are **not** placed in any `workspace/`: MBPP ships stubs only and
QuixBugs ships buggy code only.  The `_source/` files are provenance copies of
the upstream samples and do contain upstream reference code; they are outside
all task workspaces and must not be copied into one.

## Validation

```bash
python3.11 - <<'PY'
from harness.task import load_tasks
for name, want in {
    "mbpp": 15, "quixbugs": 10, "gsm8k": 15,
    "wikitable": 15, "hotpotqa": 10,
}.items():
    tasks = load_tasks(f"eval/external/{name}")
    assert len(tasks) == want, (name, len(tasks))
    assert len({task.task_id for task in tasks}) == want
    print(name, len(tasks), "ok")
PY
```
