from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ai_supervisor.core.request_gateway import RequestEnvelope
from ai_supervisor.memory.database import Database, iso_now, json_dumps, row_to_dict

RETRYABLE_STATUSES = frozenset({"retryable", "failed", "cancelled"})


class RequestService:
    """Durable request lifecycle and atomic task binding."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def capture(self, envelope: RequestEnvelope) -> tuple[dict[str, Any], bool]:
        row = await self.db.fetchone(
            "SELECT * FROM requests WHERE dedup_key = ?", (envelope.dedup_key,)
        )
        if row is None:
            try:
                await self.db.execute(
                    """
                    INSERT INTO requests(request_id, dedup_key, source, owner_id, chat_id,
                        source_message_id, project_id, conversation_id, original_request,
                        attachment_refs_json, mode, revision, status, created_at)
                    VALUES (?, ?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?)
                    """,
                    (
                        envelope.request_id,
                        envelope.dedup_key,
                        envelope.owner_id,
                        envelope.chat_id,
                        envelope.source_message_id,
                        envelope.project_id,
                        envelope.conversation_id,
                        envelope.original_text,
                        json_dumps(list(envelope.attachment_refs)),
                        envelope.mode,
                        envelope.source_revision,
                        iso_now(),
                    ),
                )
                row = await self.db.fetchone(
                    "SELECT * FROM requests WHERE dedup_key = ?", (envelope.dedup_key,)
                )
                return row_to_dict(row) or {}, True
            except Exception:
                row = await self.db.fetchone(
                    "SELECT * FROM requests WHERE dedup_key = ?", (envelope.dedup_key,)
                )
                if row is None:
                    raise
        data = row_to_dict(row) or {}
        if data.get("task_id") is not None:
            return data, False
        if data.get("status") == "captured":
            cursor = await self.db.execute(
                "UPDATE requests SET status='processing' "
                "WHERE id=? AND status='captured' AND task_id IS NULL",
                (int(data["id"]),),
            )
            won = cursor.rowcount == 1
            await cursor.close()
            if won:
                data["status"] = "processing"
                return data, True
            fresh = await self.db.fetchone("SELECT * FROM requests WHERE id=?", (int(data["id"]),))
            return row_to_dict(fresh) or data, False
        return data, False

    @staticmethod
    def envelope_from_row(row: dict[str, Any]) -> RequestEnvelope:
        try:
            refs = tuple(str(item) for item in json.loads(row.get("attachment_refs_json") or "[]"))
        except (TypeError, ValueError):
            refs = ()
        return RequestEnvelope(
            request_id=str(row["request_id"]),
            correlation_id=str(row["request_id"]),
            owner_id=int(row["owner_id"]),
            chat_id=int(row["chat_id"]),
            conversation_id=int(row["conversation_id"]),
            project_id=int(row["project_id"]),
            source_message_id=(
                int(row["source_message_id"]) if row.get("source_message_id") is not None else None
            ),
            original_text=str(row["original_request"]),
            attachment_refs=refs,
            timestamp=datetime.now(UTC),
            mode=str(row.get("mode") or "auto"),
            source_revision=int(row.get("revision") or 1),
            dedup_key=str(row["dedup_key"]),
        )

    async def create_task(
        self,
        *,
        request_id: str,
        project_id: int,
        conversation_id: int,
        title: str,
        max_rework: int,
        source_request: str,
    ) -> tuple[Any, bool]:
        """Atomically create the task, accepted event and request binding."""
        from ai_supervisor.memory.tasks import TaskService

        tasks = TaskService(self.db)
        async with self.db.transaction() as conn:
            existing = await (
                await conn.execute(
                    "SELECT * FROM tasks WHERE request_id=? ORDER BY id LIMIT 1", (request_id,)
                )
            ).fetchone()
            if existing is not None:
                await conn.execute(
                    "UPDATE requests SET task_id=?, status='task' "
                    "WHERE request_id=? AND task_id IS NULL",
                    (int(existing["id"]), request_id),
                )
                return TaskService.model(existing), False
            task, created = await tasks.create_in_transaction(
                conn,
                project_id,
                conversation_id,
                title,
                max_rework=max_rework,
                source_request=source_request,
                request_id=request_id,
                intent_source="telegram",
            )
            if created:
                await conn.execute(
                    """
                    INSERT INTO task_events(task_id, event_type, message, payload_json,
                                            created_at)
                    VALUES (?, 'accepted', 'Задача принята от владельца', '{}', ?)
                    """,
                    (task.id, iso_now()),
                )
                cursor = await conn.execute(
                    "UPDATE requests SET task_id=?, status='task' "
                    "WHERE request_id=? AND task_id IS NULL",
                    (task.id, request_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("request binding failed")
                await cursor.close()
        return await tasks.get(task.id), created

    async def mark_status(self, request_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE requests SET status=? WHERE request_id=?", (status[:40], request_id)
        )

    async def attach_task(self, request_id: str, task_id: int) -> None:
        await self.db.execute(
            "UPDATE requests SET task_id=?, status='task' WHERE request_id=?",
            (task_id, request_id),
        )

    async def get(self, request_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM requests WHERE request_id = ?", (request_id,))
        return row_to_dict(row)

    async def retry(self, request_id: str) -> tuple[dict[str, Any], bool]:
        if not request_id:
            return {}, False
        cursor = await self.db.execute(
            "UPDATE requests SET status='captured' "
            "WHERE request_id=? AND task_id IS NULL "
            "AND status IN ('retryable','failed','cancelled')",
            (request_id,),
        )
        changed = cursor.rowcount == 1
        await cursor.close()
        row = await self.get(request_id)
        return row or {}, changed

    async def recovery_reset(self) -> int:
        cursor = await self.db.execute(
            "UPDATE requests SET status='captured' WHERE status='processing' AND task_id IS NULL"
        )
        count = cursor.rowcount
        await cursor.close()
        return count
