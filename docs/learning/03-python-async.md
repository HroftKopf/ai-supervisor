# 03. Python async в проекте

## Концепция

`async`/`await` позволяет ждать сеть/БД/процесс, не блокируя event loop. Блокирующая операция
внутри async-функции останавливает весь бот.

## Реализация

`src/ai_supervisor/core/orchestrator.py` запускает задачи через планировщик:

```python
await self.scheduler.run(
    task.id,
    lambda: self._execute_task(
        task.id,
        project=project,
        project_context=project_context,
    ),
)
```

`WorkerScheduler` использует `asyncio.Semaphore`, чтобы ограничить число одновременных
исполнителей.

`src/ai_supervisor/memory/database.py` использует `aiosqlite` и lock вокруг операций одной
connection.

## Шаги

1. I/O — только await.
2. CPU/файловые операции — через `asyncio.to_thread`, если они действительно блокируют.
3. Subprocess — `asyncio.create_subprocess_exec`.
4. Отмена — `asyncio.CancelledError`, затем корректное завершение процесса.
5. Тайм-аут — `asyncio.timeout`/`wait_for`.

## Альтернативы

- Threads: проще для блокирующих библиотек, но сложнее отмена.
- Processes: изоляция, но дороже.

## Вопросы

1. Что случится, если вызвать `sqlite3.connect` напрямую в async handler?
2. Почему scheduler нужен semaphore?
3. Как отмена задачи доходит до worker-процесса?

## Проверь себя

Найди `WorkerScheduler.run` и `WorkerManager.run`; объясни, что произойдёт после `cancel()`.
