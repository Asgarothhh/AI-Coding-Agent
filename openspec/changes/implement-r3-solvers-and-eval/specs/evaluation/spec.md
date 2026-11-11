# Spec Delta: evaluation (R3)

## Purpose
Воспроизводимая оценка solvers на official smoke, lorne subset и синтетическом mock без подмены missing usage нулём и без выдачи синтетики за факты стоимости.

## ADDED Requirements

### Requirement: Official smoke отделён от синтетики
Восемь официальных примеров MUST проверяться как smoke. Синтетические сценарии MUST помечаться `synthetic: true` и MUST NOT называться официальными задачами.

#### Scenario: Official examples
- **WHEN** запускается offline smoke `examples/task-dir`
- **THEN** отчёт содержит фактические solved/failed/score и команду воспроизведения

#### Scenario: Синтетический набор
- **WHEN** генерируется mock из 100 задач
- **THEN** манифест содержит `synthetic: true` и фиксированный seed

### Requirement: Lorne subset воспроизводим без исходного TCA runtime
Подмножество lorne MUST иметь стабильный id, источник, тип и исполняемый независимый checker. Отсутствующий исходный путь TCA MUST NOT блокировать offline-проверку уже сконвертированных fixtures.

#### Scenario: Загрузка lorne
- **WHEN** вызывается загрузка `eval/external/lorne`
- **THEN** число задач равно фактическому составу каталога и каждое fixture имеет checker

### Requirement: Missing usage остаётся неизвестным
При оценке стоимости система MUST использовать только фактический usage. Отсутствующий usage MUST оставаться null/unknown и MUST NOT подменяться нулём.

#### Scenario: Нет usage
- **WHEN** в прогоне нет proxy usage для задачи
- **THEN** отчёт помечает расход как unknown/null

### Requirement: Сто задач на fake gateway
Интеграционный mock MUST прогонять 100 локальных задач через тот же интерфейс Gateway/Ledger, что и runtime, без живого proxy. Каждый каталог MUST получить `answer.txt`. Неуспешные задачи MUST NOT входить в solved. Сумма `spent + reserved` MUST NOT превышать общий бюджет.

#### Scenario: Повторный прогон того же seed
- **WHEN** mock запускается дважды с одним seed
- **THEN** решения и итоговые метрики совпадают

#### Scenario: Исчерпание бюджета
- **WHEN** резерв на следующий вызов не проходит
- **THEN** новый LLM-вызов не стартует, уже записанные ответы остаются целыми
