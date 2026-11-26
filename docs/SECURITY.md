# Безопасность

## Модель угроз

- Недоверенные данные: содержимое проекта, README, код, git diff, результаты тестов, ответы
  DeepSeek, веб-страницы, вложения пользователя.
- Возможный prompt injection: worker может попытаться заставить Kimi одобрить опасный diff,
  вывести секреты или обойти правила.
- Ошибка/компрометация модели не должна давать доступ к home, SSH, браузерным сессиям, systemd,
  Docker/Podman-сокетам и секретам Kimi/Telegram.

## Что сделано

### Telegram

- Авторизация по числовому `TELEGRAM_OWNER_ID`, не по username.
- Только личный чат (`private_chat_only=true`).
- Проверка owner для сообщений, команд, файлов и callback-запросов.
- `callback_data` содержит только числовые ID/короткие действия; никаких произвольных команд.
- Дедупликация update_id в SQLite (защита от повторной доставки после рестарта).
- Inline-кнопки не считаются подтверждением произвольных последующих действий.

### Секреты

- Только `.env`/systemd `EnvironmentFile`, `.gitignore` исключает `.env` и ключи.
- `require_env_mode_600=true` — отказ запуска при group/world-доступе.
- Redaction в логах, отчётах, diff, task events, Telegram и памяти по известным значениям и
  шаблонам (`sk-*`, Telegram bot token, Bearer, PEM, env-переменные с KEY/TOKEN/SECRET).
- Kimi-ключ и Telegram-токен не передаются worker-процессу.
- Worker получает только `DEEPSEEK_API_KEY` и минимальный набор env.
- `.env` не отправляется в контекст Kimi; вложения-`.env` не читаются.
- Проекты с отслеживаемыми `.env`, `*.pem`, `id_rsa` не регистрируются.

### Изоляция worker

DeepSeek Harness запускается в **bubblewrap**:

- read-only `/usr`, минимальный `/etc`, без `/home` и без пользовательских данных;
- на запись доступны только worktree, `DSH_HOME` задачи и state-каталог;
- отдельная PID/IPC/UTS namespace, `--new-session`, `--cap-drop ALL`, `--die-with-parent`;
- `GIT_DIR` указывает на read-only копию git-метаданных; `config` заменён санитизированной
  версией без credential/url/include и токенов в URL;
- RLIMIT_AS (память), RLIMIT_CPU, RLIMIT_NOFILE, RLIMIT_NPROC, тайм-аут процесса и kill process group;
- worker не видит Kimi/Telegram-секреты, SSH-ключи, браузерные профили, Docker-сокет;
- `sdk-minimal` не использует managed credentials пользователя;
- необязательная отправка session-log в DeepSeek отключена patch-файлом.

Внешний bwrap — граница безопасности, а `git worktree` — только изоляция изменений.
Git worktree сам по себе не является песочницей.

### Дополнительные read-only пути

- Задаются только явно через `/project extra-read` / project settings.
- В bwrap монтируются read-only; остальная домашняя директория недоступна.
- Нельзя смонтировать `/`, `$HOME` или путь, который затеняет worktree/DSH/state.
- Diff сканируется redactor'ом; при обнаружении секрета коммит/ревью/merge блокируются.

### Config import

- Только owner-команда `/project import-config`, только из явно разрешённых `extra_read_paths`.
- Symlink, path traversal, `.git`/`.env`/ключеподобные имена и текстовые секреты отклоняются.
- Лимиты 200 файлов / 10 MiB; после approval проверяется hash источника (TOCTOU).
- Бинарные файлы не сканируются на секреты; они допускаются только после явного approval.
- `--overwrite` создаёт backup в `var/backups/config-import` с mode 700.
- Импорт пишет в рабочее дерево и не коммитит автоматически; требуется отдельная проверка.

### COMPOSITE-этапы

- План проверяется на уникальные id, известные зависимости, циклы и соответствие executor.
- Опасные этапы (`import_config`, `github_ensure_repo`, `publish`, high) требуют отдельного
  one-shot approval, привязанного к source hash / repository / branch / commit.
