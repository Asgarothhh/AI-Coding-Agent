# Extended Task Set (`task-dir-extended`)

Additive extension set of **16 tasks** (8 original + 8 additional), extracted from
`task-dir.rar`.

> This set is **additive**. `examples/task-dir` remains the frozen 8-task contract
> baseline and is not modified by this directory.

## Tasks by type

### coding
| task_id | reward |
| --- | --- |
| `coding_0003` | 2 |
| `coding_0007` | 2 |
| `coding_0008` | 4 |
| `coding_0009` | 4 |

### data_analysis
| task_id | reward |
| --- | --- |
| `data_analysis_0002` | 3 |
| `data_analysis_0006` | 3 |
| `data_analysis_0007` | 4 |
| `data_analysis_0008` | 5 |

### math
| task_id | reward |
| --- | --- |
| `math_0000` | 5 |
| `math_0004` | 2 |
| `math_0005` | 5 |
| `math_0006` | 5 |

### search
| task_id | reward |
| --- | --- |
| `search_0001` | 1 |
| `search_0005` | 2 |
| `search_0006` | 3 |
| `search_0007` | 4 |

## Structure

Each task keeps its exact relative layout:

```
<task_id>/task.json
<task_id>/workspace/...
```

Task ids are unchanged.

## Excluded artifacts

The following files are intentionally **not** copied from the archive because they are
machine-specific caches or generated outputs, not part of the task contract:

- `.DS_Store`
- `__pycache__/`
- `*.pyc`
- generated `answer.txt`

## Usage

```bash
python3.11 run_agent.py --tasks-dir examples/task-dir-extended --offline --mutation-repair --max-workers 1
```

## Measured offline result

Run on a copy with `--max-workers 1`:

| type | total | solved | reward solved | comment |
| --- | --- | --- | --- | --- |
| math | 4 | 4 | 17 | deterministic arithmetic path |
| data_analysis | 4 | 4 | 15 | deterministic CSV aggregation |
| search | 4 | 2 | 3 | multi-hop chains fail |
| coding | 4 | 2 | 4 | multi-function bugs need the LLM |

```bash
python3.11 run_agent.py --tasks-dir examples/task-dir-extended --offline --mutation-repair --max-workers 1
```

Result: **16 tasks, 12 solved, 0 skipped, 4 failed gracefully, score 39, 0 tokens**.

The four failures are graceful, not errors, and both groups need the LLM path:

* `search_0006`, `search_0007` — multi-hop relation chains that the deterministic
  retrieval cannot complete (e.g. `field 'founded' missing`);
* `coding_0008`, `coding_0009` — multi-function bugs that require generated fixes
  rather than the local repair loop.
