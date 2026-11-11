# Design

## Context

См. `proposal.md`. Контракты данных — `specs/001-token-estimator/spec.md` v5.0 §4
(`Task`, `SolveResult`, `SolverContext`); публичные dataclass **не переименовываются**.
Фактический код: маршрутизация `_SOLVERS` в `harness/runner.py`; патч и hash-защита
в `harness/solvers/coding.py`; feasibility в `harness/triage.py`. Отдельных
`patch.py`/`solveability.py` нет. `openspec/specs/` пуст.

## Goals / Non-Goals

**Goals:**
- Закрыть R3-01…R3-08 минимальными вертикальными срезами на существующем стеке.
- Сохранить offline 6/8 (без mutation) и не регрессировать unit/contract.
- Один путь LLM: только `SolverContext.llm` + ledger.

**Non-Goals:**
- Async `supports()` / новый Solver Protocol.
- Перенос runtime lorne, SWE-agent, OpenHands.
- Новые зависимости и fuzzy/AST, если P0 не закрыт.
- Правки `budget.py` / gateway / estimator, кроме согласованных call site.

## Decisions

### 1. Тонкие модули вместо копирования
`harness/patch.py` — лестница SEARCH/REPLACE + `closest_window`. `coding.py` вызывает
его и оставляет JSON/fence full-file fallback (lorne/QuixBugs). `harness/solveability.py`
возвращает `solvable|uncertain|impossible`; `triage.py` остаётся scoring D2.
**Почему:** v5 и AGENTS.md называют эти файлы, дублировать цикл ремонта нельзя.
**Альтернатива:** править только `coding.py`/`triage.py` — ломает владение файлами.

### 2. Не менять форму SolveResult / SolverContext
Runner уже мапит `answer is None` → failed/skipped. Fallback unmatched deterministic:
в `run_one` после None от math/search/data_analysis, если `llm.available`.
**Почему:** reshape — контракт D1, вне scope freeze.
**Альтернатива:** `supports()` chain — большой blast radius.

### 3. Placeholder answer.txt через store
`ensure_placeholder(task)` атомарно создаёт пустой файл, если его нет; solved пишет
непустой ответ. Пустой ≠ solved.
**Почему:** v5 §10.2 и R3-08. Текущие contract-тесты «нет файла» обновляются.
**Альтернатива:** писать пустой только в fake_100 — расхождение production/mock.

### 4. Impossible только из solveability
`planner.build_plan` и `policy._impossible_tasks` используют solveability, а не
phrase-score triage. Phrase `"impossible"` и «math без цифр» → `uncertain`.
**Почему:** иначе R3-06 не меняет skip.
**Альтернатива:** только новые тесты без смены call site — false impossible останется.

### 5. Default max_iterations = 3
`SolverOptions` / `RunOptions` / CLI. Full-file fallback сохраняется.
SEARCH/REPLACE уровни: exact → newline/trailing → indent. Fuzzy ≥ 0.92 и AST —
только после P0.
**Почему:** v5 §9.3; fuzzy без stdlib-ratio уникальности рискован.
**Альтернатива:** оставить 6 — нарушает requirement.

### 6. fake_100 генерирует temp dirs
Не чекинить 100 деревьев. Seed + manifest `synthetic: true`. Fake LLM через тот
же adapter, что runtime. Workers=5, при флапе → 1.

## Risks / Trade-offs

- [Пустой answer.txt ломает contract «нет файла»] → обновить oracle на «существует и пуст»; пустой не solved.
- [3 итерации регрессируют coding] → измерить extended; mutation-repair не трогать.
- [Generic fallback тратит бюджет] → только при available LLM; official 6 non-coding остаются 0 tokens offline.
- [D2 call site] → минимальные правки planner/policy; при конфликте стоп файла.
- [TCA отсутствует] → только `eval/external/lorne`; live 10/10 historical.

## Migration Plan

1. Тесты форматов и raw, затем fallback runner, затем generic residual.
2. Placeholder answer + обновление contract.
3. cap=3, patch.py, solveability.
4. fake_100 + отчёт. Откат: git checkout затронутых D3 файлов; D1/D2 файлы не трогать при конфликте.

## Open Questions

- Точный размер общего бюджета соревнования неизвестен; fake_100 использует явный синтетический budget в манифесте.
