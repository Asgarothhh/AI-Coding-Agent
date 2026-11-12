# R3 hardening — initial state (H0)

Captured before any hardening edits. Do not treat this as a new eval run.

## Paths

- Working copy: `/home/asgaroth/Projects/AI-Coding-Agent`
- Brief path `/home/eq/techlb/openjiuwen-challenge`: **absent** on this machine
- Date: 2026-11-12

## Git

```
pwd: /home/asgaroth/Projects/AI-Coding-Agent
branch: main...origin/main [ahead 1]
HEAD: fe2ec9e053acf54ca68b00f151ec4af926c0778c
subject: feat(r3): implement solvers and eval OpenSpec change
git diff --stat: (empty — no staged/unstaged tracked diffs)
```

Untracked (not part of this hardening task; do not overwrite):

- `.cursor/` (agents, plans, rules, skills)
- `docs/development/cursor-setup-audit.md`

Note vs last D3 report: `dev3_progress.md` still cites HEAD `8cbe25f9…`. Actual HEAD is `fe2ec9e` (R3 already committed). The brief’s “nothing committed” is stale.

## Toolchain

- Python: `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` → `Python 3.11.16`
- pip: `26.2.1` (same env)
- OpenSpec: `/home/asgaroth/.local/bin/openspec` → `1.17.0`
- `npx` OpenSpec install: **not** re-run (previous npm 502)

## OpenSpec

```
openspec list
  implement-r3-solvers-and-eval     Complete
  add-token-estimator               24/40 tasks

openspec list --archived
  No archived changes found.

openspec status --all
  add-token-estimator: 4/4 artifacts complete
  implement-r3-solvers-and-eval: 4/4 artifacts complete

openspec status --change implement-r3-solvers-and-eval
  4/4 artifacts complete (proposal, specs, design, tasks)

openspec validate implement-r3-solvers-and-eval --strict
  Change 'implement-r3-solvers-and-eval' is valid
  exit 0
```

Change remains **applied / Complete / not archived**. This phase must not archive it. R3-04 and R3-07 stay PARTIAL in D3 reports even though `tasks.md` checkboxes are `[x]`.

## Ownership map (this phase)

| Zone | Owner | This phase |
|---|---|---|
| `eval/fake_100.py`, `eval/results/**`, `tests/test_fake_100.py` | D3 | **write** |
| `tests/contract/test_budget_exhaustion.py`, `tests/contract/test_budget_concurrency.py` | D3 consumer of D1 API | **write** (public ledger only) |
| `harness/budget.py`, `harness/usage.py`, `harness/openjiuwen_gateway.py`, `harness/llm.py`, `run_agent.py`, `scripts/**` | D1 | **forbidden** |
| `harness/estimator.py`, `harness/planner.py`, `harness/policy.py`, `harness/calibration.json` | D2 | **forbidden** |
| `harness/solvers/**`, `harness/patch.py`, `harness/solveability.py` | D3 | **do not touch** in this phase |
| `openspec/changes/implement-r3-solvers-and-eval/**` | Integrator/D3 | **do not archive**; no delta edits |
| `openspec/changes/add-token-estimator/**` | mixed | **forbidden** |
| `.cursor/`, `docs/` | tooling | leave untracked as-is |

## Baseline artifact preserved

Copied **before** any new `fake_100` run:

- source: `eval/results/fake_100.json`
- copy: `eval/results/fake_100_baseline.json`
- sha256: `055a41300681e80160ba9538159773ea5eace5eab0e0fe004d1f20ffea235a79`

Contents (historical R3-08 run, seed 2026, workers=5):

| field | value |
|---|---|
| solved | 59 |
| failed | 17 |
| skipped | 16 |
| sum of published states | **92** (not 100) |
| score | 76.0 |
| tokens_spent | 360.0 |
| fake_llm_calls | 68 |
| spent+reserved | 360 <= 100000 |
| errors | [] |
| tree_hash | `228d6d0b485ac653007713f22b9190ed042cc5489b384bfceef783b7fd51a96d` |

Hypothesis to confirm in slice A: eight missing IDs are generator bucket 4 (`invalid_json`): `syn_004, syn_016, syn_028, syn_040, syn_052, syn_064, syn_076, syn_088`. `load_tasks` skips them; the report writer discarded per-task records.

## Last known D3 verification (historical, not re-run at H0)

From `eval/results/dev3_progress.md` (HEAD cited there: `8cbe25f9…`):

- unit: 370 OK, skipped=2
- contract: 32 OK
- offline 8 copy: 6/8 score 16 tokens 0
- offline 16 copy: 12/16 score 42
- lorne offline copy: 0/10
- OpenSpec strict: valid
- R3-01/02/03/05/06/08 DONE; R3-04/R3-07 PARTIAL
