from __future__ import annotations

from typing import Any

from ai_supervisor.memory.database import Database, iso_now, json_dumps, row_to_dict
from ai_supervisor.models import StageRecord, StageSpec, StageStatus

STAGE_TRANSITIONS: dict[StageStatus, frozenset[StageStatus]] = {
    StageStatus.PENDING: frozenset(
        {
            StageStatus.READY,
            StageStatus.RUNNING,
            StageStatus.WAITING_APPROVAL,
            StageStatus.FAILED,
            StageStatus.BLOCKED,
            StageStatus.CANCELLED,
            StageStatus.SKIPPED,
        }
    ),
    StageStatus.READY: frozenset(
        {
            StageStatus.RUNNING,
            StageStatus.WAITING_APPROVAL,
            StageStatus.FAILED,
            StageStatus.BLOCKED,
            StageStatus.CANCELLED,
        }
    ),
    StageStatus.RUNNING: frozenset(
        {
            StageStatus.COMPLETED,
            StageStatus.FAILED,
            StageStatus.WAITING_APPROVAL,
            StageStatus.BLOCKED,
            StageStatus.CANCELLED,
        }
    ),
    StageStatus.WAITING_APPROVAL: frozenset(
        {
            StageStatus.READY,
            StageStatus.RUNNING,
            StageStatus.FAILED,
            StageStatus.BLOCKED,
            StageStatus.CANCELLED,
        }
    ),
    StageStatus.FAILED: frozenset({StageStatus.PENDING, StageStatus.READY, StageStatus.CANCELLED}),
    StageStatus.BLOCKED: frozenset({StageStatus.PENDING, StageStatus.READY, StageStatus.CANCELLED}),
    StageStatus.COMPLETED: frozenset(),
    StageStatus.CANCELLED: frozenset(),
    StageStatus.SKIPPED: frozenset(),
}


class StageError(RuntimeError):
    pass