- Worker не получает GitHub-токен и не выполняет внешние операции; это делает host-оркестратор
  после approval.
- Завершённые этапы сохраняются; повторяется только failed/running этап.
- SYSTEM/EXTERNAL вне поддерживаемых этапов fail-closed.

### Git push

- Только после отдельного одноразового approval владельца.
- Без force; удалённая вершина проверяется через `git ls-remote`.
- Remote URL валидируется; GitHub profile URL не принимается.
- Push выполняется оркестратором на host, worker не получает Git credentials.

### Оркестратор

- Модель только предлагает ТЗ/ревью; переходы состояний, merge, rollback, approval и лимиты
  выполняет Python.
- `ReviewGate` программно запрещает:
  - принять пустой diff;
  - принять работу с проваленными проверками;
  - продолжать бесконечно при исчерпании лимита доработок.
- Merge выполняется только по явному нажатию владельца, только на чистую base-ветку и только если
  base commit не изменился. `git merge --ff-only`, hooks отключены (`core.hooksPath=/dev/null`).
- Повторный accept после сбоя между merge и transition идемпотентен: система видит, что коммит
  уже в base, завершает задачу и не мержит повторно.
- Системные/внешние/составные задачи fail-closed, пока нет отдельного привилегированного
  исполнителя; это предотвращает ложный успех.
- Rollback/удаление worktree требует отдельного подтверждения с TTL и одноразовым payload.
- `git add` + проверка имён файлов: `.env`, ключи, PEM, credentials блокируются до коммита.
- Проверки проекта запускаются в том же bwrap, без API-ключей.
- Sensitive diff/worker output не отправляется в Telegram без redaction.
- Ошибки и отчёты не содержат ключей; traceback проходит через redaction filter.

### Prompt injection

- Системные промпты явно отделяют данные от инструкций.
- Код, diff, логи и отчёты исполнителя передаются Kimi как недоверенные данные.
- Worker-промпт запрещает выход за `/workspace`, доступ к секретам и обход sandbox.
- Оркестратор не исполняет команды, предложенные Kimi, на host: Kimi не имеет tool-доступа,
  а проверки берутся только из реестра проекта владельца.
- Kimi не может изменить свои полномочия или approval-политику: это код.

## Сеть worker

Phase 2.5 запускает worker с `--unshare-net` в отдельном network namespace. Прямого маршрута
наружу нет. `worker_runner` поднимает loopback TCP-relay, который ведёт в Unix-socket
`EgressProxy` на host; proxy разрешает только `egress.allowed_hosts`/`allowed_ports` и
host/port из `DEEPSEEK_BASE_URL`. Поддерживается CONNECT и plain HTTP; TLS не перехватывается.

Остаточный риск: разрешённый model API всё ещё получает содержимое промпта; это не сетевой
bypass. Custom endpoint на literal loopback не работает в netns (DSH bypasses loopback);
используйте non-loopback имя или тестовый профиль с `egress.enabled=false`.

## Осознанные ограничения

### Остальное

- `DEEPSEEK_API_KEY` доступен worker-процессу (это его собственный провайдерский ключ).
- Проектные команды тестов выполняются в окружении supervisor venv + системных инструментов;
  зависимости проекта не монтируются автоматически.
- Системный `~/.dsh` не используется и не изменяется. Это осознанно, но означает, что
  сохранённые в GUI credentials не подхватываются; используется `DEEPSEEK_API_KEY`.
- Kimi Code quota/balance не запрашивается: документированного API остатка нет; ошибки 429
  обрабатываются по тексту ответа.
- Логи и резюме могут содержать фрагменты кода проекта; они хранятся локально с правами 600.

## Что делать при инциденте

1. `systemctl --user stop ai-supervisor.service`.
2. Отозвать/ротировать `KIMI_API_KEY`, `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`.
3. Проверить `var/logs/`, `var/worktrees/`, git-ветки `ai-supervisor/*`.
4. Не запускать повторно задачу с неизвестным результатом: сначала изучить worktree и patch.
5. При подозрении на утечку — проверить сетевые соединения worker-процессов и удалить
   соответствующие worktree.
