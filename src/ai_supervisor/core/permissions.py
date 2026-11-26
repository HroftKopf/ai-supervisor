from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from ai_supervisor.memory.database import Database, iso_now, json_dumps, row_to_dict
from ai_supervisor.models import ApprovalStatus, RiskLevel


class ApprovalError(RuntimeError):
    pass


class PermissionDenied(RuntimeError):
    pass


class PermissionLevel(StrEnum):
    AUTO = "auto"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


class Action(StrEnum):
    READ_PROJECT = "read_project"
    ANALYZE_CODE = "analyze_code"
    EDIT_WORKTREE = "edit_worktree"
    RUN_LOCAL_TESTS = "run_local_tests"
    RUN_BUILD = "run_build"
    CREATE_TEMP = "create_temp"
    APPLY_MERGE = "apply_merge"
    ROLLBACK = "rollback"
    CHANGE_ARCHITECTURE = "change_architecture"
    CHANGE_PUBLIC_API = "change_public_api"
    ADD_DEPENDENCY = "add_dependency"
    INSTALL_SYSTEM_PACKAGE = "install_system_package"
    CHANGE_SYSTEM_CONFIG = "change_system_config"
    PRIVILEGED_OPERATION = "privileged_operation"
    CHANGE_DEFAULT_BRANCH = "change_default_branch"
    PUBLISH = "publish"
    DELETE_IMPORTANT_DATA = "delete_important_data"
    SEND_SECRET_EXTERNALLY = "send_secret_externally"
    DESTRUCTIVE_COMMAND = "destructive_command"
    TASK_APPROVAL = "task_approval"
    GIT_PUSH = "git_push"
    GIT_REMOTE_CHANGE = "git_remote_change"
    GITHUB_REPOSITORY_CREATE = "github_repository_create"
    CONFIG_IMPORT = "config_import"
    STAGE_APPROVAL = "stage_approval"
    PROVIDER_RECOVERY = "provider_recovery"
    BYPASS_PERMISSIONS = "bypass_permissions"
    EXPAND_OWN_PERMISSIONS = "expand_own_permissions"
    READ_OTHER_SECRETS = "read_other_secrets"
    DISABLE_SECURITY = "disable_security"
    UNCONTROLLED_ROOT = "uncontrolled_root"
    HIDE_ACTIONS = "hide_actions"


RISK_LEVELS: dict[Action, RiskLevel] = {
    Action.READ_PROJECT: RiskLevel.LOW,
    Action.ANALYZE_CODE: RiskLevel.LOW,
    Action.RUN_LOCAL_TESTS: RiskLevel.LOW,
    Action.RUN_BUILD: RiskLevel.LOW,
    Action.EDIT_WORKTREE: RiskLevel.MEDIUM,
    Action.CREATE_TEMP: RiskLevel.MEDIUM,
    Action.APPLY_MERGE: RiskLevel.HIGH,
    Action.ROLLBACK: RiskLevel.HIGH,
    Action.CHANGE_ARCHITECTURE: RiskLevel.HIGH,
    Action.CHANGE_PUBLIC_API: RiskLevel.HIGH,
    Action.ADD_DEPENDENCY: RiskLevel.HIGH,
    Action.INSTALL_SYSTEM_PACKAGE: RiskLevel.HIGH,
    Action.CHANGE_SYSTEM_CONFIG: RiskLevel.HIGH,
    Action.PRIVILEGED_OPERATION: RiskLevel.HIGH,
    Action.CHANGE_DEFAULT_BRANCH: RiskLevel.HIGH,
    Action.PUBLISH: RiskLevel.HIGH,
    Action.GIT_PUSH: RiskLevel.HIGH,
    Action.GIT_REMOTE_CHANGE: RiskLevel.HIGH,
    Action.GITHUB_REPOSITORY_CREATE: RiskLevel.HIGH,
    Action.CONFIG_IMPORT: RiskLevel.HIGH,
    Action.STAGE_APPROVAL: RiskLevel.HIGH,
    Action.PROVIDER_RECOVERY: RiskLevel.HIGH,
    Action.DELETE_IMPORTANT_DATA: RiskLevel.HIGH,
    Action.SEND_SECRET_EXTERNALLY: RiskLevel.HIGH,
    Action.DESTRUCTIVE_COMMAND: RiskLevel.HIGH,
    Action.TASK_APPROVAL: RiskLevel.HIGH,
    Action.BYPASS_PERMISSIONS: RiskLevel.CRITICAL,
    Action.EXPAND_OWN_PERMISSIONS: RiskLevel.CRITICAL,
    Action.READ_OTHER_SECRETS: RiskLevel.CRITICAL,
    Action.DISABLE_SECURITY: RiskLevel.CRITICAL,
    Action.UNCONTROLLED_ROOT: RiskLevel.CRITICAL,
    Action.HIDE_ACTIONS: RiskLevel.CRITICAL,
}

