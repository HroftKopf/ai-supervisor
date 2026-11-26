# 01. Архитектура проекта

## Концепция

AI Supervisor разделяет **принятие решений**, **исполнение** и **безопасность**. LLM не получает
прямой доступ к файлам: она формирует ТЗ/ревью/ответ, а Python проверяет переходы, права,
sandbox и Git.

## Реализация

```text
Telegram -> Handlers -> Orchestrator -> Supervisor (control LLM)
                               |-> WorkerManager -> bwrap -> DeepSeek Harness
                               |-> Workspace/GitAuthManager/GitHub
                               |-> SQLite (tasks/messages/stages/usage/approvals)
                               |-> Presentation Layer -> Telegram
```

Ключевые пути:

- `src/ai_supervisor/telegram/handlers.py` — команды и callbacks;
- `src/ai_supervisor/core/orchestrator.py` — жизненный цикл задачи;
- `src/ai_supervisor/agents/supervisor.py` — роли/структурированные вызовы;
- `src/ai_supervisor/providers/` — Kimi/OpenAI/совместимые провайдеры;
- `src/ai_supervisor/core/sandbox.py` и `worker_manager.py` — изоляция;
- `src/ai_supervisor/memory/` — SQLite, задачи, этапы, память;
- `src/ai_supervisor/presentation/` — карточки и форматирование.

## Шаги запроса

1. aiogram получает update.
2. `OwnerMiddleware` проверяет numeric owner/private chat.
3. Handler вызывает `Orchestrator`.
4. Классификатор решает chat/task.
5. Planner создаёт `TaskSpec`.
6. Worker исполняет в bwrap/worktree.
7. Validator/reviewer проверяют evidence и критерии.
8. Approval, если нужен.
9. Presentation Layer показывает результат.

## Альтернативы

- Единый LLM-агент с доступом к shell: проще, но небезопасно.
- Микросервисы: изоляция лучше, но операционная сложность выше.

## Вопросы

1. Почему LLM не вызывает `git push` напрямую?
2. Где находится граница между планом и разрешением?
3. Какой компонент является источником истины состояния задачи?

## Проверь себя

Найди в `Orchestrator` путь от `handle_incoming` до `WorkerManager.run` и назови каждый
компонент, который может отклонить действие.
