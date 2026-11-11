# D3 baseline (Gate 0)

**Date:** 2026-11-12  
**Commit:** `8cbe25f9b90d266458c75eaa8f4b19950e7b9572` (`main`)  
**Python:** `/home/asgaroth/miniconda3/envs/coding-agent/bin/python3.11` (`Python 3.11.16`)  
**OpenSpec:** standalone `1.17.0` at `~/.local/bin/openspec` (installed after CLI was missing from PATH)  
**Node:** `v26.8.1`  
**Working tree at start:** `?? .cursor/` `?? docs/` (Cursor setup, not harness)

`python3.11` is not on default PATH; all commands below used the coding-agent conda env.

## Unit

```bash
python3.11 -m unittest discover -s tests
```

- exit code: **0**
- Ran **330** tests in 9.797s
- OK (**skipped=2**)

## Contract

```bash
python3.11 -m unittest discover -s tests/contract
```

- exit code: **0**
- Ran **32** tests in 4.733s
- OK

## Offline official 8 (copy, not `examples/`)

```bash
python3.11 run_agent.py --tasks-dir <copy>/task-dir --offline --max-workers 1
```

Log: `eval/results/dev3_baseline_offline8.log`

- exit code: **0**
- tasks=8, planned=8, solved=**6**, failed=**2** (coding), skipped=0, score=**16.0**
- tokens_spent=0.0
- Matches documented offline-without-mutation-repair baseline (6/8, score 16). Coding fails without LLM.

## Offline extended 16 (copy)

```bash
python3.11 run_agent.py --tasks-dir <copy>/task-dir-extended --offline --max-workers 1
```

Log: `eval/results/dev3_baseline_offline16.log`

- exit code: **0**
- tasks=16, planned=16, solved=**12**, failed=**4** (coding), skipped=0, score=**42.0**
- tokens_spent=0.0
- README 14/16 score 46 is **not** this command: that figure requires mutation-repair and/or live LLM. This baseline is the actual `--offline` run without `--mutation-repair`.

## Notes

- Live LLM was **not** run.
- Baseline is green. D3 work may start.
- Official регламент file is not in the repo.
- `/home/eq/techlb/TCA` is absent; lorne subset is `eval/external/lorne/`.
