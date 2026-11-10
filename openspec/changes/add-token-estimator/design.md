# Design

## Context

`specs/001-token-estimator/spec.md` v5.0 заменила v4.0. Регламент приоритетнее
внутреннего плана: баллы начисляются за `reward` решённых задач, а не за точность
прогноза; design строится вокруг бюджетной дисциплины и сдачи `answer.txt`.

Baseline из v4.0 независимо не проверен: разработчик 1 обязан повторить его и
приложить лог. `openjiuwen==0.1.20.post1` закрепляется в lock-файле, совместимость
с прокси доказывается smoke-тестом. Runtime: общий бюджет, `LLM_BASE_URL`/
`LLM_API_KEY`, 4 CPU, 16 ГБ RAM, 1 час, read-only rootfs, обязательный
`<TASKS>/<task_id>/answer.txt`.

## Goals / Non-Goals

**Goals:**

- Максимизировать `Σ(reward_i × solved_i)` при общем бюджете и одном часе. - Обеспечить ноль
перерасходов: ни один LLM-вызов не уходит без атомарного резерва, `spent + reserved <= B`
всегда. - Доказать путь через openJiuwen Core SDK реальным вызовом и proxy usage и оставить
контур loader → planner → reserve → SDK → settle → verify → `answer.txt`. - Обеспечить
воспроизводимость: версии, seed, split по задаче, фактические команды и логи.

**Non-Goals:**

- MAPE, R² или конформное покрытие как цель либо гейт сдачи. - Конформные гарантии и интервалы
`[q10, q90]`, выдаваемые за гарантию. - Per-task cap: индивидуальный лимит задачи регламентом не
задан. - Локальные нейросетевые модели, GPU, внешние LLM endpoints. - Сложный плагинный реестр,
WorkSwarm UI, база данных, heartbeat, регистрация агента и правки `team.json`. - T8, T11, T16,
T17, T18, нейросетевой оценщик, геометрическая модель попыток — без измеримого влияния на score.

## Decisions

### 1. BudgetProvider изолирует источник бюджета

`B` читается только через `BudgetProvider`: официальный аргумент/env либо `OJ_TOKEN_BUDGET` для
локальных тестов; отсутствие значения — ошибка до первого вызова. **Почему:** в команде запуска
нет аргумента бюджета, а выдуманное число делает планирование фиктивным. **Альтернативы:**
20k/50k/100k — синтетика; `task.json` — там его нет.

### 2. BudgetLedger: reserve → settle → release_failed

Один реестр на процесс. `reserve` под `threading.Lock` проверяет и списывает резерв; `settle`
сверяет с фактическим `TokenUsage` и возвращает излишек; `release_failed(...,
definitely_not_sent=True)` возвращает резерв только при доказательстве, что запрос не ушёл.
**Почему:** иначе workers дважды резервируют остаток, а потерянный ответ считается бесплатным.
**Альтернативы:** учёт только по факту — перерасход; счётчик в каждом solver — второй источник
истины.

### 3. Один worker до теста конкуренции

Старт с `--max-workers 1`; до 5 сессий — только после контрактного теста, где два запроса
конкурируют за остаток и инвариант не нарушается. **Почему:** регламент не уточняет, 5 сессий —
это LLM-сессии или worker-задачи. **Альтернативы:** сразу 5 workers — быстрее, но без
доказанного резервирования недопустимо.

### 4. OpenJiuwenGateway на Core SDK и обязательный smoke

Все вызовы идут через `OpenJiuwenGateway.generate(...)` на `openjiuwen` Core:
`ModelClientConfig(api_base=LLM_BASE_URL, api_key=LLM_API_KEY)`,
`ModelRequestConfig(model=...)`, `LLMComponent`/`WorkflowAgent`, `Runner`. До planner — smoke:
короткий запрос, адрес прокси, raw usage, reasoning, отсутствие скрытых вызовов; fixture
сохраняется. **Почему:** установка пакета не доказывает требование «на базе openJiuwen».
**Альтернативы:** скрытый OpenAI-клиент — нарушение; WorkSwarm — лишнее приложение, не SDK.

### 5. Только четыре модели, `noreason` по измерению

`qwen3.8-27b`, `qwen3.8-27b-noreason`, `qwen3.6-35b-a3b`, `qwen3.6-35b-a3b-noreason`. Выбор — по
фактическим `(model, solver, task_type)`: успех на отложенных задачах и reward на единицу
расхода; пока статистики мало — одна модель, прошедшая smoke. `noreason` — только при сохранении
решаемости и экономии. **Почему:** список разрешён регламентом. **Альтернативы:** bandit в
основном прогоне — трата бюджета.

### 6. Простой оценщик вместо конформной модели

