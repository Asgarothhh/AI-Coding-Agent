# Design

## Context

См. `proposal.md`. Фактический код на локальном HEAD `60febbd`:

- `run_agent.py:105` — `--llm-backend` default `auto`; `:122` — `--model-policy`
  default `metrics`; `:197` — `default_model = args.model or $LLM_MODEL or DEFAULT_MODEL`.
- `harness/llm.py:40` — `DEFAULT_MODEL = "qwen3.6-35b-a3b-noreason"`.
- `harness/model_router.py` — при `fixed` возвращает модель verbatim, при `metrics`
  выбирает winner по `score/cost` из `harness/model_stats.json`.
- `Dockerfile:84` — `CMD ["python", "/app/run_agent.py", "--tasks-dir", "/data/tasks"]`,
  без `--llm-backend` и `--model-policy`.
- `harness/budget.py:147-200` уже читает `BUDGET` (после `OJ_TOKEN_BUDGET`).
- `harness/runner.py:369-372` включает gateway-accounting только для backend
  `openjiuwen-gateway`.

То есть требуемое поведение почти всё поддержано кодом, но официальный entrypoint
его не включает. Change сводится к смене default-ов и документации, без
архитектурных правок.

## Goals / Non-Goals

**Goals:**
- Официальный запуск (argparse default + Docker `CMD`) идёт через
  `OpenJiuwenGateway` с fixed-политикой и базовой моделью.
- `LLM_MODEL` побеждает встроенный default, но не `--model`.
- Команда в spec §15.3 и README содержит `-e BUDGET`; сроки и email сдачи
  зафиксированы в документации.
- Не регрессировать: 392 unit + 47 contract, offline 8/8 (20), Docker build.

**Non-Goals:**
- Изменение ledger/reserve/settle (B5/B7/B9/B11) — отдельный change D1.
- Правки solvers, coding repair, data_analysis — отдельные дефекты.
- Обновление `openjiuwen` до latest (`0.1.20.post3`) — P2, нужен live-прогон.
- Повторная регистрация, harness-отчёт, подделка env/process tree.
- Коммиты и push: change только фиксирует требования и задачи.

## Decisions

### 1. Default backend — `gateway`, а не `auto`
`auto` при установленном `openjiuwen` возвращает `OpenJiuwenLLMClient` и обходит
reserve/settle gateway. Официальная команда не должна зависеть от того, какой пакет
случайно импортировался.

**Почему:** без gateway бюджет резервируется advisory `check()` + post-hoc
`charge()`, при 4 воркерах возможен перерасход; жюри оценивает именно официальный
запуск.

**Альтернатива:** оставить `auto` и добавить reserve/settle в `OpenJiuwenLLMClient` —
больший blast radius и другой владелец (D1), вне scope этого change.

### 2. Default model policy — `fixed`
Жюри запускает оценку с фиксированной моделью. `metrics` выбирает модель по
`model_stats.json` и игнорирует `LLM_MODEL`/`--model`, то есть официальный прогон
пойдёт не на той модели, что заявлена.

**Почему:** соответствие условию оценки важнее внутреннего выбора «лучшей по
score/cost» модели.

**Альтернатива:** удалить `metrics` — ломает внутренние эксперименты и тесты;
`metrics` остаётся доступным явным флагом.

### 3. `DEFAULT_MODEL` — базовый alias `qwen3.6-35b-a3b`
Организатор 28.10 перечислил две модели без суффикса `-noreason`. Базовый alias —
наиболее консервативный выбор, который точно входит в оба возможных списка.

**Почему:** `-noreason` может оказаться вне разрешённого набора; при
`--model-policy fixed` встроенный default становится фактической моделью запуска.

**Альтернатива:** оставить `qwen3.6-35b-a3b-noreason` — риск нарушения при
проверке списка моделей. Если организаторы подтвердят четыре модели, alias
меняется обратно одним значением.