LEVELS: dict[Action, PermissionLevel] = {
    Action.READ_PROJECT: PermissionLevel.AUTO,
    Action.ANALYZE_CODE: PermissionLevel.AUTO,
    Action.EDIT_WORKTREE: PermissionLevel.AUTO,
    Action.RUN_LOCAL_TESTS: PermissionLevel.AUTO,
    Action.RUN_BUILD: PermissionLevel.AUTO,
    Action.CREATE_TEMP: PermissionLevel.AUTO,
    Action.APPLY_MERGE: PermissionLevel.APPROVAL_REQUIRED,
    Action.ROLLBACK: PermissionLevel.APPROVAL_REQUIRED,
    Action.CHANGE_ARCHITECTURE: PermissionLevel.APPROVAL_REQUIRED,
    Action.CHANGE_PUBLIC_API: PermissionLevel.APPROVAL_REQUIRED,
    Action.ADD_DEPENDENCY: PermissionLevel.APPROVAL_REQUIRED,
    Action.INSTALL_SYSTEM_PACKAGE: PermissionLevel.APPROVAL_REQUIRED,
    Action.CHANGE_SYSTEM_CONFIG: PermissionLevel.APPROVAL_REQUIRED,
    Action.PRIVILEGED_OPERATION: PermissionLevel.APPROVAL_REQUIRED,
    Action.CHANGE_DEFAULT_BRANCH: PermissionLevel.APPROVAL_REQUIRED,
    Action.PUBLISH: PermissionLevel.APPROVAL_REQUIRED,
    Action.DELETE_IMPORTANT_DATA: PermissionLevel.APPROVAL_REQUIRED,
    Action.SEND_SECRET_EXTERNALLY: PermissionLevel.APPROVAL_REQUIRED,
    Action.DESTRUCTIVE_COMMAND: PermissionLevel.APPROVAL_REQUIRED,
    Action.TASK_APPROVAL: PermissionLevel.APPROVAL_REQUIRED,
    Action.GIT_PUSH: PermissionLevel.APPROVAL_REQUIRED,
    Action.GIT_REMOTE_CHANGE: PermissionLevel.APPROVAL_REQUIRED,
    Action.GITHUB_REPOSITORY_CREATE: PermissionLevel.APPROVAL_REQUIRED,
    Action.CONFIG_IMPORT: PermissionLevel.APPROVAL_REQUIRED,
    Action.STAGE_APPROVAL: PermissionLevel.APPROVAL_REQUIRED,
    Action.PROVIDER_RECOVERY: PermissionLevel.APPROVAL_REQUIRED,
    Action.BYPASS_PERMISSIONS: PermissionLevel.DENIED,
    Action.EXPAND_OWN_PERMISSIONS: PermissionLevel.DENIED,
    Action.READ_OTHER_SECRETS: PermissionLevel.DENIED,
    Action.DISABLE_SECURITY: PermissionLevel.DENIED,
    Action.UNCONTROLLED_ROOT: PermissionLevel.DENIED,
    Action.HIDE_ACTIONS: PermissionLevel.DENIED,
}


class PermissionManager:
    """Programmatic policy, separate from any model prompt."""

    def level(self, action: Action) -> PermissionLevel:
        return LEVELS.get(action, PermissionLevel.DENIED)

    def risk(self, action: Action) -> RiskLevel:
        return RISK_LEVELS.get(action, RiskLevel.CRITICAL)

    def require_auto(self, action: Action) -> None:
        level = self.level(action)
        if level is PermissionLevel.DENIED:
            raise PermissionDenied(f"действие {action.value!r} запрещено политикой")
        if level is PermissionLevel.APPROVAL_REQUIRED:
            raise PermissionDenied(
                f"действие {action.value!r} требует подтверждения владельца и не может быть автоматическим"
            )

    def require_allowed(self, action: Action) -> None:
        if self.level(action) is PermissionLevel.DENIED:
            raise PermissionDenied(f"действие {action.value!r} запрещено политикой")


