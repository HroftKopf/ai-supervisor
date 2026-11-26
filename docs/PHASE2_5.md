# Phase 2.5 — исполняемые COMPOSITE-задачи и egress

Phase 2.5 устраняет блокировку `COMPOSITE`: Kimi формирует граф этапов, оркестратор
исполняет поддерживаемые операции существующими инструментами, сохраняет состояние в SQLite
и продолжает задачу после approvals/перезапуска. Worker больше не имеет прямого сетевого доступа.

## Что реализовано

### Модель COMPOSITE

`StageSpec` содержит уникальный `id`, `operation`, `goal`, `depends_on`, `executor`,
`required_permissions`, `acceptance_criteria`, `inputs`, `outputs` и параметры операции.
`TaskSpec.stages` заполняется только для `task_type=composite`; для остальных типов массив пуст.

Поддерживаемые операции и маршрутизация:

| operation | executor | реализация |
|---|---|---|
| `import_config` | `python` | существующий `ConfigImportService` в worktree, secret-scan, hash, backup |
| `git_commit` | `git` | `git add` + `WorkspaceManager.commit_changes`, no-op при отсутствии изменений |
| `validate` | `validator` | `TaskValidator` + `WorkspaceManager.collect_evidence`, checks, allowed_paths, secret scan |
| `github_ensure_repo` | `github` | `GitHubClient` + `ensure_remote`, идемпотентно |
| `publish` | `git` | ff-merge в base + `push_branch` без force + проверка remote SHA |
| `kimi_analyze` | `kimi` | `SupervisorAgent.chat`, результат как artifact |
| `deepseek_change` | `deepseek` | существующий bwrap/DSH worker, без выдачи host-доступа |

Модель `TaskSpec` проверяет уникальность id, известные зависимости, циклы, соответствие
executor операции и запрет CRITICAL. `COMPOSITE` без этапов отклоняется. Неизвестная операция
невозможна на уровне enum и даёт конкретную ошибку planner'а.

### Хранение и восстановление

Таблица `task_stages` (SQLite schema v3) хранит этапы, зависимости, параметры, approvals,
попытки, результаты и timestamps. Завершённые этапы не сбрасываются при resume. При старте:

- `RUNNING` этап переводится в `PENDING` и повторяется;
- `FAILED/BLOCKED` этап сбрасывается только при явном resume/auto-resume;
- `COMPLETED` этапы сохраняются;
- `WAITING_APPROVAL` переживает restart и продолжается после approve;
- при `app.auto_resume_composite=true` прерванная COMPOSITE-задача автоматически
  возвращается в очередь и продолжается с сохранённых этапов.

Отмена переводит незавершённые этапы в `CANCELLED`, сохраняя completed. Worktree и
промежуточные файлы не удаляются.

### Approvals и автоматическое продолжение

Этапы `import_config`, `github_ensure_repo` и `publish`, а также любой этап с `high`,
создают одноразовый approval:

- import: source hash + destination;
- GitHub: owner/name/private;
- publish: remote URL, branch, commit SHA, merge flag.

После approve approval потребляется, этап становится `READY`, задача возвращается в
`QUEUED`, и тот же исходный job продолжается автоматически. Отклонение переводит этап в
`BLOCKED`, задачу в `BLOCKED`; завершённые этапы сохраняются. Повторный resume запрашивает
approval заново. Старые approvals не могут выполнить изменившийся commit/remote/source.

### Идемпотентность внешних операций

- import: одинаковый destination не перезаписывается фиктивно; `already_applied`;
- commit: отсутствие staged-изменений — успешный no-op;
- GitHub: существующий repo проверяется, `create` не повторяется;
- publish: если remote SHA уже равен local, push не выполняется;
- после сбоя между merge и сменой статуса повторный этап видит `is_applied` и продолжает.

### Egress

Worker запускается с `--unshare-net` в отдельном network namespace и user namespace
(loopback поднимается только внутри). Наружу он не имеет маршрута. `worker_runner` поднимает
TCP-relay на `127.0.0.1`, который ведёт в Unix-socket `EgressProxy` на host. Proxy разрешает
только `egress.allowed_hosts`/`allowed_ports` (плюс host/port из `DEEPSEEK_BASE_URL`) и
поддерживает HTTP CONNECT/HTTP. DSH получает `HTTP(S)_PROXY` и использует свой global
dispatcher; прямые соединения невозможны. Сокет имеет mode 600 и живёт в `/state`.

## Git Authentication Manager (Phase 2.5.1)

Все сетевые Git-операции (`ls-remote`, `fetch`, `push`) идут через `GitAuthManager`:

- remote URL читается из локального Git config и проверяется; credentials в URL запрещены;
- GITHUB_TOKEN передаётся только для HTTPS-хостов из `github.push_hosts` (по умолчанию
  `github.com`); сторонние хосты токен не получают;
