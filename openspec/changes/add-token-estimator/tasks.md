# Tasks

## 1. Базовая линия и реестр

- [ ] 1.1 T000 — Ветка `feat/token-estimator`, базовый прогон, фиксация исходного состояния. Файлы: `.gitignore`, `eval/results/baseline.json`. Проверка: `python3.11 -m unittest discover -s tests`. Критерий: 113 юнит-тестов зелёные (2 skip), офлайн 8/8 при `--mutation-repair`, score 20, базовые числа записаны.
- [ ] 1.2 T001 — Реестр реализаций и конфиг `OJ_*`. Файлы: `harness/registry.py`, `tests/test_registry.py`. Проверка: `python3.11 -m unittest tests.test_registry -v`. Критерий: оценщик, политика и солверы собираются по имени через `OJ_ESTIMATOR`/`OJ_POLICY`/`OJ_SOLVER`; неизвестное имя даёт понятную ошибку.

## 2. Оценщик

- [ ] 2.1 T002 — Оценщик v1: T1, T13, T19. Файлы: `harness/estimator.py`, `tests/test_estimator.py`, `eval/priors.py`. Проверка: `python3.11 -m unittest tests.test_estimator -v`. Критерий: прогноз на 8 примерах с интервалом, уверенностью и факторами; MAPE и смещение зафиксированы в отчёте; прогноз офлайн и ≤ 50 мс.
- [ ] 2.2 T008 — Калибровка T2, T3, T4, T5, T6, T8, T9. Файлы: `harness/calibration.json`, `eval/fit.py`, `tests/test_estimator.py`. Проверка: `python3.11 eval/fit.py --input eval/results/ground_truth.jsonl`. Критерий: `calibration.json` с версией, множителями, коэффициентами, конформными квантилями и приорами; обучение только на train; таблица MAPE по теориям и PICP@90 посчитаны.

## 3. Политика

- [ ] 3.1 T003 — Политика v1: T7, T10, T16. Файлы: `harness/policy.py`, `tests/test_policy.py`, `eval/probe.py`. Проверка: `python3.11 -m unittest tests.test_policy -v`. Критерий: 0 перерасходов в симуляциях 20k/50k/100k, у каждого вердикта (`accept`, `reject:cost`, `reject:infeasible`, `reject:time`) есть причина, ранний стоп не теряет больше 10% решаемости.

## 4. Солверы

- [ ] 4.1 T004 — Универсальный солвер с восстановлением JSON. Файлы: `harness/solvers/generic.py`, `tests/test_generic_solver.py`. Проверка: `python3.11 -m unittest tests.test_generic_solver -v`. Критерий: неизвестный `task_type` решается с fake-LLM одним вызовом; битый JSON или проза не роняют процесс; ответ пишется атомарно.
- [ ] 4.2 T005 — Патчи через поиск/замену: T14. Файлы: `harness/patch.py`, `tests/test_patch.py`. Проверка: `python3.11 -m unittest tests.test_patch -v`. Критерий: покрыты все уровни лестницы (точное → trim → отступы → кавычки → AST-якорь → сходство ≥ 0,92), промах возвращает `closest_window`, неоднозначное совпадение отклоняется.
- [ ] 4.3 T006 — Петля проверки и два варианта правки: T15, T17, T18. Файлы: `harness/solvers/coding.py`, `harness/memory.py`, `tests/test_coding.py`. Проверка: `python3.11 -m unittest tests.test_coding -v`. Критерий: тестовые файлы неизменны по SHA-256 и восстанавливаются, первая строка `FAILED` дословно попадает в следующий промпт, лимит 6 итераций соблюдён, `best_of_two` и память эпизодов выключаемы.

## 5. Оценка и второй полигон

- [ ] 5.1 T007 — Синтетика 40–60 задач и факты. Файлы: `eval/make_synthetic.py`, `eval/results/ground_truth.jsonl`. Проверка: `python3.11 eval/make_synthetic.py --n 50 --seed 42`. Критерий: 50 задач по 10 на тип с seed 42, у каждой есть проверка, известная стоимость после прогона и запись факта в `ground_truth.jsonl`.
- [ ] 5.2 T009 — Eval, метрики, симуляции, абляции T12. Файлы: `eval/run_eval.py`, `eval/metrics.py`, `eval/results/results.md`. Проверка: `python3.11 eval/run_eval.py --simulate 20000,50000,100000`. Критерий: сначала score при 20k/50k/100k, coverage и 0 перерасходов; затем диагностика MAPE/MdAPE/смещение/PICP@90/MPIW; таблица абляций и сгенерированный `results.md`.
- [ ] 5.3 T010 — Перенос 16–18 задач lorne и манифест. Файлы: `eval/tasks-lorne/**`, `eval/import_lorne.py`. Проверка: `python3.11 eval/import_lorne.py --src /home/eq/techlb/TCA/bench/tasks`. Критерий: 16–18 задач, у каждой `check` и манифест (`id`, `type`, `prompt`, `repo`, `check`, `bug_patch`, `gold_patch`, `expected_files`, `token_budget`); grep-проверки заменены структурными.

