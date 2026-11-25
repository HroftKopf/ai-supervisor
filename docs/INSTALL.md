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
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Минимум:

```env
KIMI_API_KEY=...
KIMI_BASE_URL=https://api.kimi.ai/coding/v1
KIMI_MODEL=k3.1-pro
DEEPSEEK_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_OWNER_ID=123456789
```

`DEEPSEEK_BASE_URL` оставьте пустым для официального endpoint SDK. Если задаёте свой,
он должен быть совместим с **Messages API** (`/v1/messages`), который использует DSH 0.2.2rc2.

`config.toml`:

- `security.allowed_project_roots` — реальные корни ваших проектов.
- `limits.max_workers` — начальное значение 3.
- `limits.max_rework` — начальное значение 3.
- `sandbox.backend = "bwrap"` не меняйте на `none`.
- `usage.*` — при желании задайте цены за миллион токенов; иначе стоимость не рассчитывается.

## 4. Telegram

1. [@BotFather](https://t.me/BotFather) → `/newbot` → токен.
2. [@userinfobot](https://t.me/userinfobot) → числовой ID (не username).
3. Заполните `.env`.
4. Запустите `ai-supervisor doctor` — он проверит `getMe` и права `.env`.
5. `ai-supervisor run`.
6. Напишите `/start` боту в личном чате.

Бот использует long polling. Порты, домен, HTTPS-сертификат и публичный IP не нужны.
Работает только private chat владельца.

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
/project add demo /home/hks/Projects/demo
/project test "python -m pytest -q"
/project build "python -m compileall -q ."
```

Проект обязан быть Git-репозиторием, находиться внутри разрешённого корня и не отслеживать
секретные файлы. Затем:

```text
Добавь в README раздел «Установка» и проверь тесты.
```

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
