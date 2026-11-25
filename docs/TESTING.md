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
| `pytest -q` | **25 passed, 2 skipped** |
| `AI_SUPERVISOR_RUN_INTEGRATION=1 pytest -q tests/test_worker_integration.py` | **2 passed** |
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
- bwrap probe и отказ от неизвестного sandbox backend.

### Что не проверено живьём

Нет ключей в среде разработки, поэтому не выполнялись:

- реальный `GET /models` и chat completions Kimi Code/Moonshot;
- реальный Telegram `getMe`/long polling/кнопки;
- реальный платный DeepSeek endpoint (вместо него — локальный Messages-совместимый mock).

Эти проверки нужно выполнить после заполнения `.env`:

```bash
ai-supervisor doctor
ai-supervisor run
```

`doctor` безопасно проверяет Kimi `/models`, Telegram `getMe`, DSH runtime и bwrap.
Затем создайте тестовую задачу на небольшом проекте и проверьте отчёт, `/diff`, `/logs`,
кнопки «Принять»/«На доработку»/«Откатить».

## Ручной smoke-тест worker

`scripts/mock_deepseek_api.py` — локальный mock Messages API. Он не вызывает модель и нужен только
для проверки инфраструктуры.

```bash
python scripts/mock_deepseek_api.py --port 8765 &
export DEEPSEEK_API_KEY=test
export DEEPSEEK_BASE_URL=http://127.0.0.1:8765/v1
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
