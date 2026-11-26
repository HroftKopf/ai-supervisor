# Prompt profiles

Каждая роль использует только свой профиль плюс общий `supervisor.md`. Версия и baseline
зафиксированы в `manifest.json`; baseline-копии лежат в `baseline/2.5.1/`.

- `supervisor.md` — общая роль, стиль и безопасность;
- `classifier.md` — только классификация;
- `planner.md` — ТЗ и COMPOSITE DAG;
- `reviewer.md` — ревью и severity;
- `worker.md` — инструкции исполнителя;
- `summarizer.md` — сжатие истории.

При изменении промпта увеличьте `version` в manifest и сохраните предыдущую версию в baseline.
