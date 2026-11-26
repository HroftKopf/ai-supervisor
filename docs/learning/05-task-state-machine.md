# 05. Конечный автомат задачи

## Концепция

Состояние задачи — это не поле, которое может менять кто угодно. Переходы валидируются,
чтобы не запустить worker для завершённой задачи или не принять отменённую.

## Реализация

`src/ai_supervisor/models.py`:

```python
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PLANNING: frozenset({TaskStatus.QUEUED, ...}),
    ...
}

def ensure_transition(current: TaskStatus, new: TaskStatus) -> None:
    if new not in TRANSITIONS[current]:
        raise TransitionError(...)
```

`src/ai_supervisor/memory/tasks.py::transition` читает текущий статус и выполняет переход в
транзакции с проверкой.

## Шаги

1. Создание `CREATED`.
2. `PLANNING` — planner.
3. `QUEUED`/`RUNNING` — executor.
4. `VALIDATING`/`REVIEWING`.
5. `WAITING_APPROVAL`/`REWORK`.
6. `COMPLETED`, `BLOCKED`, `PAUSED`, `CANCELLED`, `INTERRUPTED`.

## Альтернативы

- Свободные статусы: проще, но легко получить противоречивое состояние.
- Event sourcing: мощнее, но избыточно для текущего масштаба.

## Вопросы

1. Почему `INTERRUPTED` восстанавливается иначе, чем `COMPLETED`?
2. Почему нельзя перейти из `COMPLETED` в `RUNNING`?
3. Где гарантируется атомарность перехода?

## Проверь себя

Найди все переходы из `WAITING_APPROVAL` и объясни, какой из них требует approval.
