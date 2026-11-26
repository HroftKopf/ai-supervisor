# 13. Routing и reasoning

`core/routing.py` разделяет уровни 0–3. Level 0 — native commands без LLM. `for_chat`
не повышает reasoning из-за одного слова «архитектура», `for_request` используется для
инженерных запросов, `for_spec` учитывает task type, permissions и stages.

Профили задаются в `[reasoning]`; `RoutingPolicy.reasoning()` возвращает effort/max tokens.
`SupervisorAgent` передаёт профиль в provider adapter. `finish_reason=length` не увеличивает
бюджет скрыто: структурированный вызов завершается явной ошибкой.

Ограничение: routing остаётся эвристическим; полная evaluation-матрица в разработке.

Вопрос: почему сложная формулировка не должна автоматически давать высокие permissions?
