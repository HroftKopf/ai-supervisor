from __future__ import annotations

import hashlib
from typing import Any

from ai_supervisor.memory.database import Database, iso_now, row_to_dict
from ai_supervisor.models import TaskStatus


class RevisionError(RuntimeError):
    pass


class RevisionConflict(RevisionError):
    pass


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RevisionService:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    async def create_initial_conn(
        conn: Any,
        *,
        task_id: int,
        request_id: str | None,
        source_request: str,
        author_id: int | None = None,
        revision_number: int = 1,
    ) -> int:
        content = source_request.strip()
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO request_revisions(
                task_id, request_id, revision_number, previous_revision_id,
                effective_requirements, owner_amendment, author_id, origin,
                content_fingerprint, created_at)
            VALUES (?, ?, ?, NULL, ?, '', ?, 'initial', ?, ?)
            """,
            (
                task_id,
                request_id,
                revision_number,
                content,
                author_id,
                fingerprint(content),
                iso_now(),
            ),
        )
        revision_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return revision_id

    async def ensure_initial(self, task: Any) -> dict[str, Any]:
        revision_number = max(1, int(getattr(task, "request_revision", 1) or 1))
        row = await self.db.fetchone(
            "SELECT * FROM request_revisions WHERE task_id=? AND revision_number=?",
            (task.id, revision_number),
        )
        if row is None:
            async with self.db.transaction() as conn:
                await self.create_initial_conn(
                    conn,
                    task_id=task.id,
                    request_id=getattr(task, "request_id", None),
                    source_request=getattr(task, "source_request", "") or "",
                    revision_number=revision_number,
                )
            row = await self.db.fetchone(
                "SELECT * FROM request_revisions WHERE task_id=? AND revision_number=?",
                (task.id, revision_number),
            )
        if row is None:
            raise RevisionError("не удалось создать начальную revision")
        return row_to_dict(row) or {}

    async def active(self, task: Any) -> dict[str, Any]:
        return await self.ensure_initial(task)

    async def history(self, task: Any) -> list[dict[str, Any]]:
        await self.ensure_initial(task)
        rows = await self.db.fetchall(
            "SELECT * FROM request_revisions WHERE task_id=? ORDER BY revision_number",
            (task.id,),
        )
        return [row_to_dict(row) or {} for row in rows]

    async def apply_amendment(
        self,
        *,
        task: Any,
        requirements: str,
        author_id: int,
        expected_revision: int,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        if task.status not in {
            TaskStatus.PAUSED,
            TaskStatus.INTERRUPTED,
            TaskStatus.BLOCKED,
            TaskStatus.RETRY_WAIT,
            TaskStatus.WAITING_INPUT,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.FAILED,
        }:
            raise RevisionError(f"task status {task.status.value} is not safe for amendment")
        clean = requirements.strip()
        if not clean:
            raise RevisionError("новые требования пусты")
        if len(clean) > 8000:
            raise RevisionError("новые требования превышают 8000 символов")
        if fingerprint(clean) != expected_fingerprint:
            raise RevisionError("fingerprint новых требований не совпадает с approval")
        async with self.db.transaction() as conn:
            current = await (
                await conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND request_revision=? "
                    "AND status IN ('paused','interrupted','blocked','retry_wait',"
                    "'waiting_input','waiting_approval','failed')",
                    (task.id, expected_revision),
                )
            ).fetchone()
            if current is None:
                raise RevisionConflict(
                    "revision изменилась после создания подтверждения; amendment отклонён"
                )
            previous = await (
                await conn.execute(
                    "SELECT * FROM request_revisions WHERE task_id=? AND revision_number=?",
                    (task.id, expected_revision),
                )
            ).fetchone()
            if previous is None:
                raise RevisionConflict("предыдущая revision не найдена")
            next_number = expected_revision + 1
            now = iso_now()
            if any(
                current[key]
                for key in (
                    "session_id",
                    "dsh_home",
                    "worktree_path",
                    "branch",
                    "logs_path",
                    "diff_path",
                )
            ):
                await conn.execute(
                    """
                    INSERT INTO task_sessions(
                        task_id, revision_number, session_id, dsh_home, worktree_path,
                        branch, logs_path, diff_path, retired_at, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'owner_amendment')
                    """,
                    (
                        task.id,
                        expected_revision,
                        current["session_id"],
                        current["dsh_home"],
                        current["worktree_path"],
                        current["branch"],
                        current["logs_path"],
                        current["diff_path"],
                        now,
                    ),
                )
            cursor = await conn.execute(
                """
                UPDATE tasks SET request_revision=?, spec_json='{}',
                    review_json=NULL, evidence_json=NULL, evidence_manifest_json=NULL,
                    report=NULL, error=NULL,
                    session_id=NULL, dsh_home=NULL, worktree_path=NULL,
                    branch=NULL, logs_path=NULL, diff_path=NULL, base_commit=NULL,
                    head_commit=NULL, updated_at=?
                WHERE id=? AND request_revision=?
                  AND status IN ('paused','interrupted','blocked','retry_wait',
                                 'waiting_input','waiting_approval','failed')
                """,
                (next_number, now, task.id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("конкурентное изменение revision")
            await cursor.close()
            cursor = await conn.execute(
                """
                INSERT INTO request_revisions(
                    task_id, request_id, revision_number, previous_revision_id,
                    effective_requirements, owner_amendment, author_id, origin,
                    content_fingerprint, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'owner_amendment', ?, ?)
                """,
                (
                    task.id,
                    getattr(task, "request_id", None),
                    next_number,
                    int(previous["id"]),
                    clean,
                    clean,
                    author_id,
                    fingerprint(clean),
                    now,
                ),
            )
            revision_id = int(cursor.lastrowid or 0)
            await cursor.close()
            await conn.execute(
                "UPDATE approvals SET status='revoked', decided_at=? "
                "WHERE task_id=? AND status IN ('pending','approved')",
                (now, task.id),
            )
            await conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, message, payload_json, created_at)
                VALUES (?, 'amendment', ?, ?, ?)
                """,
                (
                    task.id,
                    f"Требования изменены: revision {expected_revision} → {next_number}",
                    __import__("json").dumps(
                        {
                            "previous_revision": expected_revision,
                            "revision": next_number,
                            "revision_id": revision_id,
                            "fingerprint": fingerprint(clean),
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        row = await self.db.fetchone("SELECT * FROM request_revisions WHERE id=?", (revision_id,))
        return row_to_dict(row) or {}
