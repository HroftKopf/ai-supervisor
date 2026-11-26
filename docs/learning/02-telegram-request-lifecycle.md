# 02. Жизненный цикл Telegram-запроса

## Концепция

Telegram присылает обновления через long polling. aiogram маршрутизирует их в handler.
Middleware проверяет владельца и дубликаты, handlers не должны содержать бизнес-логику
жизненного цикла задачи.

## Реализация

`src/ai_supervisor/telegram/auth.py`:

```python
class OwnerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.id != self.settings.owner_id:
            return None
        return await handler(event, data)
```

`src/ai_supervisor/telegram/handlers.py`:

```python
async def cmd_usage(self, message: Message) -> None:
    text = await self.orchestrator.usage_text(message.chat.id)
    await self.notifier.send_text(message.chat.id, text, parse_mode="HTML")
```

`Notifier` — интерфейс; `BotNotifier` добавляет Telegram-specific HTML/limits/redaction.
Fake notifier используется в тестах.

## Шаги

1. `DedupeMiddleware` не даёт обработать один update дважды.
2. `OwnerMiddleware` отсекает чужие ID/группы.
3. Handler разбирает команду.
4. Orchestrator выполняет детерминированную операцию или задачу.
5. Presentation Layer форматирует сообщение.
6. `parse_mode` передаётся только для HTML/Markdown.

## Альтернативы

- Webhook вместо polling: ниже задержка, но нужен публичный TLS endpoint.
- Реализовывать бизнес-логику в handlers: быстрее, но невозможно тестировать без Telegram.

## Вопросы

1. Почему owner проверяется в middleware, а не по тексту команды?
2. Что произойдёт при повторном update?
3. Почему `/usage` использует Presentation Layer?

## Проверь себя

Найди `cmd_usage` и `usage_text`; объясни, где формируется HTML и почему `parse_mode` обязателен.
