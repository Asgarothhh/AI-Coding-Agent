# Tasks

## 0. Gate 0 — контекст (D3 / Integrator)

- [x] 0.1 D3 — Зафиксировать OpenSpec CLI, каталог и change `implement-r3-solvers-and-eval`. Файлы: `openspec/changes/implement-r3-solvers-and-eval/**`. Проверка: `openspec validate implement-r3-solvers-and-eval --strict`. Критерий: strict OK, `add-token-estimator` не изменён.
- [x] 0.2 D3 — Записать baseline unit/contract/offline. Файлы: `eval/results/dev3_baseline.md`. Проверка: `python3.11 -m unittest discover -s tests`. Критерий: exit 0 и фактические числа в отчёте.
- [x] 0.3 D3 — Записать research notes. Файлы: `eval/results/dev3_research.md`. Проверка: файл содержит источники OpenSpec, Cursor, Aider SEARCH/REPLACE, SWE-agent, OpenHands и что не переносилось. Критерий: нет копирования чужого runtime.

## 1. Gate 1 — формат задач и маршрутизация (R3-01, R3-02, D3)

- [x] 1.1 D3 — Сохранить `Task.raw` и валидацию reward/полей. Файлы: `harness/task.py`, `tests/test_task.py`, `tests/test_task_formats.py`. Проверка: `python3.11 -m unittest tests.test_task tests.test_task_formats -v`. Критерий: unknown fields round-trip в `raw`, невалидный JSON пропускается.
- [x] 1.2 D3 — Безопасное разрешение путей задачи/патча. Файлы: `harness/task.py`, `tests/test_task_formats.py`. Проверка: `python3.11 -m unittest tests.test_task_formats -v`. Критерий: `..`, абсолютный путь и symlink escape отклоняются.
- [x] 1.3 D3 — Unmatched deterministic → generic при доступном LLM. Файлы: `harness/runner.py`, `tests/test_solvers.py`. Проверка: `python3.11 -m unittest tests.test_solvers tests.test_generic_solver -v`. Критерий: official non-coding 0 LLM calls offline; unparseable math + FakeLLM идёт в generic.

## 2. Gate 2 — generic solver (R3-03, D3)

- [x] 2.1 D3 — Добавить компактный raw/keys в prompt generic, сохранить 1+1 вызов. Файлы: `harness/solvers/generic.py`, `tests/test_generic_solver.py`. Проверка: `python3.11 -m unittest tests.test_generic_solver -v`. Критерий: unknown type не роняет run; пустой ответ не solved; все chat через FakeLLM/gateway.

## 3. Gate 3 — coding repair и patch (R3-05, R3-04, D3; D1 CLI)

- [x] 3.1 D3/D1 — Default `max_iterations=3` и первая diagnostic ошибка. Файлы: `harness/solvers/base.py`, `harness/solvers/coding.py`, `run_agent.py`, `tests/test_coding.py`. Проверка: `python3.11 -m unittest tests.test_coding -v`. Критерий: после 3 неудач 4-го chat нет, хеши тестов восстановлены.
- [x] 3.2 D3 — SEARCH/REPLACE в `harness/patch.py` с unique match. Файлы: `harness/patch.py`, `harness/solvers/coding.py`, `tests/test_patch.py`. Проверка: `python3.11 -m unittest tests.test_patch tests.test_coding -v`. Критерий: 0/>1 match не применяются; full-file fallback остаётся зелёным.

## 4. Gate 4 — solveability (R3-06, D3; D2 call site)

- [x] 4.1 D3 — Консервативный checker. Файлы: `harness/solveability.py`, `tests/test_solveability.py`. Проверка: `python3.11 -m unittest tests.test_solveability tests.test_triage -v`. Критерий: phrase `impossible` и math без цифр не auto-impossible; missing file — impossible.
- [x] 4.2 D2/D3 — Skip planner только по `impossible`. Файлы: `harness/planner.py`, `harness/policy.py`, `tests/test_integration_planning.py`. Проверка: `python3.11 -m unittest tests.test_integration_planning tests.test_planner -v`. Критерий: uncertain остаётся кандидатом.

## 5. Gate 5 — eval и 100-task mock (R3-07, R3-08, D3)

- [x] 5.1 D3 — Placeholder `answer.txt` на старте. Файлы: `harness/store.py`, `harness/runner.py`, `tests/test_store.py`, `tests/contract/test_paths.py`. Проверка: `python3.11 -m unittest tests.test_store tests.contract.test_paths -v`. Критерий: пустой файл есть, solved только непустой.
- [x] 5.2 D3 — `eval/fake_100.py` seed/manifest. Файлы: `eval/fake_100.py`. Проверка: `python3.11 eval/fake_100.py --tasks 100 --workers 5`. Критерий: 100 answer.txt, budget invariant, synthetic true, два прогона совпадают.
- [x] 5.3 D3 — Отчёт official-8 и lorne-10 offline. Файлы: `eval/results/dev3_report.md`. Проверка: `python3.11 -c "from harness.task import load_tasks; assert len(load_tasks('examples/task-dir'))==8; assert len(load_tasks('eval/external/lorne'))==10"`. Критерий: числа из фактических прогонов, live lorne помечен historical.

## 6. Интеграционный regression

- [x] 6.1 D3 — Полный unit+contract+offline smoke. Файлы: `eval/results/dev3_progress.md`. Проверка: `python3.11 -m unittest discover -s tests && python3.11 -m unittest discover -s tests/contract`. Критерий: exit 0; offline 8 не хуже baseline 6 solved / score 16 без mutation.

## Workflow follow-up

- Reviewer после каждого Gate; Critical/High закрыть implementer-ом.
- Не архивировать change, пока P0 не verified; `add-token-estimator` не трогать.
- Коммиты только по явной просьбе владельца репозитория.
