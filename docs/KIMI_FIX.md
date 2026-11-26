# Исправление Kimi K3.1 Pro: tool_choice и thinking

## Симптом

При вызове Kimi Code API с объектом:

```json
{
  "tool_choice": {
    "type": "function",
    "function": {"name": "submit_intent"}
  }
}
```

K3.1 Pro возвращал:

```text
HTTP 400: tool_choice 'specified' is incompatible with thinking enabled
```

Причина: K3.1 Pro всегда работает в thinking-режиме, а принудительный именованный
`tool_choice` несовместим с ним. Отключать reasoning или менять модель нельзя.

## Что изменено

1. Единственное место с именованным объектом `tool_choice` —
   `SupervisorAgent._structured_call` — переведено на `tool_choice="required"`.
   В `tools` передаётся ровно один требуемый инструмент.
2. Обычное общение (`SupervisorAgent.chat`) использует `tool_choice="auto"`.
3. `KimiClient.complete`:
   - принимает только `auto` / `none` / `required`;
   - программно отклоняет именованный объект до HTTP-запроса;
   - отклоняет `required` без определений инструментов;
   - при наличии инструментов и отсутствии явного выбора ставит `auto`.
4. Сохранена передача `reasoning_content`: при ошибке/повторе используется полное
   assistant-сообщение, включая reasoning, затем ответы на **каждый** tool call.
5. Валидация на стороне оркестратора:
   - ровно один tool call и только ожидаемое имя;
   - JSON-аргументы обязаны быть объектом;
   - аргументы валидируются Pydantic-схемой (`IntentResult`, `TaskSpec`, `ReviewResult`).
6. Если модель не вернула вызов инструмента:
   - выполняется одна корректирующая попытка с `tool_choice="required"`;
   - при повторной ошибке задача завершается понятной ошибкой, без бесконечного цикла.
7. Ошибочный/неожиданный вызов инструмента получает tool-result с ошибкой, после чего
   выполняется корректирующая попытка.

## Регрессионные тесты

- `tests/test_kimi_client.py`:
  - именованный `tool_choice` отклоняется до HTTP;
  - `required` без tools отклоняется;
  - `auto`/`required` уходят строками, ровно один tool;
  - несколько tool calls отклоняются.
- `tests/test_supervisor_tool_choice.py`:
  - отсутствие tool call → retry с `required`, reasoning сохранён;
  - неверное имя инструмента → tool-result с ошибкой и retry;
  - невалидные аргументы → schema error, tool-result, retry;
  - два отсутствующих вызова → контролируемая ошибка без цикла.
- `tests/test_kimi_live.py` (opt-in):
  - live chat с `auto`;
  - live classify/plan/review с `required`.

## Результаты живой проверки

Через настроенный Kimi API (без вывода ключей):

```text
live chat: Связь работает.
live classify: task 1.0
live plan: Перевести README проекта на русский язык criteria 4
live review: valid ReviewResult (accept/rework)
```

Итог: `AI_SUPERVISOR_RUN_LIVE_KIMI=1 pytest -q tests/test_kimi_live.py` — 2 passed.
Обычные/регрессионные тесты: 103 passed, 4 skipped. `ruff check` — без ошибок.

## Перезапуск сервиса

`.env` не изменялся.

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
ai-supervisor doctor
systemctl --user restart ai-supervisor.service
journalctl --user -u ai-supervisor.service -f
```

Если сервис не установлен:

```bash
cd ~/Projects/ai-supervisor
. .venv/bin/activate
ai-supervisor doctor
ai-supervisor run
```

Проверка после рестарта: отправить боту обычный вопрос (ожидается ответ без 400), затем
небольшую задачу (должны сформироваться ТЗ и отчёт ревью). Если нужен только live smoke Kimi:

```bash
AI_SUPERVISOR_RUN_LIVE_KIMI=1 pytest -q tests/test_kimi_live.py -s
```
