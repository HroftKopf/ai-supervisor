from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_VERSION = 6


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    path TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    stack TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT 'main',
    test_command TEXT,
    build_command TEXT,
    remote_url TEXT,
    permissions_json TEXT NOT NULL DEFAULT '{}',
    extra_read_paths_json TEXT NOT NULL DEFAULT '[]',
    token_budget INTEGER NOT NULL DEFAULT 0,
    max_rework INTEGER NOT NULL DEFAULT 3,
    policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title TEXT,
    summary TEXT,
    summary_upto_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_active
    ON conversations(chat_id) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    telegram_message_id INTEGER,
    reply_to_message_id INTEGER,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    active_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    active_conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    ui_mode TEXT NOT NULL DEFAULT 'compact',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'modify',
    parent_task_id INTEGER,
    spec_json TEXT NOT NULL DEFAULT '{}',
    worker_name TEXT,
    worktree_path TEXT,
    branch TEXT,
    base_ref TEXT,
    base_commit TEXT,
    head_commit TEXT,
    rework_count INTEGER NOT NULL DEFAULT 0,
    max_rework INTEGER NOT NULL DEFAULT 3,
    attempts INTEGER NOT NULL DEFAULT 0,
    stalled_count INTEGER NOT NULL DEFAULT 0,
    progress_hash TEXT,
    blocked_reason TEXT,
    review_json TEXT,
    evidence_json TEXT,
    report TEXT,
    error TEXT,
    session_id TEXT,
    dsh_home TEXT,
    diff_path TEXT,
    logs_path TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    ui_chat_id INTEGER,
    ui_message_id INTEGER,
    ui_hash TEXT,
    ui_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, id);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    agent TEXT,
    stage_id INTEGER,
    duration_ms INTEGER,
    error_category TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);

CREATE TABLE IF NOT EXISTS task_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    operation TEXT NOT NULL,
    executor TEXT NOT NULL,
    goal TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    inputs_json TEXT NOT NULL DEFAULT '[]',
    outputs_json TEXT NOT NULL DEFAULT '[]',
    required_permissions_json TEXT NOT NULL DEFAULT '[]',
    source TEXT,
    destination TEXT,
    overwrite INTEGER NOT NULL DEFAULT 0,
    commit_message TEXT,
    repo_name TEXT,
    repo_owner TEXT,
    private INTEGER NOT NULL DEFAULT 1,
    remote TEXT NOT NULL DEFAULT 'origin',
    branch TEXT,
    merge INTEGER NOT NULL DEFAULT 1,
    instruction TEXT,
    allowed_paths_json TEXT NOT NULL DEFAULT '[]',
    require_change INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    approval_id INTEGER,
    approval_granted INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(task_id, stage_key)
);

