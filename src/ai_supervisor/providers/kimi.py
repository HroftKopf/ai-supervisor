from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.models import TokenUsage

logger = logging.getLogger(__name__)

QUOTA_MARKERS = (
    "quota",
    "usage limit",
    "credit balance",
    "insufficient",
    "insufficient_quota",
    "exceeded_current_quota",
    "extra usage",
    "monthly",
    "weekly",
    "5-hour",
    "billing",
    "balance",
)
TRANSIENT_MARKERS = (
    "concurrent",
    "too many requests",
    "overloaded",
    "temporarily",
    "try again",
    "rate limit",
    "engine",
    "timeout",
    "connection reset",
)


class KimiError(RuntimeError):
    """Base class for sanitized, user-presentable Kimi failures."""


class KimiConfigurationError(KimiError):
    pass


class KimiAuthenticationError(KimiError):
    pass


class KimiPermissionError(KimiError):
    pass


class KimiModelError(KimiError):
    pass


class KimiQuotaError(KimiError):
    pass


class KimiRateLimitError(KimiError):
    pass


class KimiConnectionError(KimiError):
    pass


class KimiProtocolError(KimiError):
    pass


class KimiToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class KimiResponse(BaseModel):
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[KimiToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""
    request_id: str | None = None

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


@dataclass(slots=True)
class KimiClientConfig:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str = "high"
    max_completion_tokens: int = 32768
    request_timeout_seconds: float = 180.0
    max_retries: int = 3
    max_concurrent_requests: int = 2
    retry_base_seconds: float = 1.0


class KimiClient:
    def __init__(self, config: KimiClientConfig, redactor: SecretRedactor | None = None) -> None:
        self.config = config
        self.redactor = redactor or SecretRedactor([config.api_key])
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url.rstrip("/"),
            timeout=config.request_timeout_seconds,
            max_retries=0,
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)

    async def close(self) -> None:
        await self._client.close()

    async def validate_model(self) -> list[str]:
        """Verify the configured model is actually offered. Never substitutes a model."""
        try:
            async with self._semaphore, asyncio.timeout(self.config.request_timeout_seconds):
                response = await self._client.models.list()
        except openai.AuthenticationError as exc:
            raise KimiAuthenticationError(
                "Kimi отклонил API-ключ (401). Проверьте, что ключ и base URL относятся к одной платформе."
            ) from exc
        except openai.APIStatusError as exc:
            if exc.status_code in {401, 403}:
                raise KimiAuthenticationError(
                    f"Kimi не авторизовал запрос ({exc.status_code}). Проверьте ключ и доступ к модели."
                ) from exc
            raise KimiModelError(
                f"Не удалось получить список моделей Kimi (HTTP {exc.status_code}); "
                "проверка доступности модели невозможна, запуск остановлен."
            ) from exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise KimiConnectionError("Нет связи с API Kimi при проверке модели.") from exc
        models = [
            str(item.id) for item in getattr(response, "data", []) if getattr(item, "id", None)
        ]
        if not models:
            raise KimiModelError(
                "API Kimi вернул пустой список моделей; проверка модели невозможна."
            )
        if self.config.model not in models:
            available = ", ".join(sorted(models)[:20])
            raise KimiModelError(
                f"Модель {self.config.model!r} не найдена у провайдера. Доступные: {available}. "
                "Система не подменяет модель автоматически."
            )
        return models

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_completion_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        prompt_cache_key: str | None = None,
        purpose: str = "chat",
    ) -> KimiResponse:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": max_completion_tokens or self.config.max_completion_tokens,
            "reasoning_effort": self.config.reasoning_effort,
        }
        if tools:
            kwargs["tools"] = list(tools)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if prompt_cache_key:
            kwargs["extra_body"] = {"prompt_cache_key": prompt_cache_key}

        attempts = self.config.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self._semaphore:
                    response = await self._stream_once(kwargs, purpose=purpose)
                return response
            except KimiQuotaError:
                raise
            except (KimiRateLimitError, KimiConnectionError) as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                delay = self._retry_delay(attempt, exc)
                logger.warning(
                    "Kimi retry %s/%s in %.1fs (%s)",
                    attempt,
                    attempts - 1,
                    delay,
                    exc.__class__.__name__,
                )
                await asyncio.sleep(delay)
            except (
                KimiAuthenticationError,
                KimiPermissionError,
                KimiModelError,
                KimiProtocolError,
            ):
                raise
        raise KimiConnectionError(f"Kimi недоступен после повторных попыток: {last_error}")

    async def _stream_once(self, kwargs: dict[str, Any], *, purpose: str) -> KimiResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage = TokenUsage()
        model = str(kwargs["model"])
        received_any = False
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                stream = await self._client.chat.completions.create(**kwargs)
                try:
                    async for chunk in stream:
                        if chunk.usage is not None:
                            usage = _parse_usage(chunk.usage)
                        model = str(getattr(chunk, "model", None) or model)
                        choices = getattr(chunk, "choices", None) or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = getattr(choice, "delta", None)
                        if delta is not None:
                            reasoning = _get(delta, "reasoning_content")
                            if reasoning:
                                reasoning_parts.append(str(reasoning))
                                received_any = True
                            text = _get(delta, "content")
                            if text:
                                content_parts.append(str(text))
                                received_any = True
                            for tool_delta in _get(delta, "tool_calls") or []:
                                received_any = True
                                index = int(_get(tool_delta, "index") or 0)
                                entry = tool_parts.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                call_id = _get(tool_delta, "id")
                                if call_id:
                                    entry["id"] += str(call_id)
                                function = _get(tool_delta, "function")
                                if function is not None:
                                    name = _get(function, "name")
                                    if name:
                                        entry["name"] += str(name)
                                    arguments = _get(function, "arguments")
                                    if arguments:
                                        entry["arguments"] += str(arguments)
                        reason = _get(choice, "finish_reason")
                        if reason:
                            finish_reason = str(reason)
                finally:
                    close = getattr(stream, "close", None)
                    if close is not None:
                        maybe = close()
                        if asyncio.iscoroutine(maybe):
                            await maybe
        except KimiError:
            raise
        except openai.APIStatusError as exc:
            if received_any:
                raise KimiProtocolError(
                    "Kimi вернул ошибку после начала потока; повторный запрос не отправлялся, "
                    "чтобы не дублировать действия."
                ) from exc
            raise self._map_status_error(exc) from exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            if received_any:
                raise KimiProtocolError(
                    "Поток Kimi оборвался после начала ответа; повторный запрос не отправлялся, "
                    "чтобы не дублировать действия."
                ) from exc
            raise KimiConnectionError("Сетевая ошибка или тайм-аут при обращении к Kimi.") from exc
        except TimeoutError as exc:
            if received_any:
                raise KimiProtocolError(
                    "Поток Kimi не завершился вовремя после начала ответа."
                ) from exc
            raise KimiConnectionError("Тайм-аут ожидания ответа Kimi.") from exc

        calls = [
            KimiToolCall(
                id=value["id"] or f"call_{index}",
                name=value["name"],
                arguments=value["arguments"] or "{}",
            )
            for index, value in sorted(tool_parts.items())
            if value["name"]
        ]
        if finish_reason is None and not content_parts and not reasoning_parts and not calls:
            raise KimiProtocolError("Kimi вернул пустой поток без завершения.")
        return KimiResponse(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            tool_calls=calls,
            finish_reason=finish_reason,
            usage=usage,
            model=model,
        )

    def _map_status_error(self, exc: openai.APIStatusError) -> KimiError:
        status = exc.status_code
        message = _error_message(exc)
        lowered = message.lower()
        if status in {401}:
            return KimiAuthenticationError(
                "Kimi отклонил API-ключ (401). Проверьте ключ и соответствие платформы base URL."
            )
        if status == 403:
            return KimiPermissionError(
                "Kimi не разрешил доступ к модели (403). Проверьте тариф/права аккаунта. "
                f"Ответ: {message[:500]}"
            )
        if status == 404:
            return KimiModelError(f"Kimi не нашёл endpoint или модель (404): {message[:500]}")
        if status == 429:
            if any(marker in lowered for marker in QUOTA_MARKERS):
                return KimiQuotaError(
                    "Квота Kimi исчерпана (429). Запросы приостановлены; проверьте лимиты аккаунта. "
                    f"Ответ: {message[:500]}"
                )
            retry_after = _retry_after(exc)
            error = KimiRateLimitError(f"Временный rate limit Kimi (429): {message[:500]}")
            if retry_after is not None:
                error.args = (error.args[0] + f" Retry-After={retry_after}s",)
                error.retry_after = retry_after
            return error
        if status == 400:
            if "reasoning_content" in lowered:
                return KimiProtocolError(
                    "Kimi требует передать reasoning_content в сообщении ассистента при tool calling. "
                    "Запрос не повторялся."
                )
            return KimiProtocolError(f"Kimi отклонил запрос (400): {message[:800]}")
        if status in {500, 502, 503, 504}:
            return KimiConnectionError(
                f"Сервер Kimi временно недоступен ({status}): {message[:500]}"
            )
        return KimiError(f"Ошибка Kimi HTTP {status}: {message[:800]}")

    def _retry_delay(self, attempt: int, error: Exception) -> float:
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return min(float(retry_after), 60.0)
        base = self.config.retry_base_seconds * (2 ** (attempt - 1))
        return min(base + random.uniform(0, 0.4), 30.0)


