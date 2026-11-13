# Proposal

## Why

Сообщения организаторов 12–13.11.2026 уточнили условия официального прогона, и
текущие default-ы им не соответствуют:

- бюджет приходит в переменной `BUDGET` и может отличаться от 100k;
- запуск оценки идёт с **фиксированной** моделью, а default `--model-policy metrics`
  выбирает модель из `harness/model_stats.json` и игнорирует `LLM_MODEL`;
- официальный дедлайн — 13.11.2026 23:59 MSK, сдача — письмо на
  `openjiuwen_org@nsu.ru`, репозиторий может быть публичным или приватным;
- организатор 28.10 назвал две модели (`qwen3.8-27b`, `qwen3.6-35b-a3b`), локальная
  спека — четыре (с `-noreason`).

Пока официальный entrypoint (`run_agent.py` + Docker `CMD`) не использует gateway,
fixed-политику и базовый alias, жюри может запустить другой транспорт, другую модель
и получить расхождение с заявленным поведением. Официальная команда из spec §15.3
без `-e BUDGET` даёт `exit 2` и 0 ответов.

## What Changes

- `run_agent.py`: default `--llm-backend gateway` (вместо `auto`) и default
  `--model-policy fixed`; `LLM_MODEL` имеет приоритет над встроенным default-ом.
- `harness/llm.py`: `DEFAULT_MODEL` — базовый alias `qwen3.6-35b-a3b` без
  `-noreason`.
- `Dockerfile` `CMD`: явно передаёт `--llm-backend gateway --model-policy fixed`,
  чтобы официальный запуск не зависел от argparse default.
- Тесты: default-ы, приоритет `LLM_MODEL`, allowlist для fixed-модели, официальная
  команда с `-e BUDGET`.
- Документация: spec §15.3 / README — `-e BUDGET`, fixed policy, базовый alias,
  дедлайн 13.11.2026 23:59 MSK и email сдачи.

Out of scope: смена `openjiuwen==0.1.20.post1` на latest; правки `harness/budget.py`
и ledger; B5/B7/B9/B11; solvers, coding/data_analysis дефекты; повторная
регистрация; любые коммиты и push; архив change `add-token-estimator`.

## Capabilities

### New Capabilities

- `runtime-defaults`: официальные default-ы runtime — LLM backend, модель, политика
  выбора модели, приоритет `LLM_MODEL`, передача `BUDGET` и параметры сдачи.

### Modified Capabilities

(пусто: `openspec/specs/` пока пуст, требования фиксируются этим change)

## Impact

- `run_agent.py` (argparse default-ы, приоритет `LLM_MODEL`)
- `harness/llm.py` (`DEFAULT_MODEL`)
- `Dockerfile` (`CMD`)
- `tests/test_llm.py`, `tests/test_model_router.py`, `tests/test_cli.py`,
  `tests/contract/**` (default-ы и приоритет модели)
- `specs/001-token-estimator/spec.md` §15.3, `README.md` (команда и сроки)
- `eval/results/audit-2026-11-13.md` (статус исправлений)