CREATE INDEX IF NOT EXISTS idx_task_stages_task ON task_stages(task_id, position);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    dsh_home TEXT,
    pid INTEGER,
    usage_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_workers_task ON workers(task_id, id);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals(status, expires_at);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error_category TEXT,
    cost REAL,
    currency TEXT,
    prompt_version TEXT,
    role TEXT,
    routing_level INTEGER,
    context_chars INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL CHECK (scope IN ('user', 'project', 'conversation')),
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    key TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    user_confirmed INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'verified_fact',
    task_id INTEGER,
    applicability TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    last_confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, project_id, conversation_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    message_id UNINDEXED,
    conversation_id UNINDEXED,
    content
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(message_id, conversation_id, content)
    VALUES (new.id, new.conversation_id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF content ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.id;
    INSERT INTO messages_fts(message_id, conversation_id, content)
    VALUES (new.id, new.conversation_id, new.content);
END;
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED,
    content
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(memory_id, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
    INSERT INTO memories_fts(memory_id, content) VALUES (new.id, new.content);
END;
"""


class Database:
    def __init__(
        self,
        path: Path,
        *,
        backup_dir: Path | None = None,
        backup_keep: int = 20,
        backup_before_migration: bool = True,
    ) -> None:
        self.path = path
        self.backup_dir = backup_dir
        self.backup_keep = backup_keep
        self.backup_before_migration = backup_before_migration
        self._connection: aiosqlite.Connection | None = None
        # One connection is shared by the whole application. Serialise operations
        # so an unrelated message insert cannot interleave with a task transaction.
        self._operation_lock = asyncio.Lock()
        self.fts_available = False

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("database is not connected")
        return self._connection

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        await self._migrate(conn)
        try:
            await conn.executescript(FTS_SCHEMA)
            self.fts_available = True
        except aiosqlite.OperationalError:
            self.fts_available = False
        await conn.commit()
        self._connection = conn

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        row = await (
            await conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
        ).fetchone()
        current = int(row[0]) if row else 1
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {current} is newer than supported {SCHEMA_VERSION}; "
                "refusing to downgrade it"
            )
        if current == SCHEMA_VERSION:
            return
        if self.backup_before_migration and self.path.exists() and self.path.stat().st_size > 0:
            await asyncio.to_thread(
                _backup_before_migration,
                self.path,
                backup_dir=self.backup_dir,
                keep=self.backup_keep,
            )
        additions = {
            "projects": {
                "remote_url": "TEXT",
                "permissions_json": "TEXT NOT NULL DEFAULT '{}'",
                "extra_read_paths_json": "TEXT NOT NULL DEFAULT '[]'",
                "token_budget": "INTEGER NOT NULL DEFAULT 0",
                "max_rework": "INTEGER NOT NULL DEFAULT 3",
                "policy_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "chat_state": {
                "ui_mode": "TEXT NOT NULL DEFAULT 'compact'",
            },
            "tasks": {
                "ui_chat_id": "INTEGER",
                "ui_message_id": "INTEGER",
                "ui_hash": "TEXT",
                "ui_updated_at": "TEXT",
                "task_type": "TEXT NOT NULL DEFAULT 'modify'",
                "parent_task_id": "INTEGER",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "stalled_count": "INTEGER NOT NULL DEFAULT 0",
                "progress_hash": "TEXT",
                "blocked_reason": "TEXT",
            },
            "task_events": {
                "agent": "TEXT",
                "stage_id": "INTEGER",
                "duration_ms": "INTEGER",
                "error_category": "TEXT",
            },
            "memories": {
                "category": "TEXT NOT NULL DEFAULT 'verified_fact'",
                "task_id": "INTEGER",
                "applicability": "TEXT NOT NULL DEFAULT ''",
                "verified": "INTEGER NOT NULL DEFAULT 0",
                "stale": "INTEGER NOT NULL DEFAULT 0",
                "last_confirmed_at": "TEXT",
            },
            "usage_events": {
                "raw_usage_json": "TEXT",
                "prompt_version": "TEXT",
                "role": "TEXT",
                "routing_level": "INTEGER",
                "context_chars": "INTEGER",
                "duration_ms": "INTEGER NOT NULL DEFAULT 0",
                "error_category": "TEXT",
                "cost": "REAL",
                "currency": "TEXT",
            },
        }
        await conn.execute("BEGIN IMMEDIATE")
        try:
            for table, columns in additions.items():
                existing = {
                    str(r["name"])
                    for r in await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
                }
                for name, definition in columns.items():
                    if name not in existing:
                        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            await conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        async with self._operation_lock:
            cursor = await self.connection.execute(sql, params)
            await self.connection.commit()
            return cursor

    async def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        async with self._operation_lock:
            await self.connection.executemany(sql, params)
            await self.connection.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._operation_lock:
            cursor = await self.connection.execute(sql, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._operation_lock:
            cursor = await self.connection.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return None
        return row[0]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._operation_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                await self.connection.rollback()
                raise
            else:
                await self.connection.commit()


def row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _backup_before_migration(path: Path, *, backup_dir: Path | None = None, keep: int = 20) -> Path:
    backup_dir = backup_dir or (path.parent / "backups")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = (
        backup_dir
        / f"pre-migration-v{SCHEMA_VERSION}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.db"
    )
    source = sqlite3.connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    target.chmod(0o600)
    if keep > 0:
        backups = sorted(
            backup_dir.glob("pre-migration-v*-*.db"), key=lambda item: item.stat().st_mtime
        )
        for stale in backups[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass
    return target
