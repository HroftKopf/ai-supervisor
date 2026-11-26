# 06. DAG и планировщик

## Концепция

COMPOSITE-задача состоит из этапов с зависимостями. DAG проверяется до исполнения, чтобы
не запускать этап с отсутствующим предком. Текущая реализация исполняет этапы
последовательно, но сохраняет состояние каждого этапа.

## Реализация

`src/ai_supervisor/memory/stages.py` хранит `task_stages` и проверяет граф.
`src/ai_supervisor/core/orchestrator.py::_execute_composite` выбирает готовые этапы.

```python
for stage in await self.stages.list(task.id):
    if stage.status in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
        continue
    # dependencies checked before executor dispatch
```

`WorkerScheduler` ограничивает параллельность задач, а не этапов.

## Шаги

1. Planner создаёт `stages` с `depends_on`.
2. Validator проверяет уникальность id, отсутствие циклов и известные операции.
3. Каждый этап получает статус и результат.
4. После падения завершённые этапы сохраняются.
5. Повтор начинается с незавершённого этапа.

## Альтернативы

- Последовательный workflow: проще, но медленнее.
- Полный DAG scheduler: быстрее, но нужны locks, integration и recovery (см. Phase 3 doc).

## Вопросы

1. Почему нельзя просто выполнить этапы в порядке массива?
2. Что означает `depends_on`?
3. Какие этапы можно безопасно выполнять параллельно?

## Проверь себя

Открой `docs/PHASE3_ARCHITECTURE.md` и назови три блокировки, необходимые для параллельного DAG.
