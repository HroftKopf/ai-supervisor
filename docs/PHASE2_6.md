# Phase 2.6 — Presentation Layer, prompts и routing

Phase 2.6 сохраняет COMPOSITE, GitAuthManager, approvals, sandbox, egress и recovery из 2.5.1
и добавляет компактный интерфейс, живую карточку задачи, режимы отображения, версионированные
роли промптов, адаптивную маршрутизацию и evaluation framework.

## Интерфейс

Presentation Layer находится в `src/ai_supervisor/presentation/`:

- `renderer.py` — TaskCard, ProgressCard, CompletionCard, ErrorCard, ApprovalCard,
  ProjectCard, UsageCard, StatusCard, SettingsCard, DetailsView;
- `progress.py` — одна редактируемая карточка на задачу, `message_id` в SQLite, throttling,
  восстановление после restart, fallback при удалённом сообщении.

Все динамические значения экранируются для Telegram HTML. Карточки ограничены 3800 символами;
обычные сообщения — настройкой `telegram.max_message_chars`. HTML не используется как замена
нативным кнопкам: применяются inline-кнопки, ссылки и edit_message_text.

Пример прогресса:

```text
<b>Публикация конфигурации Niri</b>
Задача #004 · nirikfg

Выполняется · 3/5 этапов

✓ Импорт конфигурации
✓ Создание коммита
✓ Валидация
→ Подготовка репозитория
○ Публикация
```

Пример завершения:

```text
<b>Публикация конфигурации Niri</b>
Задача #004 · nirikfg

завершена · 12 мин 4 сек

Конфигурация опубликована.

Файлов: 1
Этапов: 5/5
Запросов к моделям: 1
Проверки: не запускались
Коммит: <code>42d92f2e999f</code>
```

Кнопки: «Открыть репозиторий», «Подробности», «Расход токенов», «Журнал».
«Подробности» показывает исходный запрос, план, этапы, критерии, доказательства, коммиты,
проверки, secret-scan, ошибки и ограничения; для больших данных используется Markdown-файл.

Ошибки по умолчанию не показывают traceback: сохраняются техническая ошибка и человекочитаемая
категория (API/полномочия/данные/временный сбой/реализация/отказ/бюджет). Approvals используют
единый ApprovalCard и сохраняют прежние привязки к source hash, repository, branch и commit.

### Режимы

`/settings` переключает `compact` (по умолчанию), `detailed`, `debug`. Режим хранится в
`chat_state.ui_mode`; debug не раскрывает credentials. `/start` показывает интерактивное меню:
проекты, задачи, история, API, состояние, настройки. Свободный текст остаётся основным входом.

### Хранение message_id

В `tasks` добавлены `ui_chat_id`, `ui_message_id`, `ui_hash`, `ui_updated_at` (schema v4).
При restart сообщение восстанавливается по этим полям; если edit невозможен — отправляется
новая карточка и id обновляется. Частые обновления объединяются throttling-очередью.

## Prompt architecture

Активные профили: `supervisor`, `classifier`, `planner`, `reviewer`, `summarizer`, `worker`.
Каждый запрос получает только свой профиль; общий supervisor используется только для chat.
Версия и hashes — в `src/ai_supervisor/prompts/manifest.json`. Baseline 2.5.1 сохранён в
`prompts/baseline/2.5.1/`. Версия и hashes попадают в plan event/usage.

Изменения промптов: classifier выделен из supervisor, planner получил правила минимальности,
reviewer — blocking/warning/suggestion, worker — требование короткого структурированного отчёта.
Baseline сохранён для отката.

## Routing и reasoning

`RoutingPolicy`:

- Level 0 — существующие детерминированные команды/инструменты;
- Level 1 — простые READ_ONLY/EXECUTE/GIT задачи;
- Level 2 — обычные MODIFY/COMPOSITE;
- Level 3 — архитектура, безопасность, high-risk/COMPOSITE с внешними операциями.

`routing` в `config.toml` задаёт effort/max tokens для 1/2/3 и classifier. Kimi client принимает
per-request `reasoning_effort` и `max_completion_tokens`; глобальный профиль больше не
применяется ко всем запросам. При `finish_reason=length` структурированный запрос один раз
получает увеличенный бюджет, затем возвращает ошибку без бесконечного цикла.

## Context / retry

Планировщик получает ограниченный project snapshot, историю в пределах memory limits, цель и
роль. Worker получает ТЗ, проект и предыдущие замечания; повтор не перепланирует задачу и
использует сохранённый план. Context size фиксируется в usage (`context_chars`), роли —
`classification/planning/chat/review/analysis`.

## Prompt Evaluation

`evaluation/scenarios.json` — 24 сценария (read_only, modify, git, composite, bugfix, feature,
refactor, permissions, contradictions, already_done, unsupported, injection). Harness
`src/ai_supervisor/evaluation/harness.py` проверяет тип, criteria, stages, tools, dependencies.
`scripts/evaluate_prompts.py --live` запускает active/baseline на Kimi только при явном
разрешении владельца. Offline режим проверяет только набор сценариев и не выдаёт себя за
измерение модели.

## Фактические результаты

- `pytest`: **164 passed, 4 skipped**.
- `ruff check .`: без ошибок.
- DSH/bwrap/egress: **2 integration tests passed**.
- `ai-supervisor doctor`: GitHub auth и egress OK.
- SQLite migration v3→v4: `ui_*` columns и `ui_mode`; backup `pre-migration-v4-*.db`.
- Задача #004 реально завершена: remote `origin/main` = `42d92f2e999f`; итоговый отчёт содержит
  5/5 этапов, commit hash и remote SHA. Устаревшая auth-ошибка из отчёта удалена.
- Prompt Evaluation: 24 сценария, offline harness — passed.
- Kimi live A/B **не выполнен** повторно: 5-часовая квота вернула 429. Поэтому снижение
  токенов на 30–50% **не заявляется**. Ранее live chat/classify/plan/review и composite plan
  проходили.

## Ограничения

- Параллельного выполнения независимых этапов нет; DAG исполняется последовательно.
- Полная библиотека шаблонов планов и автоматическое накопление подтверждённого опыта пока
  не реализованы; используется planner + существующая memory.
- A/B с реальными токенами не завершён из-за квоты; baseline/optimized файлы и harness готовы.
- Реальная Telegram-проверка не выполнялась; presentation-тесты используют fake notifier.
- Live Kimi и платный DeepSeek требуют отдельного запуска.

## Обновление и откат

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
python -m pip install -e '.[dev]'
scripts/backup.sh
ai-supervisor doctor
systemctl --user restart ai-supervisor.service
```

При первом запуске будет создан `var/backups/pre-migration-v4-*.db`. Для отката интерфейса
верните предыдущий коммит; для отката промптов скопируйте файлы из
`src/ai_supervisor/prompts/baseline/2.5.1/` в активные и перезапустите сервис.
