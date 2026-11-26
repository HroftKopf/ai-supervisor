# Тестирование

## Команды

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate

ruff check .
pytest -q

# Реальный DSH runtime + bwrap + локальный Messages mock:
AI_SUPERVISOR_RUN_INTEGRATION=1 pytest -q tests/test_worker_integration.py
```

## Результаты на окружении разработки

Дата: 2026-11-26.

| Проверка | Результат |
|---|---|
| `ruff check .` | все проверки пройдены |
| `pytest -q` | **174 passed, 4 skipped** |
| `AI_SUPERVISOR_RUN_INTEGRATION=1 pytest -q tests/test_worker_integration.py` | **2 passed** |
| `AI_SUPERVISOR_RUN_LIVE_KIMI=1 pytest -q tests/test_kimi_live.py

# Coverage (branch) and offline infrastructure benchmark:
coverage run --branch --source=src/ai_supervisor -m pytest -q
coverage report -m
python benchmarks/run_offline.py` | **2 passed** |
| bwrap probe | OK (user namespaces, read-only `/usr`) |
| DSH runtime `sdk-minimal` init | OK |
| Реальный DSH turn через mock Messages API | `completed`, usage input=42 output=7 cached=3 requests=1 |
| Полный orchestrator flow с fake Kimi + real Git | `waiting_approval` → `accept` → `ff-only merge` → `completed` |

### Что покрывают unit-тесты

- Конфигурация: wrong platform model, username instead of numeric owner, `.env` permissions.
- Redaction: известные секреты, `sk-*`, Telegram token, Bearer, env-переменные.
- Path traversal и symlink-выход за allowed root; регистрация не-Git пути.
- Отслеживаемые секретные файлы в репозитории.
- SQLite WAL/FTS5: сообщения и память переживают reconnect.
- Конечный автомат: запрет недопустимых переходов.
- Owner middleware: чужой user ID, group chat, duplicate update_id.
- Approvals: привязка к user, expiry, single-use, запрет DENIED action.
- Kimi client: streaming-ассемблинг content/reasoning/tool calls/usage; отсутствие
  `temperature`/`top_p`; сохранение reasoning_content в assistant message.
- Оркестратор: chat не создаёт задачу; задача проходит plan → worker → diff → review →
  waiting_approval → accept → merge; пустой diff не принимается; лимит доработок; stop;
  запрет второй задачи при ожидании принятия.
- bwrap probe и отказ от неизвестного sandbox backend; worker netns + egress socket.
- Egress proxy: allowlisted CONNECT, отказ неизвестному host.
- COMPOSITE: граф/циклы, маршрутизация executor, approvals, reject, повтор, no-op import,
  auto-discovery Niri, mock GitHub + bare remote, сохранение completed этапов.
- SQLite v6: `task_stages`, UI/usage/prompt metadata, structured memory columns; миграция с backup.
- Git auth: HTTPS askpass, ls-remote/fetch/push через единый слой, SSH passthrough,
  third-party host без токена, отсутствие токена в env/argv/config, auth-error без ретраев,
  WAITING_INPUT и продолжение publish.
- COMPOSITE-отчёты: executor, stage status, commit hash, secret-scan, planning time.
- Presentation Layer: compact/detailed/debug, HTML escaping, edit/fallback/throttling,
  restored message_id, approval/error/details cards.
- Prompt profiles/versions/common safety; request diagnostics; capability/strategy selection.
- Reviewer root cause: planner_error не отправляет executor на rework; external → RETRY_WAIT.
- Memory categories/upsert/stale/experience extractor; 39 evaluation scenarios.
- Provider abstraction: Kimi regression, OpenAI Responses mock, tools, streaming, usage,
  normalized errors, capability negotiation, independent roles, no fallback.
- Configuration: grouped↔legacy migration, unknown section/key, wrong types, provider/role
  validation, env-only-secrets template checks, local config ignored.
- Typography: Markdown→Telegram HTML, paragraphs/code, vertical spacing and limits.
- Worker env excludes KIMI/OpenAI/OPENAI_COMPATIBLE keys.

### Что не проверено живьём

- реальный Telegram `getMe`/long polling/кнопки;
- реальный платный DeepSeek endpoint (вместо него — локальный Messages-совместимый mock);
- реальная публикация пользовательских файлов на GitHub (использован mock API и временный
  bare remote).

Kimi API проверен живьём через `AI_SUPERVISOR_RUN_LIVE_KIMI=1` (chat/classify/plan v2/review
и composite Niri plan), включая `tool_choice=required` и структурное ревью. Повторный прогон
2026-11-26 после исчерпания 5-часовой квоты вернул HTTP 429 (`KimiQuotaError`); это ограничение
аккаунта, а не регрессия кода. Live-тесты в общем `pytest` пропущены.
Telegram-проверку нужно выполнить после настройки и запуска бота:

```bash
ai-supervisor doctor
ai-supervisor run
```

`doctor` безопасно проверяет Kimi `/models`, Telegram `getMe`, DSH runtime и bwrap.
Живой Kimi smoke можно повторить отдельно:

```bash
AI_SUPERVISOR_RUN_LIVE_KIMI=1 pytest -q tests/test_kimi_live.py

# Coverage (branch) and offline infrastructure benchmark:
coverage run --branch --source=src/ai_supervisor -m pytest -q
coverage report -m
python benchmarks/run_offline.py -s
```
Затем создайте тестовую задачу на небольшом проекте и проверьте отчёт, `/diff`, `/logs`,
кнопки «Принять»/«На доработку»/«Откатить».

## Ручной smoke-тест worker

`scripts/mock_deepseek_api.py` — локальный mock Messages API. Он не вызывает модель и нужен только
для проверки инфраструктуры.

```bash
python scripts/mock_deepseek_api.py --port 8765 &
export DEEPSEEK_API_KEY=test
export DEEPSEEK_BASE_URL=http://127.0.0.1.nip.io:8765/v1
AI_SUPERVISOR_RUN_INTEGRATION=1 pytest -q tests/test_worker_integration.py
```

Тест запускает настоящий `DeepSeekHarness` в bwrap и проверяет:
- инициализацию `sdk-minimal`;
- patch отключения session-log upload;
- передачу base URL;
- сбор usage;
- завершение `completed`.

## Диагностика

```bash
ai-supervisor doctor
journalctl --user -u ai-supervisor.service -n 200 --no-pager
tail -n 200 var/logs/ai-supervisor.log
```

Если worker не стартует, проверьте:

- `bwrap` установлен и `bwrap --version` работает;
- `sys.prefix != sys.base_prefix` (запуск из venv);
- `config/dsh/disable-session-log-upload.yml` существует;
- `DEEPSEEK_BASE_URL` указывает на Messages-совместимый endpoint;
- в `var/logs/task-NNNN/attempt-N/worker.stderr.log` нет ENOENT по путям sandbox.

Логи и diff редактируются redactor'ом, но перед отправкой в issue всё равно проверьте их вручную.
