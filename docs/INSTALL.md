# Установка на Arch Linux

Проверено на Arch Linux x86_64, Python 3.14.10, bubblewrap 0.13.1, Git 2.56.1.
Требуется Python 3.12+.

## 1. Пакеты

```bash
sudo pacman -S --needed python git bubblewrap sqlite
```

- `bubblewrap` обязателен: без него worker не запускается.
- Node.js не требуется для AI Supervisor: Python SDK поставляет собственный runtime.
  Системный DeepSeek Harness и его настройки не изменяются.
- SQLite в Arch собран с FTS5 (проверяется `ai-supervisor doctor`).

## 2. Проект и venv

```bash
cd ~/Projects
git clone <repo-url> ai-supervisor   # или используйте существующий каталог
cd ai-supervisor
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Проверка:

```bash
python -c 'import ai_supervisor; print("ok")'
```

## 3. Конфигурация

```bash
cp config.example.toml config.toml
$EDITOR config.toml
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Минимум:

```env
# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_OWNER_ID=123456789
# Kimi control model
KIMI_API_KEY=...
# DeepSeek Harness executor (required)
DEEPSEEK_API_KEY=...
# Optional control providers
# OPENAI_API_KEY=
# OPENAI_COMPATIBLE_API_KEY=
# Optional GitHub
# GITHUB_TOKEN=
```

Nonsecret Kimi/DeepSeek endpoints and model IDs are in `config.toml`
(`[providers.kimi]`, `[providers.deepseek]`), not in `.env`.

`providers.deepseek.base_url` оставьте пустым для официального endpoint SDK. Если задаёте
свой, он должен быть совместим с **Messages API** (`/v1/messages`), который использует DSH
0.2.2rc2.

Провайдеры управляющих моделей выбираются в `[agent_roles]`; по умолчанию все роли — Kimi.
Для OpenAI задайте `OPENAI_API_KEY` и при необходимости `[providers.openai]`.
Ключи не передаются worker-процессам.

`config.toml`:

- `security.allowed_project_roots` — реальные корни ваших проектов.
- `scheduler.max_workers` — начальное значение 3.
- `task_limits.max_rework` — начальное значение 3.
- `sandbox.backend = "bwrap"` не меняйте на `none`.
- `usage.pricing` — при желании задайте цены за миллион токенов; иначе стоимость не рассчитывается.

## 4. Telegram

1. [@BotFather](https://t.me/BotFather) → `/newbot` → токен.
2. [@userinfobot](https://t.me/userinfobot) → числовой ID (не username).
3. Заполните `.env`.
4. Запустите `ai-supervisor doctor` — он проверит `getMe` и права `.env`.
5. `ai-supervisor run`.
6. Напишите `/start` боту в личном чате.

Бот использует long polling. Порты, домен, HTTPS-сертификат и публичный IP не нужны.
Работает только private chat владельца.

## 4.1. Обновление с Phase 1

При первом запуске новой версии `Database` сделает backup
`var/backups/pre-migration-v8-*.db` и обновит схему v1/v2/v3/v4/v5/v6/v7→v8. Старые задачи сохраняются.
Перед обновлением можно дополнительно выполнить `scripts/backup.sh`.

## 4.2. Egress worker

Worker не имеет прямого выхода в сеть. `config.toml`:

```toml
[network.egress]
enabled = true
allowed_hosts = ["api.deepseek.com"]
allowed_ports = [80, 443]
```

Для HTTPS-push в GitHub задайте `GITHUB_TOKEN`; токен используется только через временный
GIT_ASKPASS и не пишется в Git config. Если нужен GitHub Enterprise, добавьте его host в
`[github].push_hosts` (по умолчанию `["github.com"]`).

Если `providers.deepseek.base_url` задаёт другой host/port, они добавляются автоматически только для
этого endpoint. Не добавляйте произвольные хосты без необходимости. Для локального mock
используйте non-loopback имя (`127.0.0.1.nip.io`) или тестовый профиль с `egress.enabled=false`.

## 5. systemd user service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ai-supervisor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ai-supervisor.service
loginctl enable-linger "$USER"
```

`enable-linger` обязателен, если сервис должен работать после logout и без открытой сессии.

Логи:

```bash
journalctl --user -u ai-supervisor.service -f
# и файл:
tail -f ~/Projects/ai-supervisor/var/logs/ai-supervisor.log
```

Unit использует `%h/Projects/ai-supervisor`; если проект лежит в другом месте, поправьте
`WorkingDirectory`, `EnvironmentFile`, `ReadWritePaths`, `ExecStart`.

## 6. Проверка

```bash
ai-supervisor doctor
```

Ожидаемо: Python/Git/bwrap/DSH — OK; при заполненных ключах — Telegram bot и Kimi model OK.
Без ключей doctor завершится с ошибкой, но не напечатает секреты.

## 7. Первый проект

```text
/project add demo /home/you/Projects/demo
/project test "python -m pytest -q"
/project build "python -m compileall -q ."
```

Проект обязан быть Git-репозиторием, находиться внутри разрешённого корня и не отслеживать
секретные файлы. Затем:

```text
Проведи аудит README без изменений.
```

READ_ONLY-задача должна завершиться без diff. Для изменения:

```text
Добавь в README раздел «Установка» и проверь тесты.
```

Команды Phase 2: `/task`, `/task_info`, `/queue`, `/pause`, `/retry`, `/doctor`, `/push`,
`/usage week`, `/project permissions`.

После отчёта используйте кнопки или:

```text
/diff 1
/logs 1
/approve 1      # для подтверждения отката и других ожидающих действий
```

## Обновление

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
python -m pip install -e '.[dev]'
systemctl --user restart ai-supervisor.service
```

Перед обновлением рекомендуется остановить задачи и сделать backup:

```bash
scripts/backup.sh
```

## Удаление

```bash
systemctl --user disable --now ai-supervisor.service
rm -f ~/.config/systemd/user/ai-supervisor.service
```

Файлы проектов не удаляются. `var/` содержит историю и рабочие копии; удаляйте осознанно.


## Dependency lock and updates

`requirements.lock` contains the exact development environment versions generated from the
working venv. It is not hash-pinned; `pip check` and the full test suite are the current
verification gate.

```bash
python -m pip install -r requirements.lock
python -m pip check
pytest -q
AI_SUPERVISOR_RUN_INTEGRATION=1 pytest -q tests/test_worker_integration.py
```

Update one dependency at a time, regenerate the lock from a clean venv, and never run live
provider tests without an approved budget. The offline environment does not provide a
vulnerability database client (`pip-audit` is not installed), so no vulnerability-free claim is
made.
