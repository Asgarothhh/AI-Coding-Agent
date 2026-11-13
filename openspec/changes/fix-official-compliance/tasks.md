# Tasks

## 0. Gate 0 — контекст (Integrator)

- [ ] 0.1 Integrator — Зафиксировать change `fix-official-compliance` и официальные условия 12–13.11. Файлы: `openspec/changes/fix-official-compliance/**`. Проверка: `npx --yes @fission-ai/openspec@1.17.0 validate fix-official-compliance --strict`. Критерий: strict OK; `add-token-estimator` не изменён.
- [ ] 0.2 Integrator — Сверить текущие default-ы с кодом на HEAD `60febbd`. Файлы: `eval/results/audit-2026-11-13.md`. Проверка: `grep -n "llm-backend\|model-policy\|DEFAULT_MODEL" run_agent.py harness/llm.py Dockerfile`. Критерий: в отчёте зафиксированы фактические значения до правки.

## 1. Gate 1 — официальные default-ы runtime (D1)

- [ ] 1.1 D1 — `--llm-backend` default `gateway`, `--model-policy` default `fixed`; приоритет `--model` → `LLM_MODEL` → `DEFAULT_MODEL`. Файлы: `run_agent.py`. Проверка: `python3.11 run_agent.py --tasks-dir examples/task-dir --dry-run`. Критерий: plan показывает `backend=gateway`, `policy=fixed`, модель из `LLM_MODEL`, если она задана.
- [ ] 1.2 D1 — `DEFAULT_MODEL` = базовый alias `qwen3.6-35b-a3b` (без `-noreason`). Файлы: `harness/llm.py`. Проверка: `LLM_MODEL= python3.11 -c "from harness.llm import DEFAULT_MODEL; print(DEFAULT_MODEL)"`. Критерий: вывод `qwen3.6-35b-a3b`; `ESCALATION_MODEL` не меняется без согласования.
- [ ] 1.3 D1 — Docker `CMD` явно передаёт `--llm-backend gateway --model-policy fixed`. Файлы: `Dockerfile`. Проверка: `docker run --rm --entrypoint cat hack:latest /app/run_agent.py >/dev/null; docker inspect -f '{{json .Config.Cmd}}' hack:latest` либо `grep -n CMD Dockerfile`. Критерий: оба флага присутствуют в `Cmd`, `--tasks-dir /data/tasks` сохранён.

## 2. Gate 2 — тесты официального контракта (D1/D3)

- [ ] 2.1 D1 — Тесты default-ов и приоритета `LLM_MODEL`. Файлы: `tests/test_llm.py`, `tests/test_model_router.py`, `tests/test_cli.py`. Проверка: `python3.11 -m unittest tests.test_llm tests.test_model_router tests.test_cli -v`. Критерий: default backend/policy и порядок приоритета зафиксированы; `--model` по-прежнему сильнее `LLM_MODEL`.
- [ ] 2.2 D1/D3 — Fixed-политика не принимает модель вне allowlist на старте. Файлы: `tests/test_model_router.py`, `run_agent.py`. Проверка: `python3.11 -m unittest tests.test_model_router -v`. Критерий: неизвестная модель отклоняется до первого LLM-вызова с понятной диагностикой.
- [ ] 2.3 D3 — Официальная команда с `-e BUDGET` не завершается `exit 2`. Файлы: `tests/contract/**`. Проверка: `python3.11 -m unittest discover -s tests/contract`. Критерий: контракт фиксирует наличие `BUDGET` в документированной команде; размер бюджета не зашит.

## 3. Gate 3 — документация (Integrator)

- [ ] 3.1 Integrator — spec §15.3: `-e BUDGET`, `--llm-backend gateway`, `--model-policy fixed`, базовый alias; бюджет может отличаться от 100k. Файлы: `specs/001-token-estimator/spec.md`. Проверка: `grep -n "BUDGET\|model-policy\|llm-backend" specs/001-token-estimator/spec.md`. Критерий: официальная команда воспроизводима и не даёт `exit 2`.
- [ ] 3.2 Integrator — README: официальная команда, fixed policy, базовый alias, дедлайн 13.11.2026 23:59 MSK, email сдачи `openjiuwen_org@nsu.ru`. Файлы: `README.md`. Проверка: `grep -n "23:59\|openjiuwen_org@nsu.ru\|model-policy" README.md`. Критерий: нет устаревших «18:00» как официального дедлайна и «канал не подтверждён».

## 4. Gate 4 — верификация (D1/Integrator)

- [ ] 4.1 D1 — Unit и contract без регрессии. Проверка: `python3.11 -m unittest discover -s tests && python3.11 -m unittest discover -s tests/contract`. Критерий: 392 unit + 47 contract, exit 0; числа из фактического вывода.
- [ ] 4.2 D1 — Offline smoke. Проверка: `python3.11 run_agent.py --tasks-dir examples/task-dir --offline --max-workers 1` и `--tasks-dir examples/task-dir-extended`. Критерий: 8/8 score 20 и 16 задач без регрессии; сетевых вызовов нет.
- [ ] 4.3 D1/Integrator — Docker build и запуск с `-e BUDGET`. Проверка: `docker build -t hack:latest .` + официальная команда из spec §15.3 на локальной копии задач. Критерий: образ < 4 ГБ, build < 1000 s, процесс не падает на бюджете; read-only rootfs сохранён.

## 5. Итог

- [ ] 5.1 Integrator — Обновить `eval/results/audit-2026-11-13.md` §10 фактическими статусами. Проверка: таблица §10 содержит `сделано` только для подтверждённых код-лейном пунктов. Критерий: нет заявленных без прогона результатов.

## Workflow follow-up

- Код-лейн подтверждает каждый пункт выводом команды; до этого статус — «в работе».
- Не менять ledger/reserve/settle, solvers и lock `openjiuwen` в этом change.
- Коммиты и push — только по явной просьбе владельца репозитория.
- Архив change — после подтверждения §10 и `validate --strict`.
