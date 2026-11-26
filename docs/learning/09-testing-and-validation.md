# 09. Тестирование и валидация

## Концепция

Тесты делятся на детерминированные unit/integration с fake provider и opt-in live tests.
Fake provider позволяет проверять lifecycle без расходования токенов.

## Реализация

`tests/conftest.py` содержит `FakeSupervisor`, `FakeWorkers`, `FakeNotifier`, временную БД.
`tests/test_orchestrator.py` проверяет plan → worker → review → approval → merge.
`tests/test_composite.py` проверяет COMPOSITE/recovery/auth.
`tests/test_secret_leakage.py` — redaction/log/context.
`tests/test_config_migration.py` — grouped/legacy config.

Запуск:

```bash
pytest -q
ruff check .
coverage run --branch --source=src/ai_supervisor -m pytest -q
coverage report -m
AI_SUPERVISOR_RUN_INTEGRATION=1 pytest -q tests/test_worker_integration.py
AI_SUPERVISOR_RUN_LIVE_KIMI=1 pytest -q tests/test_kimi_live.py  # only with budget
```

## Шаги

1. Воспроизвести баг тестом.
2. Использовать artificial credentials.
3. Проверить happy path и отказ/тайм-аут.
4. Проверить отсутствие внешних эффектов.
5. Проверить redaction и audit.

## Вопросы

1. Почему live tests выключены по умолчанию?
2. Что должен проверять тест quota-ошибки?
3. Почему coverage не доказывает безопасность?

## Проверь себя

Найди тест, который проверяет, что empty diff не принимается; объясни, какой слой его защищает.
