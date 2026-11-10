# openJiuwen: где именно используется и как это доказать

**Связанный документ:** `spec.md` (спека 001)
**Версия:** 1.0 · 10 ноября 2026
**Пакет:** `openjiuwen==0.1.20.post1`
**Задачи:** O1–O3 в `openjiuwen.tasks.yaml`

---

## 1. Зачем этот документ

Конкурс требует использовать openJiuwen. Недостаточно написать `import openjiuwen`: нужно показать, какой слой используется, где он вызывается, что произойдёт без него и чем это подтверждается. Здесь собраны ответы на эти вопросы и план доказательств.

---

## 2. Три уровня интеграции

| Уровень | Что это | Статус | Где в коде |
|---|---|---|---|
| L1. Модельный слой | `Model`, `ModelClientConfig`, `ModelRequestConfig` из `openjiuwen.core.foundation.llm` | **работает**, выбран по умолчанию при `--llm-backend auto` | `harness/ojiuwen_llm.py`, `harness/llm.py` |
| L2. DeepAgent | `create_deep_agent`, `AgentCard`, `create_agent_session`, rails, `MaxRoundsEvaluator`, `TimeoutEvaluator` | реализован, **не подключён** к раннеру | `harness/ojiuwen_runtime.py` |
| L3. Приложение JiuwenSwarm / WorkSwarm | web UI, конфиг `~/.jiuwenswarm`, harness-отчёт | используется только для регистрации и отчёта | `~/jiuwenswarm-prep` |

Для конкурсного batch-агента основной является L1. L2 — эксперимент, L3 — требование регистрации, а не runtime.

---

## 3. Что именно вызывается на L1

1. **Ленивый импорт.** SDK импортируется только при создании первого клиента: `harness/ojiuwen_llm.py:214-220`. Без пакета харнесс и тесты работают на stdlib.
2. **Сборка клиента.**
   - `ModelClientConfig(client_provider="OpenAI", api_base=..., api_key=..., verify_ssl=...)`;
   - `ModelRequestConfig(model_name=..., temperature=..., max_tokens=...)`;
   - `api_base` передаётся в SDK как есть; SDK не добавляет `/v1`.
3. **Постоянный event loop.** Один фоновый цикл на клиента (`harness/ojiuwen_llm.py:408`), каждый `invoke` уходит через `asyncio.run_coroutine_threadsafe`. Поэтому параллельные воркеры безопасны.
4. **Учёт usage.** `usage_metadata` переводится в `prompt_tokens = input_tokens`, `completion_tokens = output_tokens`; если `output_tokens == 0`, а `reasoning_tokens > 0`, reasoning становится completion. Двойного счёта нет.
5. **Логи.** `configure_log_config` из `openjiuwen.core.common.logging.log_config` вызывается до первого `Model` (`harness/ojiuwen_llm.py:117-201`): файлы в `/tmp/openjiuwen-logs`, уровень WARNING, stdout не засоряется, WARNING/ERROR дублируются в stderr.
6. **Выбор бэкенда.** `build_llm_client` (`harness/llm.py:290-328`):
   - `auto` — openJiuwen, если `importlib.util.find_spec("openjiuwen")` успешен, иначе stdlib;
   - `openjiuwen` — только SDK, иначе `LLMError` с понятным сообщением;
   - `stdlib` — всегда urllib-клиент.
7. **Отчётность.** `run_agent.py:138-139` запоминает фактический бэкенд, `run_agent.py:169-170` кладёт в итоговый JSON поля `llm_backend` и `llm_backend_requested`.

---

## 4. Как доказываем использование

Чек-лист, который должен быть выполнен на машине команды и приложен к отчёту:

1. В образе установлен пакет: `Dockerfile:29-30` ставит `requirements.txt:3`, где закреплён `openjiuwen==0.1.20.post1`.
2. Версия подтверждена:
   `python -c "import openjiuwen; print(openjiuwen.__version__)"` → `0.1.20.post1`.
