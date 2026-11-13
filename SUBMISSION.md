# Submission — openJiuwen Challenge 2026

**Official deadline: 13.11.2026 23:59 MSK.**  
**Send to: `openjiuwen_org@nsu.ru`** — reply to the email with the key, or send a
separate email. The repository may be **public or private**.

The email has **not** been sent yet. Do not claim otherwise.

## Checklist

- [ ] Final commit is pushed and recorded (`git rev-parse HEAD`).
- [ ] Repository URL is final; public, or private with organizer access.
- [ ] Tests: baseline `60febbd` = 392 unit + 47 contract. The compliance fix
      adds tests, so the final count may be 393+48 or other — paste the actual
      output of the final verification run.
- [ ] Offline `examples/task-dir` with `--mutation-repair`: 8/8, score 20,
      0 tokens.
- [ ] Extended offline set with `--mutation-repair`: 14/16, score 46 (two known
      coding failures).
- [ ] Docker image builds and is under 4 GB.
- [ ] Official command passes `-e BUDGET` (the official budget variable may
      differ from 100k).
- [ ] No secrets in the tree or history.
- [ ] Email sent before **13.11.2026 23:59 MSK**.

## Email template

Replace `<FINAL_COMMIT>` **after** the final push. Do not send the template with
the placeholder still in it.

```text
Кому: openjiuwen_org@nsu.ru
Тема: Решение команды «Стивенсон» — openJiuwen Challenge 2026

Здравствуйте!

Направляем решение команды «Стивенсон» для openJiuwen Challenge 2026.

Команда: Стивенсон
Registration ID: 5555bec9277725f88092706dc0ae47d7
Репозиторий: https://github.com/Asgarothhh/AI-Coding-Agent
Commit: <FINAL_COMMIT>

Репозиторий: публичный (доступен без дополнительных приглашений).
Точка входа: python /app/run_agent.py --tasks-dir /data/tasks
Бюджет передаётся через переменную окружения BUDGET; LLM_BASE_URL и
LLM_API_KEY — через окружение. Модель по умолчанию — qwen3.6-35b-a3b,
политика fixed.

С уважением,
команда «Стивенсон»
```
