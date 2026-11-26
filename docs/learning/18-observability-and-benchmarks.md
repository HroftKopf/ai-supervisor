# 18. Observability и benchmarks

Наблюдаемость: `usage_events` (provider/model/role/request/attempt/cache/reasoning/cost),
`tasks`/`task_stages`/`worker` events, `provider_states`, `EvidenceManifest`, structured logs с
redaction.

Offline benchmark: `benchmarks/run_offline.py` измеряет memory search, context build, state
transitions, scheduler, recovery и peak memory на временной БД. Это не измерение качества LLM.
`coverage run --branch` измеряет statement/branch отдельно.

Вопрос: почему mock-бенчмарк не доказывает снижение token cost?