## 6. Упаковка

- [ ] 6.1 T011 — Docker, non-root, read-only прогон. Файлы: `Dockerfile`, `entrypoint.sh`, `scripts/build.sh`, `scripts/run_local.sh`. Проверка: `bash scripts/build.sh && bash scripts/run_local.sh`. Критерий: образ собирается, read-only прогон с `--network none` и `HOME=/tmp` пишет `answer.txt`, размер ≤ 4 ГБ и время сборки записаны.

## 7. openJiuwen

- [ ] 7.1 O1 — Доказательство использования openJiuwen SDK (L1). Файлы: `scripts/verify_openjiuwen.py`, `tests/test_openjiuwen_backend.py`, `eval/results/openjiuwen-proof.txt`. Проверка: `python3.11 scripts/verify_openjiuwen.py`. Критерий: подтверждены версия `0.1.20.post1`, ровно одна строка `LLM backend: openjiuwen` в stderr, `llm_backend` в summary, отсутствие секретов в логах; вывод сохранён.
- [ ] 7.2 O2 — Swarm-совместимая обёртка и harness-отчёт. Файлы: `jiuwenswarm-batch.sh`, `scripts/build.sh`, `eval/results/harness-report.json`. Проверка: `bash jiuwenswarm-batch.sh --tasks-dir examples/task-dir --offline`. Критерий: harness-отчёт содержит `JIUWENSWARM_*` или `jiuwenswarm` в дереве процессов, sha256 отчёта сохранён, порты не открываются.
- [ ] 7.3 O3 — Эксперимент DeepAgent на двух задачах. Файлы: `eval/deepagent_probe.py`, `eval/results/deepagent.md`. Проверка: `python3.11 eval/deepagent_probe.py --tasks 2 --budget 6000`. Критерий: есть сравнение cost и score с базой на тех же задачах; решение «подключить или отказаться» зафиксировано в `deepagent.md`.
- [ ] 7.4 O4 — Smoke-тест путей SDK: Model и WorkflowAgent+Runner. Файлы: `scripts/sdk_smoke.py`, `eval/results/sdk-path.md`. Проверка: `python3.11 scripts/sdk_smoke.py --model qwen3.6-35b-a3b-noreason`. Критерий: оба пути делают реальный вызов через прокси или фиксируют недоступность; для выбранного пути записаны latency, usage и решение; установка пакета без вызова доказательством не считается.

## 8. Документация и заморозка

- [ ] 8.1 T012 — README: методология, таблица теорий, ограничения. Файлы: `README.md`. Проверка: `python3.11 -c "import pathlib;assert 'MAPE' in pathlib.Path('README.md').read_text()"`. Критерий: есть методология, измеренные числа, команды воспроизведения, таблица T1–T19 с результатами и отрицательные результаты.
- [ ] 8.2 T013 — Заморозка, тег `v1.0`, финальная матрица. Файлы: правок кода нет; артефакты — тег `v1.0`, `eval/results/final.md`. Проверка: `python3.11 eval/run_eval.py --final`. Критерий: матрица раздела 13 зелёная, тег `v1.0` создан, `predictions.jsonl` и `trace.jsonl` приложены, `team.json` и секреты не закоммичены.

## 9. Расширенный набор задач

- [x] 9.1 T014 — Интеграция расширенного набора из 16 задач и фикс негативного упоминания сети. Файлы: `examples/task-dir-extended/**`, `examples/task-dir-extended/README.md`, `harness/triage.py`, `tests/test_triage.py`, `README.md`. Проверка: `python3.11 -m unittest tests.test_triage -v` и `python3.11 run_agent.py --tasks-dir examples/task-dir-extended --dry-run --offline`. Критерий: все 16 задач загружаются, ни одна не пропущена из-за фразы «no internet», замеренный результат офлайн-прогона записан в README, базовые 8 задач не изменены.
