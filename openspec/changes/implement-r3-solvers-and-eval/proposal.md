# Proposal

## Why

Developer 3 должен довести solvers и evaluation до проверяемого состояния до freeze
12.11.2026 18:00 MSK. Рабочий baseline (deterministic solvers, generic, coding
full-file, lorne fixtures) уже есть, но пункты R3-01…R3-08 из v5.0 не закрыты:
unknown fields `task.json` теряются, unmatched deterministic не уходит в generic,
SEARCH/REPLACE отсутствует, `impossible` ставится по фразам, пустой `answer.txt`
не создаётся, 100-task mock не воспроизводится. Без этих инвариантов неизвестный
формат или ложный skip обнулят reward.

## What Changes

- Сохранять полный decoded `task.json` в `Task.raw` без потери неизвестных полей.
- Передавать unmatched deterministic solver в generic при доступном LLM.
- Оставить generic последней веткой: формат из instruction/`check`, 1+1 вызов.
- Вынести безопасный SEARCH/REPLACE в `harness/patch.py`; full-file остаётся fallback.
- Снизить coding repair до 3 итераций, передавать первую diagnostic ошибку, защищать хеши тестов.
- Добавить консервативный `SolveabilityChecker`: `impossible` только по evidence.
- **BREAKING** относительно текущих contract-тестов: на старте создавать пустой
  `answer.txt` (v5 §10.2); пустой файл не считается solved.
- Добавить `eval/fake_100.py` и воспроизводимый offline-отчёт official-8 + lorne-10.

Out of scope: MAPE как цель; per-task cap; локальные LLM; перенос runtime lorne;
reshape `SolveResult`/`SolverContext`; async `Solver.supports`; fuzzy/AST patch
если не успеваем после P0; archive чужого change `add-token-estimator`; live proxy.

## Capabilities

### New Capabilities

- `task-execution`: загрузка `task.json` с `raw`, маршрутизация solver, generic
  fallback, обязательный пустой `answer.txt` на старте.
- `coding-repair`: SEARCH/REPLACE с уникальным совпадением, лимит 3 итераций,
  защита проверяющих файлов, честный провал без фиктивного solved.
- `solveability`: вердикты `solvable`/`uncertain`/`impossible` только по
  проверяемому evidence; unknown type не impossible.
- `evaluation`: official smoke, lorne subset, 100 synthetic mock, missing usage
  как null, split без утечки.

### Modified Capabilities

(пусто: `openspec/specs/` пуст, главные specs ещё не архивированы)

## Impact

- `harness/task.py`, `harness/solvers/**`, `harness/patch.py`, `harness/solveability.py`
- `harness/runner.py` (fallback + placeholder), `harness/store.py` (ensure empty)
- `harness/solvers/base.py`, `run_agent.py` (`max_iterations` default 3)
- `harness/planner.py`, `harness/policy.py` (skip только при `impossible`)
- `tests/test_task_formats.py`, `tests/test_patch.py`, `tests/test_solveability.py`,
  `eval/fake_100.py`, `eval/results/dev3_*.md`
- Contract tests, которые требуют отсутствия `answer.txt` у failed задач
