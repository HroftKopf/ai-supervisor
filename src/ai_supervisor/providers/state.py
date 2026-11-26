from __future__ import annotations

from typing import Any

from ai_supervisor.memory.database import Database, iso_now


class ProviderStateStore:
    """Persisted provider/account state. Never stores credentials."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, provider_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM provider_states WHERE provider_id = ?", (provider_id,)
        )
        return dict(row) if row else None

    async def list(self) -> dict[str, dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM provider_states")
        return {str(row["provider_id"]): dict(row) for row in rows}

    async def set_state(
        self,
        provider_id: str,
        *,
        state: str,
        category: str = "",
        reason: str = "",
        retry_not_before: str | None = None,
        account_scope_id: str = "default",
    ) -> None:
        now = iso_now()
        await self.db.execute(
            """
            INSERT INTO provider_states(provider_id, account_scope_id, state,
                failure_category, detected_at, last_checked_at, retry_not_before,
                consecutive_failures, reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                account_scope_id=excluded.account_scope_id,
                state=excluded.state,
                failure_category=excluded.failure_category,
                detected_at=excluded.detected_at,
                last_checked_at=excluded.last_checked_at,
                retry_not_before=excluded.retry_not_before,
                consecutive_failures=provider_states.consecutive_failures + 1,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (
                provider_id,
                account_scope_id,
                state,
                category or None,
                now,
                now,
                retry_not_before,
                reason[:1000],
                now,
            ),
        )

    async def set_available(self, provider_id: str) -> None:
        await self.db.execute(
            """
            INSERT INTO provider_states(provider_id, account_scope_id, state,
                detected_at, last_checked_at, consecutive_failures, reason, updated_at)
            VALUES (?, 'default', 'AVAILABLE', ?, ?, 0, '', ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                state='AVAILABLE', failure_category=NULL, detected_at=NULL,
                last_checked_at=excluded.last_checked_at, retry_not_before=NULL,
                consecutive_failures=0, reason='', updated_at=excluded.updated_at
            """,
            (provider_id, iso_now(), iso_now(), iso_now()),
        )
