# Проверенные API-факты

Дата проверки: 2026-11-26. Источники — официальная документация и установленный runtime.
Перед изменением провайдера перепроверяйте документацию: API может меняться.

## Kimi

### Kimi Code (Coding Plan)

- OpenAI-compatible base URL (international): `https://api.kimi.ai/coding/v1`
- China: `https://api.kimi.com/coding/v1`
- Anthropic base: `https://api.kimi.ai/coding/` (без `/v1`)
- Модели: `k3.1-pro`, `k3.1-pro-highspeed`, `k3`, `k3-256k`, `kimi-for-coding`,
  `kimi-for-coding-highspeed`.
- Ключи Kimi Code и Moonshot Open Platform **не взаимозаменяемы**.
- K3.1 Pro всегда включает thinking; `thinking` не поддерживается; `reasoning_effort`:
  `low`/`high`/`max`, default `high` для Coding API.
- При опечатке в model ID Kimi Code может молча откатиться на другую модель. Поэтому AI Supervisor
  проверяет `/models` и сверяет ID до запуска.
- Для tool calling обязательно возвращать полное assistant-сообщение, включая `reasoning_content`.
  Иначе Coding API отвечает 400 `thinking is enabled but reasoning_content is missing...`.

Официальные страницы:

- [Kimi Code Model Configuration](https://www.kimi.com/code/docs/en/kimi-code/models.html)
- [Kimi Code Error Reference](https://www.kimi.com/code/docs/en/kimi-code/error-reference.html)
- [Kimi Code Membership Benefits](https://www.kimi.com/code/docs/en/kimi-code/membership.html)

### Moonshot Open Platform

- Base URL: `https://api.moonshot.ai/v1`
- Модели: `kimi-k3.1-pro`, `kimi-k3.1-pro-highspeed`, `kimi-k3`, и старые K2.x.
- K3.1 Pro: 1M context, thinking only, `reasoning_effort` low/high/max (default `max`).
- Фиксированные параметры: `temperature=1.0`, `top_p=0.95`, `n=1`,
  `presence_penalty=0`, `frequency_penalty=0`; их нельзя передавать с другими значениями.
- `max_completion_tokens` default 131072 (до 1048576).
- `reasoning_content` передаётся обратно в assistant-сообщениях; при tool calls — обязательно.
- Usage: `prompt_tokens`, `completion_tokens`, `completion_tokens_details.reasoning_tokens`,
  `prompt_tokens_details.cached_tokens`, `prompt_tokens_details.cache_write_tokens`.
- 429: `exceeded_current_quota_error` (квота/баланс, retry бесполезен) и
  `rate_limit_reached_error`/`engine_overloaded_error` (временные, retry допустим).
- Kimi Code отдаёт квоты через 429-сообщения; документированного API остатка нет.

Официальные страницы:

- [Chat Completions](https://platform.kimi.ai/docs/api/chat)
- [Kimi K3.1 Pro Quickstart](https://platform.kimi.ai/docs/guide/kimi-k3-1-pro-quickstart)
- [Thinking Models](https://platform.kimi.ai/docs/guide/use-thinking-models)
- [Model Parameter Reference](https://platform.kimi.ai/docs/api/models-overview)
- [Errors](https://platform.kimi.ai/docs/api/errors)

### Реализация в AI Supervisor

- `openai>=2,<4`, `AsyncOpenAI`, streaming, `include_usage`.
- Не передаются `temperature`, `top_p`, `n`, penalties.
- `reasoning_effort` из `config.toml`.
- `reasoning_content` сохраняется в meta assistant-сообщений и передаётся обратно.
- Tool calls собираются из stream-дельт, аргументы валидируются Pydantic.
- Retry только для временных 429/5xx/сетевых ошибок; quota/401/403/400 — без retry.
- Модель не подменяется.

Примечание: URL `https://deepseek-harness.github.io/...` из первоначального ТЗ на момент проверки
возвращал 404. Актуальная документация SDK находится в GitHub-репозитории и в установленном пакете.

## DeepSeek Harness

### Установленная совместимость

| Компонент | Версия | Использование |
|---|---|---|
| Системный `dsh` | 0.2.2-rc.5 | не изменяется, не используется worker'ом |
| `deepseek-harness-sdk` | 0.2.2rc2 | официальный Python SDK |
| Bundled runtime | 0.2.2-rc.2 | запускается SDK |

SDK устанавливает `deepseek-harness-runtime-bin` (~97 МБ) и bundled `dsh`. Node.js системе не нужен.

### Python SDK

```python
from deepseek_harness import DeepSeekHarness

with DeepSeekHarness(
    dsh_home="/absolute/isolated/home",
    cwd="/absolute/worktree",
    profile="sdk-minimal",
    provider="deepseek-official",
    model="deepseek-v4-flash",
    max_tokens=32768,
) as harness:
    result = harness.run("task", session_id="task-001")
print(result.final_response, result.finish_reason)
```

- SDK требует явный `DSH_HOME`; `~/.dsh` сам не читает.
- `sdk-minimal` — standalone tree: persistent shell, `read_image`, `working_directory`, JSONL sessions,
  automatic compaction, danger-full-access. Поэтому обязателен внешний bwrap.
- В `sdk-minimal` нет managed credentials, GUI settings, telemetry, Web tools, subagents.
- DSH 0.2.2rc2 использует **Messages API** (`/v1/messages`, Anthropic-compatible SSE), а не
  `/v1/chat/completions`. Это важно для `DEEPSEEK_BASE_URL`.
- Session-log upload отключается patch-файлом:
  ```yaml
  - id: session-log-deepseek
    config:
      enabled: false
  ```
- `RunResult.events` содержит usage-блоки; AI Supervisor извлекает
  `inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens/reasoningTokens`.
- Отмена задачи = завершение процесса runtime; отдельного cancel RPC нет. Worker запускается
  отдельным процессом, поэтому orchestrator завершает process group.
- Resume — по `session_id` и тому же `cwd`/`DSH_HOME`.

Официальные источники:

- [DeepSeek Harness repository](https://github.com/deepseek-ai/deepseek-harness)
- [Python SDK guide (raw)](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/user/guide/python-sdk.md)
- Установленная копия: `docs/user/guide/python-sdk.md` в npm-пакете `@deepseek-ai/dsh`.