3. Явный прогон: `python run_agent.py --tasks-dir ... --llm-backend openjiuwen --max-workers 1`.
4. В stderr ровно одна строка `LLM backend: openjiuwen`; секретов в логах нет.
5. В итоговом JSON: `"llm_backend": "openjiuwen"`, `"llm_backend_requested": "openjiuwen"`.
6. В `trace.jsonl` и `predictions.jsonl` есть версия модели и потраченные токены.
7. `collect_harness_report.py`, запущенный из-под JiuwenSwarm, содержит `JIUWENSWARM_*` или `jiuwenswarm` в дереве процессов.
8. В README есть раздел про интеграцию и команды воспроизведения.

Заявленные в README результаты живого прогона (8/8, 454,3 единицы при одном воркере, 1162,7 при четырёх) **требуют повторного подтверждения на ПК команды**: на текущей машине SDK не установлен и ключа нет. До подтверждения это гипотеза, а не факт.

---

## 5. Если организаторы требуют именно приложение Swarm

1. Оставляем batch-раннер основным: он решает задачи, а не поднимает web UI.
2. Добавляем обёртку `jiuwenswarm-batch.sh`: экспортирует `JIUWENSWARM_*`, запускает `run_agent.py`, не открывает порты.
3. Проверяем, что `collect_harness_report.py` видит нужные переменные и процесс.
4. Пакеты проверяем отдельно: `workswarm 0.2.8` и `jiuwenswarm 0.2.3` — разные проекты. Выбираем тот, который реально поднимается и даёт `JIUWENSWARM_*`; выбор фиксируем в отчёте.
5. Если требуют web UI — поднимаем его только в отдельном контейнере и только по явному требованию. В финальный образ UI не тащим.

---

## 6. Чего делать нельзя

- выдавать stdlib-клиент за openJiuwen;
- подделывать `JIUWENSWARM_*` и дерево процессов;
- логировать API-ключ или писать его в репозиторий;
- писать логи в `/app/logs` при read-only rootfs;
- тянуть сетевые вызовы в прогноз стоимости.

---

## 7. Задачи

| ID | Что делаем | Лейн | Зависит | Готово, когда |
|---|---|---|---|---|
| O1 | Скрипт и тест доказательства L1 | B | T000 | пункты 1–6 раздела 4 выполнены, вывод сохранён в `eval/results/openjiuwen-proof.txt` |
| O2 | Swarm-совместимая обёртка и harness-отчёт | A | O1 | пункт 7 раздела 4 выполнен, отчёт и sha256 сохранены |
| O3 | Эксперимент DeepAgent на двух задачах | C | O1 | есть сравнение cost/score с базой; решение «подключать или нет» зафиксировано |

O1 обязательна. O2 выполняется, если организаторы подтверждают требование Swarm или если остаётся время. O3 — эксперимент: при отсутствии выигрыша фиксируем отказ и не подключаем DeepAgent к раннеру.

---

## 8. Риски

| Риск | Удар | Митигация |
|---|---|---|
| Организаторы считают L1 недостаточным | высокий | O2: обёртка и harness-отчёт; решение по ответу организаторов |
| `workswarm` и `jiuwenswarm` путаются | средний | оба в отдельных venv, выбор по факту работы |
| SDK недоступен в финальном окружении | высокий | fallback stdlib уже реализован; в отчёте явно указан фактический бэкенд |
| Логи SDK пишут в read-only каталог | средний | `configure_log_config` уже направляет их в `/tmp/openjiuwen-logs` |
| DeepAgent съедает бюджет | средний | O3 с жёстким лимитом на 2 задачи и сравнением с базой |

---

## 9. Чек-лист перед сдачей

- [ ] `openjiuwen==0.1.20.post1` установлен в образе.
- [ ] Версия напечатана и сохранена.
- [ ] Прогон с `--llm-backend openjiuwen` успешен.
- [ ] В stderr одна строка `LLM backend: openjiuwen`.
- [ ] В JSON `llm_backend=openjiuwen`.
- [ ] Harness-отчёт содержит `JIUWENSWARM_*` или процесс `jiuwenswarm`.
- [ ] README описывает интеграцию и команды.
- [ ] Ключи и `team.json` не закоммичены.