### 4. `LLM_MODEL` приоритетнее встроенного default, но не `--model`
Порядок: `--model` → `LLM_MODEL` → `DEFAULT_MODEL`. Это позволяет жюри или
оператору переопределить модель окружением, не меняя образ.

**Почему:** текущий `run_agent.py:197` уже так работает; фиксируем поведение
тестом, чтобы `metrics` не перехватывал выбор.

**Альтернатива:** заставить `LLM_MODEL` побеждать `--model` — неожиданно для CLI и
ломает существующие тесты.

### 5. Docker `CMD` дублирует default-ы явно
`CMD` получает `--llm-backend gateway --model-policy fixed`. Даже если argparse
default когда-нибудь изменится, официальный образ сохранит поведение.

**Почему:** образ — официальный entrypoint; неявные default-ы здесь слишком дороги.

**Альтернатива:** полагаться только на argparse — одна правка default-а меняет
поведение образа молча.

### 6. Не менять `openjiuwen` post1 в этом change
Сообщение «версия берётся последняя» неоднозначно; локальная спека и lock
фиксируют `0.1.20.post1`, live-прогон на `post3` не выполнен.

**Почему:** смена версии без live-прогона — риск для уже проверенных 392+47 и
gateway-пути.

**Альтернатива:** обновить lock сейчас — требует пересборки, live и повторной
проверки, не влезает в окно до 23:59.

### 7. Ledger и solvers не трогаем
B5/B7/B9/B11 и дефекты coding/data_analysis задокументированы в
`eval/results/audit-2026-11-13.md`, но относятся к D1/D3 и не входят в цель
«официальный entrypoint соответствует объявленным условиям».

**Почему:** один change — одна цель; смешивание правок ledger и default-ов
затрудняет откат.

### 8. Документация — часть change
spec §15.3 и README содержат команду без `-e BUDGET` и старые сроки. Без правки
документации жюри воспроизведёт команду, которая даёт `exit 2`.

## Risks / Trade-offs

- [fixed-модель хуже metrics по score/cost] → это осознанно: соответствие условию
  оценки приоритетнее; `metrics` доступен флагом для внутренних прогонов.
- [Базовый alias без `-noreason` даёт другой расход/качество] → измерить на live
  smoke; при подтверждении четырёх моделей вернуть `-noreason` точечно.
- [gateway default ломает offline/тесты без ключей] → `--offline` и `stdlib`
  остаются явными путями; тесты используют FakeLLM/mock gateway.
- [`LLM_MODEL` в окружении жюри неожиданно меняет модель] → это и есть требуемый
  приоритет; значение логируется в plan/dry-run.
- [Docker `CMD` и argparse разъедутся] → тест сравнивает argv `CMD` с ожидаемыми
  флагами.
- [Правка README/spec конфликтует с другими лейнами] → изменения минимальные, по
  согласованию с владельцами файлов; код-лейн подтверждает статус в отчёте.

## Migration Plan

1. `harness/llm.py` — `DEFAULT_MODEL`; `run_agent.py` — default backend/policy.
2. `Dockerfile` `CMD` — явные флаги.
3. Тесты default-ов и приоритета `LLM_MODEL`; прогон 392+47.
4. Документация: spec §15.3, README, сроки/email.
5. Проверка offline 8/8 (20) и Docker build; статус в
   `eval/results/audit-2026-11-13.md`.

Откат: revert перечисленных файлов; ledger, solvers и `openjiuwen` lock не
затронуты, поэтому откат не требует пересборки зависимостей.

## Open Questions

- Разрешены две модели или четыре (с `-noreason`)? До ответа используется базовый
  alias.
- Передаст ли жюри `-e BUDGET` и в каком размере? Код читает `BUDGET`, дефолт
  100000 — только для `--offline`.
- Достаточно ли Core SDK Model как клиента, или главный цикл должен быть на
  агентах/workflow openJiuwen? Вопрос задан 11.11 19:25, ответа нет; harness-отчёт
  ждёт `JIUWENSWARM_*`/`jiuwenswarm` в process tree, а `run_agent.py` запускается
  напрямую.