Fallback: медиана `(solver, task_type, model)` → `(solver, task_type)` → глобальная → эвристика
по объёму prompt. Confidence: `high` ≥ 20 наблюдений, `medium` 8–19, `low` < 8. `upper_cost` —
эмпирический запас, не гарантия. После ответа обновляется in-memory множитель, пересчитываются
только незапущенные задачи; `calibration.json` не переписывается. **Почему:** на 8–20 задачах
сложные модели не проверяются, ошибку исправляют резерв и перепланирование. **Альтернативы:**
conformal predictor — гарантия не держится при малом n; нейросеть ≤ 5 МБ — выигрыш не доказан.

### 7. Планировщик по ожидаемому reward

`v_i = reward_i × P_solve_i`, `c_i = upper_cost_i`, `t_i` — оценка времени. Максимизируем `Σ(v_i
× x_i)` при `Σ(c_i × x_i) <= remaining - planning_reserve` и `Σ(t_i × x_i) <= remaining_time`.
Порядок — `v_i / c_i`; после каждой задачи план пересчитывается. `planning_reserve` — 5%, при
`low` confidence — 10%. **Почему:** reward — ценность, не сложность; «брать всё» игнорирует
остаток. **Альтернативы:** knapsack — только если успевает с тестами; иначе greedy.

### 8. `answer.txt` обязателен, диагностика — вне каталога задачи

Пишем `<TASKS>/<task_id>/answer.txt` атомарно: temp в том же каталоге → UTF-8 → `flush` →
`os.fsync` → `os.replace` → fsync каталога. На старте пустой файл создаётся там, где его нет;
пустой не входит в `solved_count`. `trace.jsonl` и `predictions.jsonl` — в `/tmp` и stderr;
`OUT` не обязателен. **Почему:** регламент требует `answer.txt`, а `OUT` не передаётся и не
монтируется в приведённой команде. **Альтернативы:** писать в `OUT` — потеря ответов; держать в
памяти — потеря при таймауте.

### 9. Детерминированные солверы первыми, generic — fallback

Покрытый шаблон решается без LLM. Неизвестный тип идёт в generic solver: читает фактическую
инструкцию и `task.json`, не требует JSON без запроса, делает один ограниченный вызов и максимум
одну попытку форматирования при достаточном резерве. `impossible` — только при проверяемом
доказательстве; иначе `uncertain` с `reward × P_solve`. **Почему:** 8/8 при нуле токенов
доказывает пользу детерминированного пути, но не обобщается на 100 задач. **Альтернативы:** LLM
для всех типов — дороже; эвристический отказ — потеря reward.

### 10. Coding repair: уникальный SEARCH/REPLACE и защита тестов

Цикл: план → минимальный патч → применение только при однозначном совпадении → синтаксис →
связанные тесты → точная первая ошибка в следующий промпт. Лестница: точное совпадение →
пробелы/переводы → отступы → AST-якорь → fuzzy при одном кандидате. При 0 или >1 совпадении патч
не применяется, возвращается `closest_window`. Хеши проверяющих файлов сохраняются и
восстанавливаются; успех с подменёнными тестами запрещён; максимум 3 итерации. **Почему:**
перезапись файла дороже и хрупче, подмена тестов даёт ложный `answer.txt`. **Альтернативы:**
unified diff — чаще ломается; best-of-two и память — только после базового цикла.

### 11. Docker и runtime под 4 CPU

Проектируем и тестируем под `--cpus 4` (до письменного подтверждения 8): read-only rootfs,
`tmpfs /tmp:rw,size=2g`, `HOME=/tmp`, `--cap-drop ALL`, `no-new-privileges`, bind mount
`$TASKS:/data/tasks:rw`, 16 ГБ RAM, образ < 4 ГБ, сборка < 1000 с, без скачиваний после сборки;
digest образа и lock-файл. **Почему:** в регламенте 4 CPU, в примере 8; широкие локальные права
делают тест недействительным. **Альтернативы:** оптимизация под 8 ядер — риск таймаута;
непривилегированный `USER` без проверки mount — риск отсутствия записи.

### 12. Три разработчика и фиксированные зоны файлов

Разработчик 1: `run_agent.py`, runtime, gateway, budget, usage, store, Docker. Разработчик 2:
estimator, planner, policy, calibration и их тесты. Разработчик 3: runner, solvers, patch,
triage, solveability, eval. Контракт раздела 4 согласуется в первый час; чужой файл не правится
без согласия владельца. **Почему:** лейны A/B/C/D/F пересекаются по файлам и dataclass.
**Альтернативы:** свободные зоны — быстрее старт, дороже интеграция.

### Решение: никаких изменений без выигрыша на отложенных данных

Новая реализация включается по умолчанию, только если проходит unit/contract, не ухудшает
отборочный прогон, не добавляет неконтролируемых LLM-вызовов, не нарушает бюджетный инвариант,
даёт не худший TotalScore на отложенных задачах при сопоставимом бюджете и укладывается в
runtime/RAM/CPU/размер образа. Иначе остаётся базовая версия. Улучшение MAPE без выигрыша по
score основанием не является.