class ApprovalService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def request(
        self,
        *,
        action: Action,
        payload: dict[str, Any],
        created_by: int,
        task_id: int | None = None,
        ttl_seconds: int = 600,
    ) -> int:
        if LEVELS[action] is PermissionLevel.DENIED:
            raise PermissionDenied(
                f"действие {action.value!r} запрещено и не может быть подтверждено"
            )
        if ttl_seconds < 30 or ttl_seconds > 86400:
            raise ApprovalError("approval TTL must be between 30 and 86400 seconds")
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)
        cursor = await self.db.execute(
            """
            INSERT INTO approvals(task_id, action, payload_json, status, created_by,
                                  created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                action.value,
                json_dumps(payload),
                ApprovalStatus.PENDING.value,
                created_by,
                now.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
            ),
        )
        approval_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return approval_id

    async def grant_once(
        self,
        *,
        action: Action,
        payload: dict[str, Any],
        decided_by: int,
        task_id: int | None = None,
        ttl_seconds: int = 600,
    ) -> int:
        """Record an explicit, one-shot user decision made by an inline button."""
        approval_id = await self.request(
            action=action,
            payload=payload,
            created_by=decided_by,
            task_id=task_id,
            ttl_seconds=ttl_seconds,
        )
        await self.db.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ? WHERE id = ? AND status = ?",
            (
                ApprovalStatus.APPROVED.value,
                iso_now(),
                decided_by,
                approval_id,
                ApprovalStatus.PENDING.value,
            ),
        )
        return approval_id

    async def consume(
        self, approval_id: int, *, user_id: int, action: Action | None = None
    ) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ApprovalError("подтверждение не найдено")
            data = {key: row[key] for key in row.keys()}
            if int(data["decided_by"] or 0) != user_id:
                raise PermissionDenied("подтверждение выдано другому пользователю")
            if data["status"] != ApprovalStatus.APPROVED.value:
                raise ApprovalError("подтверждение не в состоянии approved")
            if action is not None and data["action"] != action.value:
                raise ApprovalError("подтверждение относится к другому действию")
            if datetime.fromisoformat(data["expires_at"]) <= datetime.now(UTC):
                await conn.execute(
                    "UPDATE approvals SET status = ? WHERE id = ?",
                    (ApprovalStatus.EXPIRED.value, approval_id),
                )
                raise ApprovalError("срок подтверждения истёк")
            await conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
                (
                    ApprovalStatus.EXECUTED.value,
                    iso_now(),
                    approval_id,
                    ApprovalStatus.APPROVED.value,
                ),
            )
            payload = json.loads(data["payload_json"])
            if not isinstance(payload, dict):
                raise ApprovalError("повреждён payload подтверждения")
            return payload

    async def approve_pending(self, approval_id: int, *, user_id: int) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ApprovalError("подтверждение не найдено")
            data = {key: row[key] for key in row.keys()}
            if int(data["created_by"]) != user_id:
                raise PermissionDenied("подтверждение выдано другому пользователю")
            if data["status"] != ApprovalStatus.PENDING.value:
                raise ApprovalError("подтверждение уже обработано")
            if datetime.fromisoformat(data["expires_at"]) <= datetime.now(UTC):
                await conn.execute(
                    "UPDATE approvals SET status = ? WHERE id = ?",
                    (ApprovalStatus.EXPIRED.value, approval_id),
                )
                raise ApprovalError("срок подтверждения истёк")
            await conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                (ApprovalStatus.APPROVED.value, iso_now(), user_id, approval_id),
            )
            payload = json.loads(data["payload_json"])
            if not isinstance(payload, dict):
                raise ApprovalError("повреждён payload подтверждения")
            return {"id": approval_id, "action": data["action"], "payload": payload}

    async def get(self, approval_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return row_to_dict(row)

    async def reject_pending(self, approval_id: int, *, user_id: int) -> bool:
        cursor = await self.db.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = ? AND created_by = ?",
            (
                ApprovalStatus.REJECTED.value,
                iso_now(),
                user_id,
                approval_id,
                ApprovalStatus.PENDING.value,
                user_id,
            ),
        )
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed

    async def latest_pending(
        self, *, user_id: int, task_id: int | None = None
    ) -> dict[str, Any] | None:
        if task_id is None:
            row = await self.db.fetchone(
                "SELECT * FROM approvals WHERE status = ? AND created_by = ? ORDER BY id DESC LIMIT 1",
                (ApprovalStatus.PENDING.value, user_id),
            )
        else:
            row = await self.db.fetchone(
                "SELECT * FROM approvals WHERE status = ? AND created_by = ? AND task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (ApprovalStatus.PENDING.value, user_id, task_id),
            )
        return row_to_dict(row)

    async def pending_for_task(self, task_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM approvals WHERE task_id = ? AND status = ? ORDER BY id",
            (task_id, ApprovalStatus.PENDING.value),
        )
        return [row_to_dict(row) or {} for row in rows]

    async def expire_old(self) -> int:
        cursor = await self.db.execute(
            "UPDATE approvals SET status = ? WHERE status = ? AND expires_at <= ?",
            (ApprovalStatus.EXPIRED.value, ApprovalStatus.PENDING.value, iso_now()),
        )
        count = cursor.rowcount
        await cursor.close()
        return count


def describe_action(action: Action) -> str:
    descriptions = {
        Action.APPLY_MERGE: "слияние изменений задачи в основную ветку",
        Action.ROLLBACK: "удаление worktree и ветки задачи (изменения сохраняются в patch)",
        Action.ADD_DEPENDENCY: "добавление существенной зависимости",
        Action.INSTALL_SYSTEM_PACKAGE: "установка системного пакета",
        Action.PUBLISH: "публикация изменений",
        Action.DELETE_IMPORTANT_DATA: "удаление важных данных",
    }
    return descriptions.get(action, action.value)
