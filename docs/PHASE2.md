# Phase 2 — модернизация ядра

> Phase 2.5 добавляет исполняемые COMPOSITE-этапы и egress; актуальные результаты см.
> [PHASE2_5.md](PHASE2_5.md). Ниже — историческая граница Phase 2 core.

Этот этап расширяет Phase 1, не переписывая bwrap, DSH worker runner, SQLite/FTS и owner-auth.
Реализована безопасная основа для универсальных задач; полная декомпозиция/routing/GitHub API
вынесены дальше и явно перечислены в конце.

## Что изменено

### Контракт задачи v2

`TaskSpec` теперь содержит:

- `task_type`: READ_ONLY, MODIFY, GIT_OPERATION, EXECUTE, SYSTEM_OPERATION,
  EXTERNAL_OPERATION, COMPOSITE;
- `source_request`, `scope`, `allowed_paths`, `expected_artifacts`, `required_tools`,
  `required_permissions`, `dependencies`, `parent_task_id`, бюджеты и лимиты попыток;
- `requires_file_changes`; для READ_ONLY он не может быть true.

Старые сохранённые ТЗ читаются с defaults (`MODIFY`, requires_file_changes=true), поэтому
история не теряется.

### Типы задач и валидация

- READ_ONLY: пустой diff — нормальный результат; требуется фактический отчёт/проверки.
- MODIFY: diff/коммит нужен только если `requires_file_changes=true`.
- EXECUTE: успешность определяется проверками/exit codes; отсутствие проверок помечается
  как «не проверено», а не «тесты прошли».
- GIT_OPERATION: проверяется состояние Git; публикация — отдельным approval `/push`.
- SYSTEM_OPERATION/EXTERNAL_OPERATION: пока **fail-closed** (BLOCKED), так как
  привилегированный исполнитель ещё не реализован. COMPOSITE в Phase 2.5 исполняется
  через этапы; см. [PHASE2_5.md](PHASE2_5.md).
- Проверяются `allowed_paths`, expected artifacts, worker completion и настроенные проверки.

### Жизненный цикл

Добавлены состояния:

`VALIDATING`, `WAITING_INPUT`, `RETRY_WAIT`, `PARTIALLY_COMPLETED`, `BLOCKED`.

Переходы проверяются `TRANSITIONS`; после перезапуска незавершённые задачи становятся
`INTERRUPTED` (кроме WAITING_APPROVAL/PAUSED/BLOCKED/WAITING_INPUT/RETRY_WAIT).
Валидация выполнения отделена от Kimi review.

### Ревью

Kimi возвращает `approved` / `rework_required` / `approval_required` / `blocked` / `failed`.
Замечание содержит criterion, evidence, severity, recommendation и within_authority.
Оркестратор не разрешает `approved` при critical-замечании, проваленных проверках,
нарушении allowed_paths или отсутствии требуемого изменения. Minor/info не блокируют.

### Прогресс и лимиты

Для каждой попытки сохраняются diff hash, checks и замечания. Если две повторные попытки
дают одинаковое состояние, задача переходит в BLOCKED, а не запускает бесконечную доработку.
`max_attempts`/`max_rework`/`max_iterations`/`max_minutes` учитываются; при исчерпании —
отчёт и сохранение worktree.

### Политика и approvals

- Добавлена карта risk LOW/MEDIUM/HIGH/CRITICAL для действий.
- До worker проверяются unknown tools, CRITICAL permissions, high-risk/system/external/composite.
- HIGH требует одноразового approval с TTL; после approval задача продолжается с места остановки.
- Git push выполняется только после отдельного approval, без force, с проверкой remote SHA.
- Устаревшие approvals не могут откатить уже завершённую задачу; повторный merge идемпотентен
  (обнаружение уже применённого коммита).

### Git

- Накопительный diff от base commit включает коммиты всех попыток.
- Коммит создаётся только при наличии новых staged изменений.
- `git push` — только через approval, с проверкой remote; force отсутствует.
- Merge проверяет чистую base-ветку и неизменность base commit; после сбоя между merge и
  transition задача завершается идемпотентно при повторном accept.

### Проекты и конфиги

- Project settings: remote, token budget, max rework, per-project policy, extra read-only paths.
- Telegram: `/project list|select|permissions|remote|budget|max-rework|extra-read|permission`.
- Extra read paths монтируются в bwrap read-only; worker может скопировать конфиг в worktree.
- `/project import-config <source> <dest> [--overwrite]` — отдельный approval-tool:
  source только из extra read paths, запрет symlink/секретов/traversal, лимиты 200 файлов/10 MiB,
  hash источника проверяется после approval, overwrite делает backup в `var/backups/config-import`.
- Diff с секретами блокируется до коммита/ревью/merge.

### Usage

- per-model pricing (`[usage.prices."provider/model"]`);
- cache read/write, reasoning, duration, errors;
- локальные сутки по `app.timezone`;
- `/usage today|week|month|task N`.

### Telegram

Добавлены `/task`, `/task_info`, `/queue`, `/cancel`, `/pause`, `/retry`, `/push`, `/github`,
`/report full`, `/doctor`, `/project` management. Старые команды сохранены.

### Миграции

`Database` версионирует схему, делает backup перед миграцией (v1/v2→v3), добавляет колонки транзакционно,
отказывается открывать БД новее поддерживаемой версии. Тест `tests/test_migrations.py`.

## Что проверено

- `pytest`: 120 passed, 4 skipped.
- `ruff check`: без ошибок.
- Реальный DSH runtime в bwrap: 2 integration tests passed (включая bash tool call).
- Живой Kimi API: chat/classify/plan v2/review — passed.
- Миграция копии рабочей БД v1/v2→v3 с backup — passed, 2 historical tasks сохранены.
- Merge idempotence и approved Git push в локальный bare remote — tests.
- Config import: file/dir, backup при overwrite, secret scan, traversal/symlink rejection,
  source hash после approval — tests.
- Startup recovery: RUNNING→INTERRUPTED, PAUSED сохраняется, worker → interrupted — test.
- GitHub REST client (exists/info/create/auth errors) — tests с локальным mock API.
- Telegram-команды и project settings проверены unit-тестами/фикстурами; live Telegram не запускался.

## Ограничения

Актуальные ограничения Phase 2.5: [PHASE2_5.md](PHASE2_5.md).
Исторически Phase 2 core не исполнял COMPOSITE и не имел egress; сейчас это реализовано.

## Перезапуск после обновления

`.env` не изменялся.

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
ai-supervisor doctor
systemctl --user restart ai-supervisor.service
journalctl --user -u ai-supervisor.service -f
```

Если сервис не установлен: `ai-supervisor run`.
Перед первым запуском рекомендуется остановить старый процесс и сделать backup:

```bash
scripts/backup.sh
```

При старте будет создан backup `var/backups/pre-migration-v3-*.db`, затем схема обновится.
Проверьте после рестарта: `/doctor`, `/queue`, обычный вопрос Kimi, небольшую READ_ONLY задачу
(должна завершиться без diff), затем MODIFY-задачу и `/push` при наличии remote.
