# 17. Архитектурный рефакторинг

`orchestrator.py` остаётся крупным; в Phase 2.9.3 выделены отдельные `RequestEnvelope`,
`EvidenceManifest`, `ProviderStateStore`, `FaultInjector`. Это только начало декомпозиции.

Правильный следующий шаг — characterization tests, затем извлечение сервисов
Request/Planning/Execution/Review/Recovery с явными зависимостями. Нельзя переносить весь
Orchestrator в один новый giant service или давать сервису ссылку на весь Orchestrator.

Ограничение: milestone E (декомпозиция) не завершён.

Вопрос: почему уменьшение количества строк само по себе не является архитектурным улучшением?
