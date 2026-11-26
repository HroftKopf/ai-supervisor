# 11. Отказоустойчивость и hardening

## Race condition

**Проблема:** два обработчика одновременно читают `QUEUED` и оба запускают worker.
**В проекте:** `Orchestrator` использует per-task `asyncio.Lock` (`_action_locks`) и
`TaskService.transition` проверяет текущий статус в транзакции.
**Код:** `_locked_action` в `core/orchestrator.py`, `Database.transaction` в `memory/database.py`.

Вопрос: почему проверка статуса до транзакции недостаточна?

## Атомарность и транзакции

**Проблема:** задача переведена в `RUNNING`, но событие/worker не сохранены.
**В проекте:** `Database.transaction` выполняет `BEGIN IMMEDIATE`, commit/rollback.
**Код:** `memory/database.py`.

Вопрос: что произойдёт, если исключение возникнет между двумя `execute` внутри транзакции?

## Идемпотентность

**Проблема:** supervisor упал после успешного `git push`, но до записи stage result. Повтор
может сделать второй push.
**В проекте:** `_stage_publish` сначала читает remote SHA; если `remote_sha == local`, этап
возвращает `already_published=True` и не делает push. Fault hook `after_git_push` моделирует
падение сразу после внешнего действия.
**Код:** `Orchestrator._stage_publish`, `core/fault_injection.py`.

Вопрос: какие ещё внешние действия должны проверять фактическое состояние перед повтором?

## Процесс и дочерний процесс

**Проблема:** worker запускает инструменты; при отмене нужно завершить всю группу, не задеть
процессы владельца.
**В проекте:** worker запускается с `start_new_session=True`; завершение — `os.killpg`;
`WorkerManager._terminate`.
**Код:** `core/worker_manager.py`, `tests/test_worker_lifecycle.py`.

Вопрос: почему `kill(pid)` недостаточен для дочерних процессов?

## SIGTERM и SIGKILL

**Проблема:** SIGTERM может быть проигнорирован; SIGKILL не даёт очистить ресурсы.
**В проекте:** сначала SIGTERM и ожидание, затем SIGKILL; timeout и cancel используют этот
порядок. Временные файлы/процессы проверяются тестами.
**Код:** `WorkerManager.run`, `WorkerManager.cancel`.

Вопрос: почему SIGKILL не должен быть первым шагом?

## Circuit breaker

**Проблема:** после quota/429 бесконечные повторы сжигают квоту.
**В проекте:** `ProviderRegistry` quota gate блокирует роль/провайдера; `/provider test`
выполняет health check и снимает блокировку только при успехе. Retry ограничен и использует
backoff+jitter/Retry-After.
**Код:** `providers/registry.py`, `providers/kimi.py`.

Вопрос: почему нельзя автоматически переключиться на другой платный API?

## Fault injection

**Проблема:** обычный happy-path тест не проверяет падение между внешним действием и записью
результата.
**В проекте:** `FaultInjector` с именованными точками, opt-in через
`AI_SUPERVISOR_FAULT_POINTS`; тесты используют синтетические точки.
**Код:** `core/fault_injection.py`, `tests/test_fault_injection.py`.

Вопрос: какие точки должны быть до и после Git push?

## Unit и integration tests

**Unit:** проверяют одну функцию на fake данных (например, `ProviderRegistry`).
**Integration:** проверяют взаимодействие (БД + Orchestrator + fake provider/worker).
**В проекте:** `tests/conftest.py` fake provider/notifier; `tests/test_composite.py` end-to-end;
`tests/test_worker_integration.py` real DSH/bwrap.
**Вопрос:** почему mock-тест не доказывает, что bwrap действительно изолирует?

## Supply chain security

**Проблема:** зависимость может быть подменена или содержать уязвимость.
**В проекте:** зависимости объявлены в `pyproject.toml` с диапазонами; перед обновлением —
`pip check`, тесты, DSH integration, проверка lockfile/artifact policy (пока не реализован
полный lock). Не обновляйте всё вслепую.
**Вопрос:** что даёт hash-pinned lockfile и чего он не гарантирует?

## Итог

Безопасность и восстановление обеспечиваются комбинацией Python-проверок, БД-транзакций,
sandbox и тестов на отказы. Инструкции модели не являются границей безопасности.
