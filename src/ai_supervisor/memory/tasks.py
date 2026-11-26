from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ai_supervisor.memory.database import Database, iso_now, json_dumps, row_to_dict
from ai_supervisor.models import (
    TaskRecord,
    TaskStatus,
    ensure_transition,
    utcnow,
)

# A task blocks a second task in the same project until its worktree is merged,
# rolled back, or cancelled.
ACTIVE_TASK_STATUSES = (
    TaskStatus.CREATED.value,
    TaskStatus.PLANNING.value,
    TaskStatus.QUEUED.value,
    TaskStatus.RUNNING.value,
    TaskStatus.VALIDATING.value,
    TaskStatus.REVIEWING.value,
    TaskStatus.REWORK.value,
    TaskStatus.WAITING_APPROVAL.value,
    TaskStatus.WAITING_INPUT.value,
    TaskStatus.RETRY_WAIT.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.PAUSED.value,
    TaskStatus.INTERRUPTED.value,
)

# Only these were actually in flight when the service stopped. WAITING_APPROVAL
# and PAUSED survive a restart unchanged.
INTERRUPTIBLE_TASK_STATUSES = (
    TaskStatus.CREATED.value,
    TaskStatus.PLANNING.value,
    TaskStatus.QUEUED.value,
    TaskStatus.RUNNING.value,
    TaskStatus.VALIDATING.value,
    TaskStatus.REVIEWING.value,
    TaskStatus.REWORK.value,
)


class TaskError(RuntimeError):
    pass


