# 14. Context Engine и retrieval

Контекст собирается в `core/context.py`, `memory/conversations.py` и
`core/request_gateway.py`. `ProjectContextBuilder` читает файлы в границах project root,
редактирует секреты и ограничивает размер.

Память: `memory_context(..., query=...)` сначала ищет FTS/LIKE кандидатов, затем фильтрует
project/conversation scope и stale. Это исправляет выборку «последние 100» с фильтрацией после
LIMIT. Полноценные typed fragments, per-role budgets и обязательные retention-правила пока не
реализованы; это отражено в отчёте.

Вопрос: почему контекст из README не должен становиться system instruction?
