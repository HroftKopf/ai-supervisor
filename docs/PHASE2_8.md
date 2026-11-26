# Phase 2.8 — Provider-Agnostic Intelligence

Phase 2.8 сохраняет COMPOSITE, GitAuthManager, approvals, sandbox, egress, recovery и
Presentation Layer Phase 2.6. Управляющие LLM отделены от DeepSeek Harness, который остаётся
исполнительным backend.

## Аудит

До изменений управляющие запросы были жёстко привязаны к `KimiClient`:

- `SupervisorAgent` напрямую вызывал `self.kimi.complete`;
- `Orchestrator` записывал usage как `provider="kimi"`, `model=settings.kimi.model`;
- CLI создавал только `KimiClient`;
- сообщения об ошибках/статусе были Kimi-специфичны;
- не было реестра ролей и capability negotiation.

Фактические prompt-профили остались независимыми: `common.md` + роль; `supervisor.md` только
chat. Версии/hashes проверены; baseline 2.6.0 сохранён.

## Provider abstraction

`src/ai_supervisor/providers/base.py`:

- `LLMProvider` Protocol: `complete`, streaming flag, tools, structured function calls,
  `health`, capabilities, `close`;
- нормализованные `ProviderResponse`, `ProviderToolCall`, `TokenUsage`;
- `ProviderCapabilities`: tools, structured_output, streaming, reasoning, context_window,
  max_output_tokens, prompt_cache;
- нормализованные ошибки: authentication, permission, quota, rate limit, connection,
  protocol, capability.

Адаптеры:

- `KimiProvider` — обёртка существующего проверенного `KimiClient`, без переписывания;
- `OpenAIProvider` — официальный OpenAI Responses API (`/responses`): `instructions`, `input`,
  function tools, `max_output_tokens`, `reasoning.effort`, `prompt_cache_key`, output items,
  usage details. Реализовано накопление streaming-событий;
- `OpenAICompatibleProvider` — Chat Completions-совместимые сторонние/локальные API.

Provider-specific параметры остаются в адаптерах. `reasoning` не отправляется, если capability
выключена; внутренний `max` для OpenAI отображается в `high`.

## Роли и конфигурация

`[agent_roles]` в `config.toml` выбирает провайдера независимо для:

`chat`, `classifier`, `planner`, `reviewer`, `summarizer`, `experience`.

```toml
[providers.openai]
base_url = "https://api.openai.com/v1"
model = "gpt-4.1"
reasoning = false
prompt_cache = true  # false для endpoint без cache

[agent_roles]
default_provider = "kimi"
chat = "kimi"
classifier = "kimi"
planner = "kimi"
reviewer = "kimi"
summarizer = "kimi"
experience = "kimi"
```

Ключи только в окружении: `KIMI_API_KEY`, `OPENAI_API_KEY`,
`OPENAI_COMPATIBLE_API_KEY`. Ключи не попадают в Telegram, SQLite, логи, argv, worker env.
При отсутствии ключа выбранной роли запрос завершается ошибкой: автоматического fallback на
другой платный API нет.

DeepSeek Harness продолжает использовать `DEEPSEEK_API_KEY` и не получает управляющие ключи.

## Команды

- `/models` — роли, провайдеры, модели, доступность;
- `/provider info` — capabilities и модели без секретов;
- `/provider test [role]` — health/`models.list`, без completion;
- `/provider select <role> <provider>` — явная смена на время работы; при наличии активных
  задач отклоняется, чтобы не менять конфигурацию запущенной задачи.

Постоянная смена — через `[agent_roles]` и рестарт.

## Usage

`Orchestrator` записывает фактического провайдера и модель роли, а не Kimi по умолчанию.
Нормализованные поля: input/output/cached/reasoning, requests, duration, cost при заданном
тарифе. Оригинальные provider usage details сохраняются в `ProviderResponse.raw_usage`;
reasoning/cache не добавляются к output/input повторно.

## Typography

`presentation/formatting.py` — единые правила:

- заголовок и содержимое разделены пустой строкой;
- смысловые блоки отделены пустой строкой;
- длинные списки разделяются;
- короткие метаданные группируются;
- заголовки и ключевые понятия — `<b>`;
- стабильные Markdown-блоки кода сохраняются как `<pre><code>`;
- Markdown Kimi конвертируется безопасно, с escape до HTML.

Telegram не поддерживает CSS/margin/padding/line-height; отступы реализованы переносами строк.
Длинные ответы при риске нарушения HTML отправляются безопасным plain-text fallback.

### До/после

До (плотный технический вид):

```text
Публикация завершена · 12 мин
Файлов: 1
Этапов: 5/5
Запросов к моделям: 1
Проверки: не запускались
```

После:

```text
<b>Публикация конфигурации Niri</b>

Задача #004 · nirikfg

завершена · 12 мин

Конфигурация опубликована.

Файлов: 1
Этапов: 5/5
Запросов к моделям: 1
Проверки: не запускались

Коммит: <code>42d92f2e999f</code>
```

## Проверки

- `pytest`: **160 passed, 4 skipped**.
- `ruff check .`: без ошибок.
- DSH/bwrap/egress: **2 integration tests passed**.
- Kimi regression: существующие тесты и live-профиль сохранены.
- OpenAI: mock Responses API — tool call, structured function schema, streaming, usage,
  normalized errors, reasoning capability, no automatic fallback.
- Provider switching: independent role config, memory DB unaffected.
- Worker env: `OPENAI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY` исключены.
- Typography: snapshot-тесты отступов, Markdown, кода и лимитов Telegram.
- Реальный OpenAI запрос не выполнялся: нет согласованного ключа/бюджета.
- Live Kimi A/B остаётся заблокированным квотой 429; экономия не заявляется.

## Ограничения

- OpenAI adapter проверен на mock Responses API, не на платном live-вызове.
- `OpenAIProvider` streaming реализует базовое накопление; сложные event-сценарии не проверялись live.
- Runtime `/provider select` не сохраняется в config.toml; для постоянной конфигурации нужен рестарт.
- Активные задачи не перепланируются автоматически при смене провайдера; смена блокируется.
- Реальная Telegram-типографика требует отдельной визуальной проверки: CSS недоступен,
  unit/snapshot-тесты не заменяют просмотр в Telegram.

## Подключение OpenAI

1. Установите ключ в защищённый `.env` (не в config.toml):

```bash
OPENAI_API_KEY=...
chmod 600 .env
```

2. Укажите модель/роли:

```toml
[providers.openai]
model = "gpt-4.1"

[agent_roles]
default_provider = "openai"
chat = "openai"
classifier = "openai"
planner = "openai"
reviewer = "openai"
summarizer = "openai"
experience = "openai"
```

3. Перезапустите:

```bash
. .venv/bin/activate
ai-supervisor doctor
systemctl --user restart ai-supervisor.service
/models
/provider test planner
```

Документация Responses API: <https://developers.openai.com/api/reference/resources/responses/methods/create>.

## Откат

- Вернуть `default_provider = "kimi"` и роли `kimi` в `[agent_roles]`.
- Или `git revert` коммита Phase 2.8.
- SQLite schema v6 добавляет `usage_events.raw_usage_json`; память/задачи v5 совместимы.
- GitAuthManager, sandbox, approvals и egress не ослаблялись.
