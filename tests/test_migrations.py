from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_supervisor.memory.database import Database


@pytest.mark.asyncio
async def test_v1_database_is_migrated_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key,value) VALUES('schema_version','1');
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY, name TEXT, path TEXT, description TEXT NOT NULL DEFAULT '',
            stack TEXT NOT NULL DEFAULT '', instructions TEXT NOT NULL DEFAULT '',
            base_branch TEXT NOT NULL DEFAULT 'main', test_command TEXT, build_command TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, project_id INTEGER, conversation_id INTEGER,
            title TEXT NOT NULL, status TEXT NOT NULL, spec_json TEXT NOT NULL DEFAULT '{}',
            worker_name TEXT, worktree_path TEXT, branch TEXT, base_ref TEXT, base_commit TEXT,
            head_commit TEXT, rework_count INTEGER NOT NULL DEFAULT 0,
            max_rework INTEGER NOT NULL DEFAULT 3, review_json TEXT, evidence_json TEXT,
            report TEXT, error TEXT, session_id TEXT, dsh_home TEXT, diff_path TEXT,
            logs_path TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
        );
        CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY, task_id INTEGER, project_id INTEGER, provider TEXT NOT NULL,
            model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    db = Database(path)
    await db.connect()
    try:
        version = await db.scalar("SELECT value FROM schema_meta WHERE key='schema_version'")
        assert version == "12"
        task_columns = {str(row["name"]) for row in await db.fetchall("PRAGMA table_info(tasks)")}
        project_columns = {
            str(row["name"]) for row in await db.fetchall("PRAGMA table_info(projects)")
        }
        usage_columns = {
            str(row["name"]) for row in await db.fetchall("PRAGMA table_info(usage_events)")
        }
        assert {"task_type", "attempts", "progress_hash", "stalled_count"} <= task_columns
        assert {"remote_url", "policy_json", "extra_read_paths_json"} <= project_columns
        assert {
            "duration_ms",
            "cost",
            "currency",
            "raw_usage_json",
            "request_id",
            "provider_id",
            "account_scope_id",
            "reasoning_profile",
        } <= usage_columns
        task_ui_columns = {
            str(row["name"]) for row in await db.fetchall("PRAGMA table_info(tasks)")
        }
        assert {
            "ui_chat_id",
            "ui_message_id",
            "ui_hash",
            "request_id",
            "source_request",
            "request_revision",
            "intent_source",
        } <= task_ui_columns
        assert {str(row["name"]) for row in await db.fetchall("PRAGMA table_info(requests)")} >= {
            "request_id",
            "dedup_key",
            "original_request",
            "revision",
            "task_id",
        }
        memory_columns = {
            str(row["name"]) for row in await db.fetchall("PRAGMA table_info(memories)")
        }
        assert {"category", "task_id", "applicability", "verified", "stale"} <= memory_columns
        stage_columns = {
            str(row["name"]) for row in await db.fetchall("PRAGMA table_info(task_stages)")
        }
        assert {
            "stage_key",
            "operation",
            "depends_on_json",
            "status",
            "result_json",
        } <= stage_columns
        assert list((tmp_path / "backups").glob("pre-migration-v12-*.db"))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v9_request_table_migration_preserves_tasks_and_memory(tmp_path: Path) -> None:
    path = tmp_path / "v9.db"
    db = Database(path)
    await db.connect()
    try:
        await db.execute(
            "INSERT INTO projects(name, path, created_at, updated_at) VALUES(?,?,?,?)",
            ("legacy", str(tmp_path), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        await db.execute(
            "INSERT INTO conversations(chat_id, project_id, created_at, updated_at) VALUES(?,?,?,?)",
            (1, 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        await db.execute(
            "INSERT INTO tasks(project_id, conversation_id, title, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                1,
                1,
                "legacy task",
                "completed",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        await db.execute(
            "INSERT INTO memories(scope, project_id, key, content, created_at, updated_at) "
            "VALUES('project', 1, 'legacy', 'remember me', ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        await db.execute("DROP TABLE requests")
        await db.execute("DROP INDEX IF EXISTS idx_tasks_request_id")
        await db.execute("UPDATE schema_meta SET value='9' WHERE key='schema_version'")
    finally:
        await db.close()
    reopened = Database(path)
    await reopened.connect()
    try:
        assert (
            await reopened.scalar("SELECT value FROM schema_meta WHERE key='schema_version'")
            == "12"
        )
        assert await reopened.scalar("SELECT COUNT(*) FROM tasks WHERE title='legacy task'") == 1
        assert (
            await reopened.scalar("SELECT content FROM memories WHERE key='legacy'")
            == "remember me"
        )
        assert await reopened.scalar("SELECT COUNT(*) FROM requests") == 0
        assert list((tmp_path / "backups").glob("pre-migration-v12-*.db"))
    finally:
        await reopened.close()
