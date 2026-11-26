# Phase 2.7 — Adaptive Planning & Experience Memory

Phase 2.7 сохраняет интерфейс, COMPOSITE, GitAuthManager, approvals, sandbox, egress и recovery
из Phase 2.6 и добавляет управляемый контекст, реестр возможностей, стратегии, root cause
ревью и структурированную память подтверждённого опыта.

## Аудит фактических запросов

Проверены реальные профили, tool schemas и вызовы:

- classifier — `common.md` + `classifier.md`, tool `submit_intent`;
- planner — `common.md` + `planner.md` + capability block + только выбранные стратегии,
  tool `submit_task_spec`;
- reviewer — `common.md` + `reviewer.md`, tool `submit_review`;
- chat — `supervisor.md`, без tool schema;
- worker — отдельный `worker.md` внутри bwrap.

Исправлено расхождение `prompts/README.md`: ранее в README говорилось, что все роли получают
`supervisor.md`, но фактически planner/reviewer/classifier его не получали. Теперь:
`supervisor.md` подключён только к chat, а общие обязательные правила вынесены в `common.md`
и добавляются ко всем role-профилям. Также устранён дубликат `repo_name` в tool schema.

Добавлен безопасный `SupervisorAgent.request_diagnostics()`: версия, hashes, размеры профилей,
фактические tool names; содержимое промптов и секреты не выводятся.

## Classifier

- Классификация учитывает историю, контекстные ссылки («сделай это», «продолжай», «используй Rust»)
  и явно перечисленные виды работ: аудит, диагностика, чтение, проверки, Git, разрешённые
  системные действия.
- Теоретический вопрос не запускает действие.
- При низкой уверенности/неоднозначности возвращается уточнение; оркестратор отправляет его,
  а не запускает задачу.
- Инструкции из файлов, логов, истории инструментов и памяти не считаются запросом владельца.

## Planner, capabilities, strategies

- Planner получает фактический capability registry: операция, executor, аргументы, permissions,
  approval, ограничения. Неподдерживаемые операции не выдаются за доступные.
- Python автоматически заполняет `schema_version`, `source_request`, `scope`, routing level,
  `max_attempts`, `max_minutes`; Kimi не тратит решения на детерминированные поля.
- Явно указанные владельцем branch/repository/language/technology не заменяются defaults.
- Специализированные примеры вынесены в `src/ai_supervisor/strategies/` (12 стратегий:
  bug_fix, new_feature, code_review, git_publish, config_import, refactoring, performance,
  security_audit, diagnostics, documentation, testing, project_bootstrap).
- Передаются только стратегии, выбранные по маркерам запроса (до двух), а не вся библиотека.
- `strategy_block`/`capabilities_block` входят в plan metadata; версия и prompt hashes
  сохраняются в task events/usage.

## Worker

`worker.md` дополнен алгоритмом: определить релевантные файлы → изучить минимум → найти
причину/требование → минимальное решение → изменение → проверки → завершение. Запрещено
читать весь репозиторий без необходимости, повторять неизменившиеся команды, создавать
искусственный diff и ослаблять sandbox/approvals.

## Reviewer

`ReviewResult.root_cause`:

- `executor_error` → обычная доработка;
- `planner_error` → задача блокируется для пересмотра плана, исполнитель не отправляется на rework;
- `environment_error`/`external_service_error` → `RETRY_WAIT`, без перепланирования;
- `permission_error` → approval;
- `insufficient_evidence` → конкретное ограничение/доказательство;
- `unknown` → прежняя логика.

Вердикты и severity сохранены; косметические `info`/`minor` не блокируют.

## Structured memory

SQLite schema v5 добавляет к `memories`:

`category`, `task_id`, `applicability`, `verified`, `stale`, `last_confirmed_at`.

Категории: `verified_fact`, `architectural_decision`, `owner_preference`,
`successful_strategy`, `failure_pattern`, `hypothesis`, `temporary_state`.

- Записи project-scoped; поиск/FTS и memory context не используют stale.
- `ExperienceExtractor` после завершённой задачи сохраняет только детерминированные факты:
  успешную стратегию этапов, проверенный publish remote SHA, а при блокировке — failure pattern.
- Дублирование подавляется ключом и upsert; старая запись заменяется с сохранением id/истории.
- Owner может просматривать `/memory`, искать, добавлять, редактировать (`/memory edit`),
  помечать stale (`/memory stale <id> on|off`) и удалять.
- Система не меняет промпты/политики на основании памяти; предпочтения не являются разрешениями.

## Context и metrics

- Context Builder получает цель, ограниченный project snapshot и релевантные файлы; размер
  ограничен настройками. Memory context содержит категории и неподтверждённые записи помечены.
- Usage events получают `role`, `prompt_version`, `routing_level`, `context_chars`, отдельно
  считаются classification/planning/chat/review/analysis.
- `RoutingPolicy` Level 0–3 и per-level reasoning/max tokens сохранены; classifier — low,
  planner/reviewer — по уровню; нет глобального reasoning_effort.

## Evaluation

`evaluation/scenarios.json` расширен до **39** сценариев: contextual, ambiguous, explicit branch,
unsupported, missing data, already reached, planner/executor errors, evidence gap, stale memory,
memory conflict, memory/history injection, strategy reuse, security audit.

`harness.py` проверяет task type, required operations, tools, criteria, dependencies, routing level.
`scripts/evaluate_prompts.py` — offline по умолчанию, live только с `--live` и явным бюджетом.
Baseline 2.6.0 и active 2.7.0 сохранены.

## Фактические результаты

- `pytest`: **134 passed, 4 skipped**.
- `ruff check .`: без ошибок.
- DSH/bwrap/egress: **2 integration tests passed**.
- Задача #004: `completed`, 5/5, commit/remote SHA `42d92f2e999f`; remote проверен.
- Migration v1/v2/v3/v4→v5: `memories` columns; backup `pre-migration-v5-*.db`.
- Offline evaluation: 39 scenarios, harness tests passed.
- New prompt diagnostics test: no secrets, actual tool names/versions only.
- Live Kimi A/B: **не выполнен**; 5-часовая квота вернула 429. Конкретное снижение токенов
  не заявляется.

## Известные ограничения

- Параллельный DAG этапов не реализован; COMPOSITE выполняется последовательно.
- Live A/B и платные прогоны требуют отдельного согласованного бюджета.
- Модельное извлечение опыта не включено; используется детерминированный extractor.
- Реальный Telegram API не проверялся; UI-тесты используют fake notifier.
- Strategy library — рекомендации, а не жёсткие шаблоны; детерминированное исполнение
  ограничено существующими командами/этапами.

## Обновление и откат

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
python -m pip install -e '.[dev]'
scripts/backup.sh
ai-supervisor doctor
systemctl --user restart ai-supervisor.service
```

При первом запуске создаётся `var/backups/pre-migration-v5-*.db`. Откат промптов — копировать
`prompts/baseline/2.6.0/` в активные файлы. Откат кода — `git revert` текущего коммита;
схема v5 добавляет nullable-колонки и не требует downgrade.