- токен пишется только в приватный временный файл `0600`; askpass-скрипт `0700` читает его;
  ни токен, ни его значение не попадают в argv, URL, Git config, SQLite, логи или Telegram;
- `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS_REQUIRE=force`; command-level config отключает hooks,
  credential helpers, redirects и proxy, требует TLS; опасные `url.*`, `http.*`, `credential.*`,
  `include.*`, `remote.*.pushurl` в локальном config отклоняются;
- SSH remote (`git@...`, `ssh://...`) обрабатывается как SSH и не переводится в HTTPS;
- временные файлы удаляются в `finally`; при аварийном завершении остатки `git-auth-*`
  удаляются при следующем startup;
- при отсутствии/отклонении токена COMPOSITE переходит в `WAITING_INPUT`, а не зацикливается;
- `doctor` проверяет GitHub token через `/user`.

Диагностика не печатает токен. Токен не передаётся worker/DSH/Kimi и не попадает в их sandbox.

### Отчёты

Для COMPOSITE отчёт показывает `Python Orchestrator (COMPOSITE)`, статус каждого этапа,
executor, длительность, hash коммита, результат secret-scan, проверки и remote SHA; время
планирования учитывается отдельно.

## Интеграционный сценарий Niri

`tests/test_composite.py::test_niri_composite_publishes_to_github_after_approvals`:

1. Source обнаруживается в extra_read_paths (без ручного указания пути).
2. `import` копирует конфиг в worktree (approval).
3. `commit` создаёт коммит.
4. `validate` проверяет diff/секреты.
5. `github_ensure_repo` создаёт repo через mock API и настраивает remote (approval).
6. `publish` делает ff-merge и push в временный bare remote (approval).
7. Remote SHA проверяется; задача завершается финальным отчётом.

`test_live_kimi_builds_composite_niri_plan` проверяет, что живой Kimi формирует именно
`import → commit → validate → github → publish` с корректными executor.

## Проверено

- `pytest`: **125 passed, 4 skipped**.
- `ruff check .`: без ошибок.
- `ai-supervisor doctor`: GitHub token принят; egress netns/proxy OK.
- Реальный `git push --dry-run` из worktree #004 через `GitAuthManager` — rc 0 (без публикации).
- Реальный DSH runtime в bwrap с egress proxy: **2 integration tests passed** (Messages mock
  через non-loopback hostname; proxy разрешает только нужный host/port).
- Живой Kimi: chat/classify/plan/review + composite Niri plan — passed (последний успешный
  прогон; следующий упёрся в 5-часовую квоту 429).
- Миграция копии рабочей БД v2→v3: schema 3, `task_stages`, backup `pre-migration-v3-*.db`,
  исторические задачи сохранены.
- COMPOSITE: зависимости, approvals, отказ, повтор, сохранение completed, no-op import,
  auto-discovery, GitHub mock + local bare remote, auto-resume после restart, отсутствие
  model-вызовов на простых этапах, auth-blocked auto-resume.
- Git auth: HTTPS askpass, host allowlist, SSH passthrough, missing/rejected token,
  отсутствие токена в env/config, единый auth для ls-remote/fetch/push, отсутствие ретраев
  на auth-error, отчётные поля.

## Ограничения

- Независимые этапы исполняются последовательно; параллельного DAG пока нет.
- `inputs`/`outputs` сохраняются как артефакты/метаданные, но не подставляются как
  шаблонные переменные между этапами; параметры этапов задаются явно.
- `SYSTEM_OPERATION`/`EXTERNAL_OPERATION` без поддерживаемого этапа по-прежнему fail-closed.
- Egress proxy не является TLS-intercepting; он туннелирует CONNECT. Custom endpoint на
  literal loopback не работает в netns (DSH bypasses loopback), используйте non-loopback имя
  или тестовый профиль без egress.
- Реальная публикация пользовательских файлов на GitHub не выполнялась; тесты используют
  mock API и временный bare remote.
- Telegram live и платный DeepSeek endpoint не проверялись.

## Миграция и запуск

При первом запуске Phase 2.5 создаётся backup `var/backups/pre-migration-v3-*.db`, затем
схема обновляется до v3 (добавляется `task_stages`). `.env` не изменялся.

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
python -m pip install -e '.[dev]'
scripts/backup.sh
ai-supervisor doctor
systemctl --user restart ai-supervisor.service
journalctl --user -u ai-supervisor.service -f
```

Если сервис не установлен: `ai-supervisor run`.

Проверьте: `/doctor`, `/queue`, READ_ONLY-задачу, COMPOSITE с простым import/commit/validate,
затем Niri-сценарий с настроенным `extra_read_paths`. Для реального GitHub задайте
`GITHUB_TOKEN` и `egress.allowed_hosts` при необходимости.
