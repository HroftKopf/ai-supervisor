# 08. Sandbox и безопасность

## Концепция

LLM/worker не доверенные. Безопасность обеспечивается Python-политикой, bwrap, отдельным
сетевым namespace, allowlist egress proxy, минимальным env и approvals.

## Реализация

`src/ai_supervisor/core/sandbox.py` формирует bwrap argv: read-only system paths, writable
worktree, отдельный home, network namespace.

`src/ai_supervisor/core/worker_manager.py` запускает subprocess и передаёт только
`DEEPSEEK_API_KEY`; управляющие ключи не передаются.

`src/ai_supervisor/core/git_auth.py` использует temporary askpass, host allowlist, TLS,
`GIT_TERMINAL_PROMPT=0` и проверяет опасные local config keys.

`src/ai_supervisor/core/egress.py` разрешает только CONNECT/HTTP к allowlisted host:port.

## Шаги

1. Проверить project root/worktree containment.
2. Построить sandbox argv.
3. Запустить worker с минимальным env.
4. Собрать diff/evidence.
5. Проверить secret-scan и policy.
6. Запросить approval для внешнего действия.
7. Выполнить Git operation через GitAuthManager.

## Вопросы

1. Почему управляющие API-ключи не должны попадать worker?
2. Что мешает worker подключиться к произвольному host?
3. Почему approval не делает действие безопасным автоматически?

## Проверь себя

Найди в `worker_argv` список запрещённых mount/env и объясни каждый.
