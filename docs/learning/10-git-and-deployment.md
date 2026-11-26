# 10. Git и развёртывание

## Концепция

Каждая задача работает в отдельном worktree/branch. Публикация выполняется только после
approval, без force-push, с проверкой remote SHA. Секреты остаются в `.env` и не попадают
в Git.

## Реализация

`src/ai_supervisor/core/workspace.py` создаёт worktree, собирает diff, выполняет ff-only merge
и rollback с patch.
`src/ai_supervisor/core/git_auth.py` — временный askpass/Git config hardening.
`src/ai_supervisor/core/git_ops.py` — publish/remote verification.

`systemd/ai-supervisor.service` запускает `ai-supervisor run`; `.env` режим 600.

## Шаги

1. `config.example.toml` → `config.toml`, `.env.example` → `.env`.
2. `pip install -e '.[dev]'`.
3. `ai-supervisor doctor`.
4. `systemctl --user enable --now ai-supervisor.service`.
5. Проверить `/start`, `/usage`, `/task_info`.
6. Для обновления — backup, restart, smoke checks.

## Вопросы

1. Почему `config.toml` не коммитится?
2. Что проверяет `publish` перед push?
3. Почему rollback сохраняет patch?

## Проверь себя

Прочитай `WorkspaceManager.apply` и `GitAuthManager.context`; перечисли все проверки перед
внешним Git-действием.
