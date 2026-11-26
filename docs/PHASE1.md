# Phase 1 — что реализовано

## Компоненты

| Компонент | Файлы | Состояние |
|---|---|---|
| Telegram long polling, owner auth, dedupe | `telegram/` | реализовано, live не проверено |
| Kimi provider | `providers/kimi.py`, `agents/supervisor.py` | реализовано, live chat/classify/plan/review проверены |
| DeepSeek Harness SDK | `worker_runner.py`, `core/worker_manager.py` | реализовано, проверено с mock DSH |
| bwrap sandbox | `core/sandbox.py` | реализовано, проверено |
| Git worktree / diff / merge / rollback | `core/workspace.py` | реализовано, проверено |
| Task FSM | `models.py`, `memory/tasks.py` | реализовано, тесты |
| SQLite WAL + FTS5 + миграция схемы | `memory/` | реализовано |
| Project registry + path validation | `memory/projects.py` | реализовано |
| Approvals + TTL + one-shot | `core/permissions.py` | реализовано |
| Usage/cost tracking | `core/usage.py` | реализовано; цены опциональны |
| Review gate + rework limit | `core/reviewer.py` | реализовано |
| Reports + inline buttons | `core/reviewer.py`, `telegram/` | реализовано |
| Memory summaries/FTS | `memory/conversations.py` | реализовано |
| systemd unit, doctor, backup script | `systemd/`, `scripts/`, `cli.py` | реализовано |

## Автономный цикл

1. Входящее сообщение сохраняется; Kimi классифицирует намерение.
2. `task` → планирование: Kimi tool call `submit_task_spec`.
3. Создаётся запись задачи и git worktree/ветка.
4. DeepSeek SDK запускается в bwrap с отдельным `DSH_HOME`.
5. Оркестратор собирает `git diff`, изменения файлов, проверки, exit codes.
6. Kimi получает evidence и возвращает `accept|rework|fail`.
7. `ReviewGate` программно запрещает принять пустой diff/проваленные проверки.
8. До `max_rework` циклов; затем `failed` и отчёт.
9. `accept` → `waiting_approval`; принять/откатить можно только явной кнопкой.
10. Merge — `--ff-only`, только на чистую base-ветку с неизменившимся base commit.

## Состояния

`created → planning → queued → running → reviewing → rework → waiting_approval → completed`
и терминальные `failed`, `cancelled`; плюс `paused`, `interrupted`.
Переходы проверяются в Python (`ensure_transition`), а не промптом.

## Permissions

- AUTO: чтение проекта, анализ, правки в worktree, локальные тесты/сборки.
- APPROVAL_REQUIRED: merge, rollback, архитектура, публичные интерфейсы, зависимости,
  системные пакеты, публикация, удаление данных, привилегированные/разрушительные операции.
- DENIED: обход permissions/sandbox, расширение полномочий, чужие секреты, отключение безопасности,
  неконтролируемый root, сокрытие действий.

Модель не вызывает эти действия напрямую: оркестратор проверяет уровень действия и, где нужно,
создаёт одноразовый approval с TTL.

## Ограничения

Смотри также README и `docs/SECURITY.md`.

1. **Сеть worker не ограничена allowlist.** bwrap изолирует файлы/PID, но network shared ради
   DeepSeek API. Это главный риск Phase 1.
2. **Один активный task на проект.** Разные проекты параллельны; внутри проекта — сериализация.
3. **Нет автоматического merge-конфликта.** При сдвиге base ветка/worktree сохраняются, merge
   отклоняется с сообщением.
4. **Окружение проверок** — supervisor venv + системные инструменты; project-specific venv не
   монтируется.
5. **Вложения** — текст и JPEG/PNG/WebP; бинарные файлы не передаются.
6. **Память** — FTS5 + резюме; нет векторной БД. Резюме неавторитетно.
7. **Стоимость** — только если заданы цены; иначе только токены. Остаток квоты Kimi Code API
   не документирован и не выдумывается.
8. **Откат после merge** — вручную `git revert`; автоматический rollback Phase 1 не делает.
9. **Resume DSH** — может не сработать при несовместимой/повреждённой сессии; молчаливой подмены нет.
10. **MCP/SSH/браузер/контейнеры** — не реализованы (Phase 4).
11. **Живые Telegram и платный DeepSeek** не проверены; Kimi API проверен живьём, DeepSeek SDK — на локальном Messages-mock. См. docs/TESTING.md.
12. **systemd unit** — пример; пути и ReadWritePaths нужно адаптировать.
13. **DeepSeek endpoint** — SDK использует Messages API; нужен совместимый endpoint.
14. **Root/home** — worker не получает домашнюю директорию, но host network остаётся.

## Критерии приёмки Phase 1

| # | Критерий | Статус |
|---|---|---|
| 1 | Написать Kimi через Telegram | код готов; live Telegram не проверено (нет запущенного бота) |
| 2 | Обычное общение | код + fake Kimi + live Kimi chat |
| 3 | Формирование ТЗ | code + schema test + live Kimi plan |
| 4 | DeepSeek получает ТЗ без копирования | worker prompt + SDK, mock test |
| 5 | Работа в разрешённом окружении | bwrap + worktree, integration test |
| 6 | Kimi получает фактические результаты | evidence builder, unit test + live review |
| 7 | Отчёт в Telegram | report + fake notifier test; live не проверено |
| 8 | Повторная доработка | rework loop test |
| 9 | История после перезапуска | SQLite reconnect test |
| 10 | Посторонний не управляет | owner middleware tests |
| 11 | Критические операции требуют подтверждения | approvals + button merge/rollback; tests |
| 12 | Зависший исполнитель останавливается | stop test + timeout/kill |
| 13 | Ключи не в репозитории/логах | .gitignore, redaction tests |
| 14 | Worker не блокирует Telegram | aiogram tasks + background jobs; unit design |
| 15 | Не нужен терминал/IDE | systemd unit + long polling |

Пункты 1, 7, 14 требуют live-проверки с Telegram-токеном; код к ним готов, но без запуска
нельзя утверждать, что Telegram-доставка проверена.

## Следующий этап

Phase 2 core реализован: task types, contract v2, новые статусы, type-aware validation,
структурное ревью, progress detection, migrations, approvals, Git push, per-model usage.
Остальное (egress proxy, COMPOSITE/DAG, routing, GitHub API, system/external executor) —
см. [PHASE2.md](PHASE2.md).
Phase 3: multi-agent, DAG, integration branch.
Phase 4: расширенные инструменты.
Phase 5: production/backup/VPS split.
