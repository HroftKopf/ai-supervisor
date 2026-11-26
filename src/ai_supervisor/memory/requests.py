from __future__ import annotations

from typing import Any

from ai_supervisor.core.request_gateway import RequestEnvelope
from ai_supervisor.memory.database import Database, iso_now, json_dumps, row_to_dict


class RequestService:
    """Durable, deduplicated request capture.

    A request row is the idempotency boundary. A request with status
    ``processing`` is claimed once; concurrent delivery returns the existing
    row and cannot start a second planner/worker. A crash leaves it
    ``processing`` and startup recovery resets it to ``captured`` only when no
    task was attached.
    """

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
                # Unique race: another delivery won the INSERT. Read it back.
                row = await self.db.fetchone(
                    "SELECT * FROM requests WHERE dedup_key = ?", (envelope.dedup_key,)
                )
                if row is None:
                    raise
        data = row_to_dict(row) or {}
        if data.get("status") == "captured" and data.get("task_id") is None:
            await self.db.execute(
                "UPDATE requests SET status='processing', request_id=? WHERE id=?",
                (envelope.request_id, int(data["id"])),
            )
            data["status"] = "processing"
            data["request_id"] = envelope.request_id
            return data, True
        return data, False

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

    async def recovery_reset(self) -> int:
        cursor = await self.db.execute(
            "UPDATE requests SET status='captured' WHERE status='processing' AND task_id IS NULL"
        )
        count = cursor.rowcount
        await cursor.close()
        return count
