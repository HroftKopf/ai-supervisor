# 07. SQLite и память

## Концепция

SQLite хранит задачи, сообщения, этапы, approvals, usage и структурированную память. Это
локальная БД с WAL и одной writer-connection; долговременная память отделена от контекста
конкретной LLM-сессии.

## Реализация

`src/ai_supervisor/memory/database.py`:

```python
await conn.execute("PRAGMA journal_mode=WAL")
await conn.execute("PRAGMA foreign_keys=ON")
await conn.execute("PRAGMA busy_timeout=5000")
```

`src/ai_supervisor/memory/conversations.py` добавляет сообщения и память, redacts содержимое.
`MemoryRecord` содержит `category`, `project_id`, `source`, `verified`, `stale`, `task_id`.

## Шаги

1. Открыть соединение, FTS5.
2. Мигрировать схему с backup.
3. Сохранить сообщение/память с redaction.
4. Извлечь релевантные данные по project/conversation scope.
5. Исключить stale/unverified из доверенного контекста.
6. Ограничить размеры через `[memory]`/`[context]`.

## Альтернативы

- Postgres: лучше для параллельных writers, но тяжелее для local-first.
- Векторная БД: семантический retrieval, но сложнее и дороже.

## Вопросы

1. Почему нельзя смешивать знания проектов?
2. Чем `verified_fact` отличается от `hypothesis`?
3. Почему stale-записи исключаются из retrieval?

## Проверь себя

Найди `memory_context` и объясни, как в нём помечаются неподтверждённые факты.
