# Prompt profiles

Каждая роль использует общий `common.md` плюс только свой профиль. `supervisor.md` подключён
только к chat. Версия и baseline зафиксированы в `manifest.json`; active 2.7.0; baseline-копии лежат в
`baseline/2.6.0/` (и историческая `baseline/2.5.1/`).

- `supervisor.md` — общая роль, стиль и безопасность;
- `classifier.md` — только классификация;
- `planner.md` — ТЗ и COMPOSITE DAG;
- `reviewer.md` — ревью и severity;
- `worker.md` — инструкции исполнителя;
- `summarizer.md` — сжатие истории.

При изменении промпта увеличьте `version` в manifest и сохраните предыдущую версию в baseline.