class TaskStageService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def ensure(self, task_id: int, specs: list[StageSpec]) -> list[StageRecord]:
        """Persist plan stages once. Existing runtime state is never reset by replanning."""
        now = iso_now()
        for position, spec in enumerate(specs):
            await self.db.execute(
                """
                INSERT INTO task_stages(
                    task_id, stage_key, position, operation, executor, goal,
                    depends_on_json, acceptance_criteria_json, inputs_json, outputs_json,
                    required_permissions_json, source, destination, overwrite, commit_message,
                    repo_name, repo_owner, private, remote, branch, merge, instruction,
                    allowed_paths_json, require_change, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, stage_key) DO NOTHING
                """,
                (
                    task_id,
                    spec.id,
                    position,
                    spec.operation.value,
                    spec.executor.value,
                    spec.goal,
                    json_dumps(spec.depends_on),
                    json_dumps(spec.acceptance_criteria),
                    json_dumps(spec.inputs),
                    json_dumps(spec.outputs),
                    json_dumps([item.value for item in spec.required_permissions]),
                    spec.source,
                    spec.destination,
                    int(spec.overwrite),
                    spec.commit_message,
                    spec.repo_name,
                    spec.repo_owner,
                    int(spec.private),
                    spec.remote,
                    spec.branch,
                    int(spec.merge),
                    spec.instruction,
                    json_dumps(spec.allowed_paths),
                    int(spec.require_change),
                    StageStatus.PENDING.value,
                    now,
                    now,
                ),
            )
        return await self.list(task_id)

    async def list(self, task_id: int) -> list[StageRecord]:
        rows = await self.db.fetchall(
            "SELECT * FROM task_stages WHERE task_id = ? ORDER BY position, id", (task_id,)
        )
        return [StageRecord.model_validate(row_to_dict(row)) for row in rows]

    async def get(self, stage_id: int) -> StageRecord:
        row = await self.db.fetchone("SELECT * FROM task_stages WHERE id = ?", (stage_id,))
        if row is None:
            raise StageError("этап не найден")
        return StageRecord.model_validate(row_to_dict(row))

    async def get_optional(self, stage_id: int) -> StageRecord | None:
        row = await self.db.fetchone("SELECT * FROM task_stages WHERE id = ?", (stage_id,))
        return StageRecord.model_validate(row_to_dict(row)) if row else None

    async def by_key(self, task_id: int, stage_key: str) -> StageRecord | None:
        row = await self.db.fetchone(
            "SELECT * FROM task_stages WHERE task_id = ? AND stage_key = ?",
            (task_id, stage_key),
        )
        return StageRecord.model_validate(row_to_dict(row)) if row else None

    async def update(self, stage_id: int, **fields: Any) -> StageRecord:
        allowed = {
            "status",
            "attempts",
            "approval_id",
            "approval_granted",
            "result_json",
            "error",
            "started_at",
            "finished_at",
            "repo_owner",
        }
        if not fields:
            return await self.get(stage_id)
        unexpected = set(fields) - allowed
        if unexpected:
            raise StageError(f"unexpected stage fields: {sorted(unexpected)}")
        fields["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE task_stages SET {assignments} WHERE id = ?",
            (*fields.values(), stage_id),
        )
        return await self.get(stage_id)

    async def set_status(
        self,
        stage_id: int,
        status: StageStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        approval_id: int | None = None,
        approval_granted: bool | None = None,
        increment_attempts: bool = False,
    ) -> StageRecord:
        stage = await self.get(stage_id)
        if status is not stage.status and status not in STAGE_TRANSITIONS[stage.status]:
            raise StageError(f"invalid stage transition: {stage.status.value} -> {status.value}")
        now = iso_now()
        fields: dict[str, Any] = {"status": status.value}
        if result is not None:
            fields["result_json"] = json_dumps(result)
        if error is not None:
            fields["error"] = error
        elif status is StageStatus.COMPLETED:
            fields["error"] = None
        if approval_id is not None:
            fields["approval_id"] = approval_id
        if approval_granted is not None:
            fields["approval_granted"] = int(approval_granted)
        if increment_attempts:
            fields["attempts"] = stage.attempts + 1
        if status is StageStatus.RUNNING:
            fields["started_at"] = stage.started_at or now
        if status in {
            StageStatus.COMPLETED,
            StageStatus.FAILED,
            StageStatus.BLOCKED,
            StageStatus.CANCELLED,
            StageStatus.SKIPPED,
        }:
            fields["finished_at"] = now
        return await self.update(stage_id, **fields)

    async def reset_running(self, task_id: int) -> int:
        cursor = await self.db.execute(
            "UPDATE task_stages SET status = ?, error = ?, updated_at = ? "
            "WHERE task_id = ? AND status = ?",
            (
                StageStatus.PENDING.value,
                "Выполнение прервано перезапуском",
                iso_now(),
                task_id,
                StageStatus.RUNNING.value,
            ),
        )
        count = cursor.rowcount
        await cursor.close()
        return count

    async def reset_waiting(self, task_id: int) -> int:
        cursor = await self.db.execute(
            "UPDATE task_stages SET status = ?, approval_id = NULL, approval_granted = 0, "
            "updated_at = ? WHERE task_id = ? AND status = ?",
            (
                StageStatus.PENDING.value,
                iso_now(),
                task_id,
                StageStatus.WAITING_APPROVAL.value,
            ),
        )
        count = cursor.rowcount
        await cursor.close()
        return count

    async def reset_failed(self, task_id: int) -> int:
        cursor = await self.db.execute(
            "UPDATE task_stages SET status = ?, error = NULL, approval_id = NULL, "
            "approval_granted = 0, updated_at = ? WHERE task_id = ? AND status IN (?, ?)",
            (
                StageStatus.PENDING.value,
                iso_now(),
                task_id,
                StageStatus.FAILED.value,
                StageStatus.BLOCKED.value,
            ),
        )
        count = cursor.rowcount
        await cursor.close()
        return count

    async def cancel_open(self, task_id: int) -> int:
        cursor = await self.db.execute(
            "UPDATE task_stages SET status = ?, finished_at = ?, updated_at = ? "
            "WHERE task_id = ? AND status IN (?, ?, ?, ?, ?)",
            (
                StageStatus.CANCELLED.value,
                iso_now(),
                iso_now(),
                task_id,
                StageStatus.PENDING.value,
                StageStatus.READY.value,
                StageStatus.RUNNING.value,
                StageStatus.WAITING_APPROVAL.value,
                StageStatus.FAILED.value,
            ),
        )
        count = cursor.rowcount
        await cursor.close()
        return count
