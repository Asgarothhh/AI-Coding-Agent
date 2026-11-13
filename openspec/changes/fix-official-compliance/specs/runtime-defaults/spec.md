# Spec Delta: runtime-defaults

## Purpose
Официальный entrypoint должен по умолчанию использовать gateway, фиксированную политику выбора модели и базовый alias, соблюдать приоритет `LLM_MODEL`, читать `BUDGET` и документировать подтверждённые условия сдачи.

## ADDED Requirements

### Requirement: Default LLM backend — gateway
Официальный запуск `run_agent.py` MUST по умолчанию использовать backend `gateway` (`OpenJiuwenGateway`), а не `auto`. Offline-режим и `stdlib` MUST оставаться явно выбираемыми и MUST NOT включаться неявно в официальном прогоне.

#### Scenario: Запуск без флага backend
- **WHEN** `run_agent.py` запускается без `--llm-backend` и без `--offline`
- **THEN** выбранный backend — `gateway`, и бюджетный учёт идёт через reserve/settle gateway

#### Scenario: Явный offline
- **WHEN** передан `--offline`
- **THEN** сетевые LLM-вызовы отключены, backend не делает запросов к шлюзу

### Requirement: Default model policy — fixed
При официальном запуске политика выбора модели MUST быть `fixed`. Политика `metrics` MUST NOT применяться по умолчанию и MUST NOT переопределять модель, заданную `--model` или `LLM_MODEL`.

#### Scenario: Есть model_stats.json
- **WHEN** в образе присутствует `harness/model_stats.json` и флаги политики не переданы
- **THEN** используется `fixed`-модель, а не winner из `metrics`

#### Scenario: Явная metrics-политика
- **WHEN** оператор явно передал `--model-policy metrics`
- **THEN** выбор по измеренной статистике разрешён в пределах allowlist

### Requirement: Явный LLM_MODEL имеет приоритет
Если задан `LLM_MODEL`, система MUST использовать его значение как модель по умолчанию. Приоритет MUST быть: `--model`, затем `LLM_MODEL`, затем встроенный `DEFAULT_MODEL`.

#### Scenario: LLM_MODEL задан
- **WHEN** `LLM_MODEL=qwen3.6-35b-a3b` и `--model` не передан
- **THEN** plan и фактический вызов используют `qwen3.6-35b-a3b`

#### Scenario: --model сильнее окружения
- **WHEN** заданы и `--model`, и `LLM_MODEL`
- **THEN** используется значение `--model`

### Requirement: Default model — базовый alias
Встроенный `DEFAULT_MODEL` MUST быть базовым alias `qwen3.6-35b-a3b` без суффикса `-noreason`, пока организаторы не подтвердят расширенный список моделей.

#### Scenario: Окружение не задаёт модель
- **WHEN** ни `--model`, ни `LLM_MODEL` не заданы
- **THEN** используется `qwen3.6-35b-a3b`

### Requirement: Официальный BUDGET
Официальная команда MUST передавать бюджет через переменную окружения `BUDGET`. Значение бюджета MUST NOT быть зашито в код или образ, поскольку оно может отличаться от 100k токенов. Отсутствие бюджета вне offline MUST приводить к явной диагностике, а не к молчаливому дефолту.

#### Scenario: Бюджет передан
- **WHEN** контейнер запускается с `-e BUDGET=<значение>`
- **THEN** `BudgetProvider` использует это значение как общий бюджет

#### Scenario: Бюджет не передан
- **WHEN** `BUDGET`/`OJ_TOKEN_BUDGET` не заданы и `--offline` не указан
- **THEN** процесс завершается с диагностикой и не начинает решение задач

### Requirement: Документированные условия сдачи
Документация MUST содержать официальный дедлайн 13.11.2026 23:59 MSK, email сдачи `openjiuwen_org@nsu.ru` и допустимость публичного или приватного репозитория. Документация MUST NOT называть канал сдачи неподтверждённым.

#### Scenario: Проверка README и spec
- **WHEN** читается официальная команда и раздел о сдаче
- **THEN** указаны `-e BUDGET`, fixed policy, базовый alias, дедлайн 23:59 MSK и email сдачи
