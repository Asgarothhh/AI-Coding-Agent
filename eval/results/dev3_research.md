# D3 research notes

Research for R3 solvers/eval. Live LLM was not used. TCA path `/home/eq/techlb/TCA` is absent.

## Sources checked

| Source | What was used | What was not copied |
|---|---|---|
| OpenSpec install/setup/CLI (`openspec.dev/docs/*`, CLI 1.17.0) | Brownfield catalog, `openspec new change`, artifact schema spec-driven, `--strict` validate, do not create a second root | Did not run `openspec init --tools cursor` (would write `.agents/skills/` and can wipe legacy command files). Did not use `config reset`. |
| Cursor subagents (`cursor.com/docs/subagents`) | Specialized roles, explicit delegation, sequential writer because of shared checkout | No fourth permanent subagent |
| Aider edit formats (`aider.chat/docs/more/edit-formats.html`) | SEARCH/REPLACE unique block is cheaper and safer than whole-file; whole-file remains a fallback | Did not port Aider runtime, udiff parser, or architect/editor split |
| SWE-agent (GitHub README/principles) | Localize defect → reproduce → compact edit → run tests | Did not port SWE-agent agent loop, tools, or Docker image |
| OpenHands Software Agent SDK (testing guide principles) | Unit without LLM → component with fake LLM → e2e | Did not port OpenHands SDK or extra test runner |
| openJiuwen Core (`openjiuwen.com/en/`) | Solvers must use existing gateway/ledger; no second client | Did not rewrite D1 gateway |
| In-repo lorne (`eval/external/lorne`) | 10 converted tasks as second polygon | Did not import TCA runtime, local models, secrets, or `check.sh` |

## Principles applied

1. Small vertical slices, not a solver rewrite.
2. Test first for each gap (fail → fix → pass).
3. Minimal patch over full-file overwrite; keep working full-file path.
4. Independent checkers; golden fixtures stay versioned.
5. Missing usage is unknown, not zero.
6. Each failure has reason_code and a reproducible command.
7. One model invocation path.

## Explicit non-transfers

SWE-agent retrieval memory, best-of-N, OpenHands cloud runtime, Aider whole-repo map, lorne local LLM, extra pip packages for fuzzy matching.