## Architecture

```text
run_agent.py
  RuntimeConfig / BudgetProvider
  → TaskLoader (task_id, reward, raw task.json, answer.txt)
  → FeatureExtractor → CostEstimator → SolveabilityChecker → TaskPlanner
  → SolverRegistry (deterministic / coding / generic)
  → SolverExecutor
       ├── BudgetLedger.reserve() до вызова → OpenJiuwenGateway → Core SDK → proxy
       ├── BudgetLedger.settle() после ответа/ошибки
       └── проверка результата
  → AnswerStore (атомарный answer.txt)
  → RunReporter (stderr + /tmp)
```

Принципы: единый источник остатка; все LLM-вызовы через один шлюз; сначала
дешёвый путь; неизвестный тип не роняет процесс; проверка не изменяет задачу;
сохранение сразу; перепланирование после каждой задачи; параллелизм только после
теста Ledger.

Реестр — явные фабрики без плагинной системы: `build_estimator(config)`,
`build_policy(config)`, `build_solver(task)`, `build_llm_gateway(config)`.
Неизвестное имя — ошибка на старте; ошибка оценщика может дать fallback, ошибка
шлюза не переключает запрос на неразрешённый endpoint или локальную модель.

## Data Contracts

```python
@dataclass(frozen=True)
class Task:
    task_id: str
    path: str
    reward: float
    task_type: str | None
    raw: dict                 # исходный task.json без потери полей
    instruction: str | None
@dataclass(frozen=True)
class CostEstimate:
    expected_cost: float      # input/10 + output
    upper_cost: float         # эмпирический риск-ориентированный запас
    expected_input_tokens: float
    expected_output_tokens: float
    confidence: Literal["low", "medium", "high"]
    model: str
    solver: str
    drivers: tuple[str, ...]
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int        # включает reasoning, если так считает proxy
    reasoning_tokens: int     # диагностика, не всегда добавляется
    total_cost: float         # output + input / 10
    source_fields: tuple[str, ...]
@dataclass(frozen=True)
class Decision:
    action: Literal["accept", "reject_cost", "reject_impossible", "reject_time"]
    reason: str
    planned_cost: float
    expected_reward: float
@dataclass(frozen=True)
class SolveResult:
    task_id: str
    status: Literal["solved", "failed", "impossible", "budget_exhausted", "timeout"]
    answer: str | None
    actual_cost: float
    model: str | None
    attempts: int
    reason: str
```

`BudgetLedger` (Protocol):

```python
reserve(request_id, estimated_input, max_output, safety_margin=0.0) -> Reservation | None
settle(request_id, usage: TokenUsage) -> None
release_failed(request_id, *, definitely_not_sent: bool) -> None
remaining() -> float
```

Резерв не меньше `estimated_input/10 + max_total_output + margin`; проверка и
списание атомарны; недостаточный резерв блокирует вызов; излишек возвращается после
`settle`; потерянный ответ не бесплатен; счётчик не отрицателен; финальный учёт —
по прокси организаторов.

## Risks / Trade-offs

- **Источник бюджета неизвестен** → `BudgetProvider` и явная ошибка, вопрос
  организаторам в первый час, без выдуманного числа.
- **SDK несовместим с прокси** → smoke до planner; при блокере — запрос
  поддерживаемой конфигурации, а не скрытый клиент.
- **Права на bind mount** → тест с точной командой жюри и фактическим UID, без
  ослабления Docker-ограничений.
- **4 CPU** → консервативные лимиты, один worker, замер на 100 задачах.
- **Малый n (8 примеров)** → простые медианы, confidence, split по задаче; без
  статистических гарантий; утечка train/test исключена делением по `task_id`.
- **Нет 100 задач локально** → мок-задачи и fake proxy с известным usage; полный
  цикл проверяется без сети.
- **Срыв сроков** → P0 (SDK, Ledger, `answer.txt`, отборочный прогон) не
  обмениваются на новые теории; заморозка 12.11 18:00.
- **Перерасход из-за прогноза** → `planning_reserve` отдельно от резерва вызова и
  перепланирование после каждой задачи.

## Out of Scope

- MAPE как цель, конформные гарантии, per-task cap и любые индивидуальные лимиты
  задач; локальные нейросетевые модели, GPU, fine-tuning, внешние LLM endpoints.
- Сложный плагинный реестр, WorkSwarm UI, база данных, heartbeat, регистрация.
- T8 (self-estimate), T11 (bandit), T16 (probe), T17 (best-of-two), T18 (memory),
  нейросетевой оценщик, геометрическая модель попыток, conformal predictor — P1/P2
  без доказанного выигрыша на отложенных данных.
- `OUT`/`predictions.jsonl`/`trace.jsonl` как обязательные артефакты сдачи; правки
  `specs/**`, `team.json`, чужих change и файлов других владельцев.
