# 04. LLM API и провайдеры

## Концепция

Оркестратор зависит от нормализованного интерфейса, а не от Kimi/OpenAI. Адаптер переводит
формат провайдера в общий `ProviderResponse` и нормализует ошибки.

## Реализация

`src/ai_supervisor/providers/base.py`:

```python
class LLMProvider(Protocol):
    async def complete(self, messages, **kwargs) -> ProviderResponse: ...
    async def health(self) -> ProviderHealth: ...
```

`src/ai_supervisor/providers/registry.py` выбирает провайдера по роли:

```python
def provider_for(self, role: str) -> LLMProvider:
    name = self._roles[role]
    provider = self._providers.get(name)
    if provider is None:
        raise ProviderConfigurationError(...)
    return provider
```

`SupervisorAgent` использует `_provider("planner").complete(...)`; Kimi/OpenAI adapters
возвращают одинаковые поля: content, tool_calls, finish_reason, usage.

## Capability negotiation

Если модель не поддерживает reasoning/prompt cache, адаптер не отправляет параметр.
Kimi и OpenAI имеют разные форматы tools; структурный контракт `submit_task_spec` остаётся
общим.

## Шаги

1. `common.md` + role prompt.
2. JSON Schema tool.
3. Вызов provider.
4. Проверка tool call и Pydantic-валидация.
5. Нормализация usage/ошибок.
6. Запись provider/model/role в usage.

## Вопросы

1. Почему `provider_for` не делает fallback на другой платный API?
2. Что означает `finish_reason=length`?
3. Чем Responses API отличается от Chat Completions?

## Проверь себя

Сравни `KimiProvider` и `OpenAIProvider`: какие поля нормализуются в `ProviderResponse`?
