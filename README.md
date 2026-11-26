# AI Supervisor

Персональная мультиагентная система для программирования с Telegram-интерфейсом:
**Kimi K3.1 Pro** — технический руководитель и ревьюер, **DeepSeek Harness** — исполнитель,
**Python-оркестратор** — управляющая инфраструктура.

> Статус: **Phase 1 + Phase 2 core**. Добавлены типы задач, контракт v2, новые состояния,
> детерминированная проверка по типу задачи, структурное ревью, progress detection, миграции,
> политики/подтверждения, per-model usage и Git push. Живые сценарии Kimi API проверены;
> Telegram Bot API и платный DeepSeek endpoint ещё не проверялись. Часть требований
> (полная декомпозиция, routing, GitHub API, system/external executor) вынесена в следующий
> этап и явно перечислена в [«Известных ограничениях»](#известные-ограничения-phase-2).
> Система не заявляет о непроверенных функциях как о работающих.

---

## Что уже работает

- Telegram long polling, только личный чат numeric owner ID, защита от чужих сообщений и callback.
- Обычный диалог с Kimi на естественном языке; классификация «обсуждение / задача».
- Формирование ТЗ v2 инструментом `submit_task_spec`: тип задачи, область, разрешённые пути,
  инструменты, полномочия, артефакты, бюджеты, критерии; `reasoning_content` сохраняется.
- Типы задач: `READ_ONLY`, `MODIFY`, `GIT_OPERATION`, `EXECUTE`, `SYSTEM_OPERATION`,
  `EXTERNAL_OPERATION`, `COMPOSITE`. Небезопасные/нереализованные типы fail-closed.
- Изолированный git worktree на задачу, отдельная ветка, коммит оркестратора, накопительный diff
  от base commit (включая коммиты предыдущих попыток).
- DeepSeek Harness через официальный Python SDK (`deepseek-harness-sdk==0.2.2rc2`),
  профиль `sdk-minimal`, отдельный `DSH_HOME` на задачу.
- Запуск worker внутри bubblewrap: read-only `/usr`, только worktree/DSH-home/state доступны на запись,
  без домашней директории и без Kimi/Telegram-ключей.
- Сбор фактических доказательств: накопительный `git diff`, список файлов, exit codes проверок,
  вывод тестов/сборки, проверка expected artifacts.
- Проверка по типу задачи: READ_ONLY не требует diff; MODIFY требует изменения только если
  `requires_file_changes=true`; EXECUTE учитывает проверки; запрет выхода за `allowed_paths`.
- Структурное ревью: `approved`, `rework_required`, `approval_required`, `blocked`, `failed`;
  замечания с criterion/evidence/severity/recommendation/within_authority.
- Progress detection: одинаковый diff/проверки/замечания останавливают цикл как `BLOCKED`,
  а не бесконечно дорабатывают.
- Новые состояния: `VALIDATING`, `WAITING_INPUT`, `RETRY_WAIT`, `PARTIALLY_COMPLETED`, `BLOCKED`;
  миграции SQLite v1→v2 с backup.
- Preflight-контракт: неизвестные tools, CRITICAL-полномочия и нереализованные privileged-типы
  блокируются до worker; HIGH требует подтверждения.
- Git push с обязательным подтверждением, без force, с проверкой remote SHA.
- Per-model usage/pricing, кэш/reasoning, duration; команды `/usage today|week|month|task N`.
- Новые Telegram-команды: `/task`, `/task_info`, `/queue`, `/cancel`, `/pause`, `/retry`, `/doctor`,
  `/push`; проектные `/project list|select|permissions|remote|budget|extra-read|permission`.
- FTS5-поиск, резюме, stop/rollback/pause/retry, systemd, doctor, redaction.

Phase 1 намеренно **не** реализует SSH, браузер, Docker/Podman, MCP и распределённый VPS.
Это следующие этапы, а не скрытые заглушки.

---

## Архитектура

```text
Telegram (owner-only)
      │
      ▼
Application / Orchestrator ─────── SQLite WAL
      │        │                    ├─ conversations/memory FTS5
      │        │                    ├─ projects, tasks, events
      │        │                    ├─ approvals, usage
      │        ▼
      │    Kimi Supervisor (OpenAI-compatible API)
      │        │ plan/review/chat
      ▼        ▼
  Task Scheduler (max 3 workers, per-project serialization)
      │
      ▼
  Worker Manager ── bubblewrap ── DeepSeek Harness SDK
      │                            (isolated DSH_HOME + worktree)
      ▼
  Workspace Manager
      │ git diff / checks / commit / merge / rollback
      ▼
  Files / Git / test commands
```

Модель предлагает действие, но не исполняет его напрямую. Все переходы состояний, sandbox,
merge, откат, approval и лимиты контролируются Python-кодом. Kimi не имеет инструментов доступа
к файлам или терминалу; она возвращает структурированные ТЗ/ревью.

---

## Быстрый старт на Arch Linux

```bash
sudo pacman -S --needed python git bubblewrap sqlite

cd ~/Projects/ai-supervisor
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

cp .env.example .env
chmod 600 .env
$EDITOR .env

ai-supervisor doctor
ai-supervisor run
```

`doctor` проверяет Python, SQLite FTS5, Git, bubblewrap, DSH runtime, права `.env`, Telegram-бота
и доступность выбранной модели Kimi. Он не печатает секреты.

`ai-supervisor run` запускает long polling. Публичный IP, порты и домен не нужны.
Для работы без открытого терминала используйте systemd user unit:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ai-supervisor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ai-supervisor.service
loginctl enable-linger "$USER"
journalctl --user -u ai-supervisor.service -f
```

Подробно: [docs/INSTALL.md](docs/INSTALL.md), безопасность: [docs/SECURITY.md](docs/SECURITY.md).

---

## Секреты и `.env`

Секреты хранятся только в `.env` (или `EnvironmentFile` systemd), не в `config.toml` и не в Git.

```env
KIMI_API_KEY=
KIMI_BASE_URL=https://api.kimi.ai/coding/v1
KIMI_MODEL=k3.1-pro

DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=
DEEPSEEK_MODEL=

TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_ID=

# Optional: GitHub repository info/create. Never passed to the worker.
GITHUB_TOKEN=
```

- `.env` должен иметь режим `600`; при `require_env_mode_600=true` сервис откажется запускаться с
  более широкими правами.
- Ключ Kimi никогда не передаётся worker-процессу.
- `DEEPSEEK_API_KEY` передаётся только изолированному DSH-процессу.
- В логах, отчётах, diff и Telegram секреты заменяются на `[REDACTED]`.

### Kimi Code и Moonshot Open Platform — разные системы

| | Kimi Code | Moonshot Open Platform |
|---|---|---|
| Base URL | `https://api.kimi.ai/coding/v1` | `https://api.moonshot.ai/v1` |
| Model ID | `k3.1-pro` | `kimi-k3.1-pro` |
| Ключ | из Kimi Code Console | из platform.kimi.ai |
| Оплата | подписка/квоты | pay-as-you-go |

Ключи и base URL **не взаимозаменяемы**. Система сверяет выбранную модель со списком `/models`
и не подменяет модель. Если Kimi Code молча откатился бы на другую модель из-за опечатки,
AI Supervisor остановится с ошибкой до выполнения задач.

Параметры K3.1 Pro: `temperature`/`top_p` не передаются (они фиксированы), используется streaming,
`reasoning_effort` (`low`/`high`/`max`), `max_completion_tokens`; `reasoning_content` сохраняется
и передаётся обратно в assistant-сообщениях, включая tool-calling.

Актуальные ссылки и проверенные факты: [docs/API_NOTES.md](docs/API_NOTES.md).

### DeepSeek Harness

AI Supervisor использует **официальный Python SDK** `deepseek-harness-sdk==0.2.2rc2`.
SDK запускает собственный bundled runtime; системный `dsh` 0.2.2-rc.5 не изменяется.

- Профиль `sdk-minimal`: `danger-full-access` внутри внешнего bwrap, без managed credentials.
- `DSH_HOME` — `var/dsh-homes/task-NNNN`, никогда `~/.dsh`.
- Patch `config/dsh/disable-session-log-upload.yml` отключает необязательную отправку журналов
  сессии провайдеру.
- SDK 0.2.2rc2 общается с DeepSeek по **Messages API** (`/v1/messages`). Если
  `DEEPSEEK_BASE_URL` задан, endpoint должен быть Messages-совместимым. Пустое значение
  использует официальный default SDK.
- Worker не получает `KIMI_API_KEY`, `TELEGRAM_BOT_TOKEN`, SSH-ключи или браузерные профили.
- Существующие настройки Harness пользователя не читаются и не перезаписываются.

---

## Telegram

1. Создайте бота у [@BotFather](https://t.me/BotFather), получите токен.
2. Узнайте **числовой** user ID через [@userinfobot](https://t.me/userinfobot) или Telegram API.
   Username не используется как идентификатор.
3. Заполните `TELEGRAM_BOT_TOKEN` и `TELEGRAM_OWNER_ID` в `.env`.
4. Запустите `ai-supervisor doctor`, затем `ai-supervisor run`.
5. Напишите боту `/start` из личного чата владельца.

По умолчанию бот работает только в private chat. Сообщения, команды, файлы и callback-запросы
других пользователей отклоняются. Long polling означает отсутствие публичного IP и вебхуков.

Команды: `/start`, `/help`, `/projects`, `/project` (`list|select|info|add|create|remove|
permissions|remote|budget|max-rework|extra-read|permission|test|build|instructions`),
`/new`, `/task`, `/task_info`, `/tasks`, `/queue`, `/status`, `/agents`, `/logs`, `/diff`,
`/report`, `/approve`, `/reject`, `/stop`, `/cancel`, `/pause`, `/resume`, `/retry`,
`/rollback`, `/rework`, `/push`, `/github info|create`, `/usage` (`today|week|month|task N`),
`/memory`, `/settings`, `/doctor`. Команды не обязательны — можно писать обычным языком.

### Проекты

```text
/project add hks-shell /home/hks/hks-shell
/project test "cargo test"
/project build "cargo build"
/project create my-new-project "Rust / Wayland"
/project use hks-shell
```

- Путь должен быть абсолютным, существующим Git-репозиторием и находиться строго внутри
  `security.allowed_project_roots` из `config.toml`.
- Симлинки разрешаются, выход за разрешённую директорию и регистрация `/`/`$HOME` запрещены.
- Репозитории с отслеживаемыми `.env`, `*.pem`, `id_rsa` и т.п. не регистрируются.
- `/project remove` удаляет только запись реестра; файлы на диске не трогает.

На каждую задачу создаётся `git worktree` и ветка `ai-supervisor/task-NNNN-...`.
Принять изменения можно только кнопкой «Принять изменения»: выполняется проверка чистой основной
ветки и неизменности base commit, затем `git merge --ff-only`. При конфликте/сдвиге base задача
не мержится автоматически; worktree сохраняется.

---

## Как проходит задача

1. Сообщение владельца классифицируется Kimi.
2. Для задачи Kimi формирует ТЗ через tool call.
3. Оркестратор создаёт задачу и worktree.
4. DeepSeek Harness выполняет работу в bwrap; оркестратор собирает `git diff`, exit codes проверок.
5. Kimi получает фактические доказательства и выносит `accept`/`rework`/`fail`.
6. При `rework` — до 3 циклов (настраивается); при исчерпании лимита задача останавливается с отчётом.
7. При `accept` — отчёт с inline-кнопками. Merge выполняется только после явного нажатия владельца.
8. Незавершённые изменения при ошибке/остановке остаются в worktree; `/rollback` сохраняет patch.

Текстовое «готово» от исполнителя не считается доказательством. Проверка опирается на diff,
состояние файлов, exit codes и настроенные проверки. Если проверки не настроены, отчёт прямо
указывает, что верификация ограничена diff и ревью.

---

## Конфигурация

`config.toml` содержит лимиты, sandbox, тайм-ауты, память, Git и политику хранения.
Ключевые параметры:

```toml
[limits]
max_workers = 3
max_rework = 3
max_task_minutes = 45
max_iterations = 12
max_task_kimi_requests = 40
max_task_total_tokens = 0
daily_token_limit = 0

[sandbox]
backend = "bwrap"
network = true
memory_mb = 4096
```

Все значения валидируются Pydantic при запуске. `sandbox.backend = "none"` запрещён, пока явно
не выставлен `security.allow_unsafe_no_sandbox = true`; в production этого делать нельзя.

`/settings` показывает безопасную сводку без секретов.

---

## Расходы

Учитываются отдельно:

- входные/выходные токены Kimi;
- кэш-чтение/кэш-запись и reasoning-токены Kimi, если API их вернул;
- входные/выходные/кэш/reasoning-токены DeepSeek, если DSH их вернул;
- количество запросов;
- суммы по задаче, проекту и дню.

Стоимость в долларах рассчитывается только если в `[usage]` явно заданы цены за миллион токенов.
Иначе `/usage` показывает только токены и явно сообщает, что цены не заданы. Система не выдумывает
остаток квоты Kimi Code: если провайдер не отдаёт данные, их нет.

При исчерпании лимита новые запросы приостанавливаются, задача переходит в `paused`, владелец
получает уведомление и может продолжить после устранения причины.

---

## Что проверено

Окружение фактической разработки:

- Arch Linux x86_64;
- Python 3.14.10;
- DeepSeek Harness CLI 0.2.2-rc.5 (системный, не изменялся);
- `deepseek-harness-sdk` 0.2.2rc2 (bundled runtime);
- bubblewrap 0.13.1;
- SQLite 3.54.1 с FTS5;
- Git 2.56.1.

Выполнено:

- `pytest`: **59 passed, 3 skipped** (интеграционные и live-тесты по умолчанию пропускаются).
- `AI_SUPERVISOR_RUN_INTEGRATION=1 pytest tests/test_worker_integration.py`: **2 passed** —
  реальный `DeepSeekHarness` в bwrap против локального Messages-совместимого mock API.
  Проверены запуск runtime, проброс `DEEPSEEK_BASE_URL`, запись результата, usage (input 42,
  output 7, cached 3, requests 1), завершение `completed` и реальный `bash` tool call,
  изменивший файл в worktree.
- `AI_SUPERVISOR_RUN_LIVE_KIMI=1 pytest tests/test_kimi_live.py`: **1 passed** — реальные
  запросы к настроенному Kimi API: обычное общение (`tool_choice=auto`), классификация,
  формирование ТЗ v2 и структурное ревью (`tool_choice=required` + ровно один инструмент).
- `ruff check .`: все проверки пройдены.
- `bwrap` probe: user namespaces + read-only `/usr` + изолированный worktree.
- Ручной end-to-end оркестрации с fake Kimi и реальным Git: plan → execute → diff → review →
  waiting_approval → accept → ff-merge → completed.
- SQLite WAL/FTS5, восстановление истории, redaction, path traversal, approval expiry,
  owner middleware, duplicate update.

**Не проверено живьём:**

- реальный Telegram Bot API (getMe, long polling, кнопки);
- реальный платный DeepSeek endpoint (использовался локальный Messages-mock, а не платный endpoint).

Kimi Code API проверен живьём: `/models`, streaming chat, classify, ТЗ и review с
`tool_choice=required` и одним инструментом. Ошибка `tool_choice 'specified' is incompatible
with thinking enabled` больше не воспроизводится; подробности в [docs/KIMI_FIX.md](docs/KIMI_FIX.md).

Эти проверки нужно выполнить отдельно (Telegram и платный DeepSeek):

```bash
ai-supervisor doctor
ai-supervisor run
```

Результаты и известные ограничения: [docs/TESTING.md](docs/TESTING.md).

---

## Известные ограничения Phase 2

- **Сеть worker не изолирована по allowlist.** bwrap изолирует файлы/PID/UID, но worker должен
  достучаться до DeepSeek API, поэтому сеть общая. Это позволяет exfiltration данных проекта
  при prompt injection. Phase 2: egress-proxy/allowlist.
- **Одна активная задача на проект.** Разные проекты могут выполняться параллельно (до
  `max_workers`), но внутри проекта задачи сериализованы, чтобы не конфликтовать в базовой ветке.
- **Нет автоматического разрешения конфликтов.** Если основная ветка сдвинулась, merge отклоняется,
  worktree сохраняется.
- **Окружение проверок проекта** — supervisor venv + системные инструменты; Project-specific
  `.venv`/`node_modules` не монтируются. Для Python-проектов задайте команду, которая работает
  в этом окружении, или используйте системный `uv`/`poetry` после отдельной настройки.
- Поддерживаются текстовые вложения и JPEG/PNG/WebP; произвольные бинарные файлы не передаются.
- Память — SQLite FTS5 + резюме; векторного поиска нет. Резюме не считается подтверждённым фактом.
- Нет SSH, браузера, Docker/Podman, MCP, удалённых worker-узлов.
- Откат после merge не автоматизирован — используйте `git revert`.
- Kimi Code не предоставляет документированный API остатка квоты; `/usage` не выдумывает его.
- При несовместимости сохранённой DSH-сессии `/resume` может потребовать нового запуска; система
  не подменяет сессию молча.

### Что ещё не реализовано в Phase 2

- Полная декомпозиция `COMPOSITE` на зависимые этапы, DAG и параллельные подзадачи.
- Маршрутизация Level 0–3 между моделями/исполнителями (кроме детерминированных Telegram-команд).
- Продвинутые GitHub PR/checks и настройки организаций; базовые repo info/create и Git push реализованы.
- Полноценный привилегированный исполнитель для `SYSTEM_OPERATION`/`EXTERNAL_OPERATION`;
  сейчас эти типы блокируются до отдельного этапа.
- Config import как отдельный approval-tool; вместо него worker получает только явно разрешённые
  read-only paths и может скопировать конфиг в worktree с secret-сканом diff.
- Ретенция task-логов/событий и JSON-корреляция логов; файловые логи ротируются, task-каталоги
  пока требуют периодической очистки.

Полный список: [docs/PHASE2.md](docs/PHASE2.md).

---

## Тесты

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
AI_SUPERVISOR_RUN_INTEGRATION=1 .venv/bin/pytest -q tests/test_worker_integration.py
AI_SUPERVISOR_RUN_LIVE_KIMI=1 .venv/bin/pytest -q tests/test_kimi_live.py -s
```

Тесты не требуют реальных API-ключей. Интеграционный тест поднимает локальный mock Messages API
и запускает настоящий DSH runtime в bwrap.

---

## Структура

```text
src/ai_supervisor/
  agents/            Kimi supervisor (prompt orchestration)
  providers/         Kimi OpenAI-compatible client
  core/              orchestrator, scheduler, worker manager, workspace,
                     sandbox, validation, review gates, Git/GitHub,
                     permissions, usage, redaction
  memory/            SQLite WAL, projects, conversations, tasks, FTS5
  telegram/          bot, owner auth, handlers, notifications
  tools/             reserved for Phase 4 tools
  prompts/           supervisor/planner/reviewer/worker/summarizer
config/dsh/          privacy patch for DSH session-log upload
systemd/             user service
tests/               unit + optional real DSH integration
docs/                API notes, install, security, testing, phase 1/2
```

---

## Следующий этап

1. **Phase 2.5:** egress allowlist/proxy для worker, полная декомпозиция COMPOSITE, routing,
   GitHub API, привилегированный system/external executor, retention/structured logs.
2. **Phase 3:** несколько исполнителей, граф зависимостей, интеграционная ветка и разрешение
   конфликтов, параллельные worktree.
3. **Phase 4:** SSH, Podman, браузер, MCP, системный мониторинг — отдельными модулями с политиками.
4. **Phase 5:** production systemd/backup, затем Controller + Worker Node с mTLS.

Не реализовывать распределённую инфраструктуру раньше стабильной локальной версии.