def _get(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(key)
    direct = getattr(value, key, None)
    if direct is not None:
        return direct
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, Mapping):
        return extra.get(key)
    return None


def _parse_usage(value: Any) -> TokenUsage:
    prompt_details = _get(value, "prompt_tokens_details") or {}
    completion_details = _get(value, "completion_tokens_details") or {}
    return TokenUsage(
        input_tokens=int(_get(value, "prompt_tokens") or 0),
        output_tokens=int(_get(value, "completion_tokens") or 0),
        cached_tokens=int(_get(prompt_details, "cached_tokens") or 0),
        cache_write_tokens=int(_get(prompt_details, "cache_write_tokens") or 0),
        reasoning_tokens=int(_get(completion_details, "reasoning_tokens") or 0),
        requests=1,
    )


def _error_message(exc: openai.APIStatusError) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error)
        if error:
            return str(error)
        return str(body)
    return str(getattr(exc, "message", None) or exc)


def _retry_after(exc: openai.APIStatusError) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def structured_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
            "strict": True,
        },
    }


def parse_tool_call(response: KimiResponse, expected_name: str) -> dict[str, Any]:
    if not response.tool_calls:
        raise KimiProtocolError(f"Kimi не вызвал обязательный инструмент {expected_name!r}.")
    call = next((item for item in response.tool_calls if item.name == expected_name), None)
    if call is None:
        names = ", ".join(item.name for item in response.tool_calls)
        raise KimiProtocolError(
            f"Kimi вызвал неожиданный инструмент ({names}); ожидался {expected_name!r}."
        )
    try:
        value = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as exc:
        raise KimiProtocolError(
            f"Kimi вернул некорректный JSON аргументов инструмента: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise KimiProtocolError("Аргументы инструмента Kimi должны быть JSON-объектом.")
    return value