class TaskService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        project_id: int,
        conversation_id: int,
        title: str,
        *,
        max_rework: int,
        source_request: str = "",
        request_id: str | None = None,
        intent_source: str = "telegram",
    ) -> TaskRecord:
        now = iso_now()
        cursor = await self.db.execute(
            """
            INSERT INTO tasks(project_id, conversation_id, title, request_id,
                              source_request, intent_source, status, max_rework,
                              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                conversation_id,
                title.strip()[:120] or "Задача",
                request_id,
                source_request.strip()[:8000],
                intent_source,
                TaskStatus.CREATED.value,
                max_rework,
                now,
                now,
            ),
        )
        task_id = int(cursor.lastrowid or 0)
        await cursor.close()
        task = await self.get(task_id)
        await self.add_event(task_id, "created", "Задача создана")
        return task

    async def get(self, task_id: int) -> TaskRecord:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise TaskError("задача не найдена")
        return TaskRecord.model_validate(row_to_dict(row))

    async def get_optional(self, task_id: int) -> TaskRecord | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        data = row_to_dict(row)
        return TaskRecord.model_validate(data) if data else None

    async def latest(
        self, *, project_id: int | None = None, statuses: Sequence[str] | None = None
    ) -> TaskRecord | None:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = await self.db.fetchone(f"SELECT * FROM tasks{where} ORDER BY id DESC LIMIT 1", params)
        data = row_to_dict(row)
        return TaskRecord.model_validate(data) if data else None

    async def list(
        self,
        *,
        project_id: int | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[TaskRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self.db.fetchall(
            f"SELECT * FROM tasks{where} ORDER BY id DESC LIMIT ?", (*params, limit)
        )
        return [TaskRecord.model_validate(row_to_dict(row)) for row in rows]

    async def active(self, project_id: int | None = None) -> list[TaskRecord]:
        return await self.list(project_id=project_id, statuses=ACTIVE_TASK_STATUSES, limit=100)

    async def transition(self, task_id: int, new_status: TaskStatus) -> TaskRecord:
        now = iso_now()
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise TaskError("задача не найдена")
            current = TaskStatus(row["status"])
            ensure_transition(current, new_status)
            started_at = (
                "COALESCE(started_at, ?)" if new_status == TaskStatus.RUNNING else "started_at"
            )
            finished_at = (
                "COALESCE(finished_at, ?)"
                if new_status
                in {
                    TaskStatus.COMPLETED,
                    TaskStatus.PARTIALLY_COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }
                else "finished_at"
            )
            params: list[Any] = [new_status.value, now]
            if new_status == TaskStatus.RUNNING:
                params.append(now)
            if new_status in {
                TaskStatus.COMPLETED,
                TaskStatus.PARTIALLY_COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                params.append(now)
            params.extend([task_id, current.value])
            cursor = await conn.execute(
                f"""
                UPDATE tasks SET status = ?, updated_at = ?, started_at = {started_at},
                                 finished_at = {finished_at}
                WHERE id = ? AND status = ?
                """,
                params,
            )
            if cursor.rowcount != 1:
                raise TaskError("состояние задачи изменилось конкурентно")
            await cursor.close()
        task = await self.get(task_id)
        await self.add_event(
            task_id, "status", f"Статус: {new_status.value}", {"status": new_status.value}
        )
        return task

    async def update_fields(self, task_id: int, **fields: Any) -> TaskRecord:
        allowed = {
            "title",
            "request_id",
            "source_request",
            "request_revision",
            "intent_source",
            "task_type",
            "parent_task_id",
            "spec_json",
            "worker_name",
            "worktree_path",
            "branch",
            "base_ref",
            "base_commit",
            "head_commit",
            "rework_count",
            "attempts",
            "stalled_count",
            "progress_hash",
            "blocked_reason",
            "review_json",
            "evidence_json",
            "report",
            "error",
            "session_id",
            "dsh_home",
            "diff_path",
            "logs_path",
            "cancel_requested",
            "ui_chat_id",
            "ui_message_id",
            "ui_hash",
            "ui_updated_at",
        }
        unexpected = set(fields) - allowed
        if unexpected:
            raise TaskError(f"unexpected task fields: {sorted(unexpected)}")
        if not fields:
            return await self.get(task_id)
        fields["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*fields.values(), task_id),
        )
        return await self.get(task_id)

    async def save_spec(self, task_id: int, spec: Any) -> TaskRecord:
        payload = spec.model_dump() if hasattr(spec, "model_dump") else spec
        task_type = payload.get("task_type", "modify")
        if hasattr(task_type, "value"):
            task_type = task_type.value
        return await self.update_fields(
            task_id,
            title=payload.get("title", "Задача")[:120],
            task_type=task_type,
            parent_task_id=payload.get("parent_task_id"),
            spec_json=json_dumps(payload),
        )

    async def reset_started_at(self, task_id: int) -> None:
        await self.db.execute(
            "UPDATE tasks SET started_at = ?, finished_at = NULL, updated_at = ? WHERE id = ?",
            (iso_now(), iso_now(), task_id),
        )

    async def request_cancel(self, task_id: int) -> None:
        await self.update_fields(task_id, cancel_requested=True)

    async def clear_cancel(self, task_id: int) -> None:
        await self.update_fields(task_id, cancel_requested=False)

    async def add_event(
        self,
        task_id: int,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        *,
        agent: str | None = None,
        stage_id: int | None = None,
        duration_ms: int | None = None,
        error_category: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO task_events(task_id, event_type, message, payload_json, agent,
                                    stage_id, duration_ms, error_category, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                event_type,
                message[:8000],
                json_dumps(payload or {}),
                agent,
                stage_id,
                duration_ms,
                error_category,
                iso_now(),
            ),
        )

    async def events(self, task_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)
        )
        return [row_to_dict(row) or {} for row in reversed(rows)]

    async def add_agent_message(self, task_id: int, sender: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO agent_messages(task_id, sender, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, sender[:40], content[:20000], iso_now()),
        )

    async def agent_messages(self, task_id: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM agent_messages WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )
        return [row_to_dict(row) or {} for row in reversed(rows)]

    async def create_worker(
        self,
        *,
        worker_id: str,
        task_id: int,
        kind: str,
        session_id: str | None,
        dsh_home: str | None,
        pid: int | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO workers(id, task_id, kind, status, session_id, dsh_home, pid, started_at)
            VALUES (?, ?, ?, 'starting', ?, ?, ?, ?)
            """,
            (worker_id, task_id, kind, session_id, dsh_home, pid, iso_now()),
        )

    async def update_worker(
        self,
        worker_id: str,
        *,
        status: str | None = None,
        session_id: str | None = None,
        pid: int | None = None,
        usage: Any | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if session_id is not None:
            fields["session_id"] = session_id
        if pid is not None:
            fields["pid"] = pid
        if usage is not None:
            fields["usage_json"] = json_dumps(
                usage.model_dump() if hasattr(usage, "model_dump") else usage
            )
        if status in {"finished", "failed", "cancelled", "timeout"}:
            fields["finished_at"] = iso_now()
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE workers SET {assignments} WHERE id = ?", (*fields.values(), worker_id)
        )

    async def workers(self, task_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM workers WHERE task_id = ? ORDER BY started_at", (task_id,)
        )
        return [row_to_dict(row) or {} for row in rows]

    async def mark_interrupted_on_startup(self) -> list[int]:
        rows = await self.db.fetchall(
            f"SELECT id, status FROM tasks WHERE status IN "
            f"({','.join('?' for _ in INTERRUPTIBLE_TASK_STATUSES)})",
            INTERRUPTIBLE_TASK_STATUSES,
        )
        interrupted: list[int] = []
        for row in rows:
            task_id = int(row["id"])
            current = TaskStatus(row["status"])
            new = TaskStatus.INTERRUPTED
            await self.db.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, error = ? WHERE id = ? AND status = ?",
                (
                    new.value,
                    iso_now(),
                    "Сервис был остановлен во время выполнения; работа не продолжена автоматически.",
                    task_id,
                    current.value,
                ),
            )
            await self.add_event(
                task_id, "interrupted", "Сервис перезапущен. Задача помечена как прерванная."
            )
            interrupted.append(task_id)
        return interrupted

    async def mark_workers_interrupted_on_startup(self) -> int:
        cursor = await self.db.execute(
            "UPDATE workers SET status = 'interrupted', finished_at = ? "
            "WHERE status IN ('starting', 'running')",
            (iso_now(),),
        )
        count = cursor.rowcount
        await cursor.close()
        return count

    async def usage_json(self, task_id: int) -> dict[str, Any]:
        return {"note": "use UsageService for aggregates", "task_id": task_id}


def parse_usage(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def task_age_seconds(task: TaskRecord) -> float:
    end = task.finished_at or utcnow()
    return max(0.0, (end - task.created_at).total_seconds())
