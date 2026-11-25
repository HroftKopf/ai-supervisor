from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_supervisor.config import Settings
from ai_supervisor.memory.database import Database, iso_now
from ai_supervisor.models import TokenUsage


class LimitExceeded(RuntimeError):
    def __init__(self, message: str, *, limit: str) -> None:
        super().__init__(message)
        self.limit = limit


@dataclass(slots=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    requests: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def record(
        self,
        *,
        provider: str,
        model: str,
        usage: TokenUsage,
        task_id: int | None = None,
        project_id: int | None = None,
    ) -> None:
        if usage.requests == 0 and usage.total_tokens == 0:
            return
        await self.db.execute(
            """
            INSERT INTO usage_events(task_id, project_id, provider, model, input_tokens,
                output_tokens, cached_tokens, cache_write_tokens, reasoning_tokens, requests, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                project_id,
                provider,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                usage.cache_write_tokens,
                usage.reasoning_tokens,
                usage.requests,
                iso_now(),
            ),
        )

    async def _totals(self, where: str, params: tuple[Any, ...]) -> UsageTotals:
        row = await self.db.fetchone(
            f"""
            SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cached_tokens),0) AS cached_tokens,
                   COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,
                   COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                   COALESCE(SUM(requests),0) AS requests
            FROM usage_events {where}
            """,
            params,
        )
        if row is None:
            return UsageTotals()
        return UsageTotals(
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cached_tokens=int(row["cached_tokens"]),
            cache_write_tokens=int(row["cache_write_tokens"]),
            reasoning_tokens=int(row["reasoning_tokens"]),
            requests=int(row["requests"]),
        )

    async def task_totals(self, task_id: int) -> UsageTotals:
        return await self._totals("WHERE task_id = ?", (task_id,))

    async def project_totals(self, project_id: int) -> UsageTotals:
        return await self._totals("WHERE project_id = ?", (project_id,))

    async def daily_totals(self, day: datetime | None = None) -> UsageTotals:
        target = (day or datetime.now(UTC)).astimezone(UTC).date()
        start = datetime(target.year, target.month, target.day, tzinfo=UTC)
        end = start + timedelta(days=1)
        return await self._totals(
            "WHERE created_at >= ? AND created_at < ?",
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        )

    async def task_provider_requests(self, task_id: int, provider: str) -> int:
        value = await self.db.scalar(
            "SELECT COALESCE(SUM(requests),0) FROM usage_events WHERE task_id = ? AND provider = ?",
            (task_id, provider),
        )
        return int(value or 0)

    async def by_provider(
        self, *, task_id: int | None = None, project_id: int | None = None
    ) -> dict[str, UsageTotals]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self.db.fetchall(
            f"""
            SELECT provider,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cached_tokens),0) AS cached_tokens,
                   COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,
                   COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                   COALESCE(SUM(requests),0) AS requests
            FROM usage_events {where} GROUP BY provider
            """,
            params,
        )
        result: dict[str, UsageTotals] = {}
        for row in rows:
            result[str(row["provider"])] = UsageTotals(
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cached_tokens=int(row["cached_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
                reasoning_tokens=int(row["reasoning_tokens"]),
                requests=int(row["requests"]),
            )
        return result

    async def ensure_kimi_budget(self, task_id: int) -> None:
        task_requests = await self.task_provider_requests(task_id, "kimi")
        if task_requests >= self.settings.limits.max_task_kimi_requests:
            raise LimitExceeded(
                "Лимит запросов Kimi на задачу исчерпан "
                f"({task_requests}/{self.settings.limits.max_task_kimi_requests}).",
                limit="task_kimi_requests",
            )
        if task_id > 0 and self.settings.limits.max_task_total_tokens > 0:
            task_totals = await self.task_totals(task_id)
            if task_totals.total_tokens >= self.settings.limits.max_task_total_tokens:
                raise LimitExceeded(
                    "Лимит токенов на задачу исчерпан "
                    f"({task_totals.total_tokens}/{self.settings.limits.max_task_total_tokens}).",
                    limit="task_tokens",
                )
        daily = await self.daily_totals()
        if self.settings.limits.daily_token_limit > 0:
            if daily.total_tokens >= self.settings.limits.daily_token_limit:
                raise LimitExceeded(
                    "Суточный лимит токенов исчерпан "
                    f"({daily.total_tokens}/{self.settings.limits.daily_token_limit}).",
                    limit="daily_tokens",
                )
        if self.settings.limits.daily_request_limit > 0:
            if daily.requests >= self.settings.limits.daily_request_limit:
                raise LimitExceeded(
                    "Суточный лимит запросов исчерпан "
                    f"({daily.requests}/{self.settings.limits.daily_request_limit}).",
                    limit="daily_requests",
                )

    async def warning(self) -> str | None:
        daily = await self.daily_totals()
        limit = self.settings.limits.daily_token_limit
        if limit <= 0:
            return None
        percent = int((daily.total_tokens / limit) * 100)
        if percent >= self.settings.limits.warn_at_percent:
            return (
                f"Использовано {percent}% суточного лимита токенов ({daily.total_tokens}/{limit})."
            )
        return None

    def estimate_cost(self, totals: UsageTotals, provider: str) -> float | None:
        usage = self.settings.usage
        if not usage.configured:
            return None
        if provider == "kimi":
            input_price = usage.kimi_input
            output_price = usage.kimi_output
        elif provider == "deepseek":
            input_price = usage.deepseek_input
            output_price = usage.deepseek_output
        else:
            return None
        if input_price <= 0 and output_price <= 0:
            return None
        return (totals.input_tokens / 1_000_000) * input_price + (
            totals.output_tokens / 1_000_000
        ) * output_price

    @staticmethod
    def format_totals(totals: UsageTotals) -> str:
        return (
            f"вход {totals.input_tokens:,}, выход {totals.output_tokens:,}, "
            f"кэш-чтение {totals.cached_tokens:,}, кэш-запись {totals.cache_write_tokens:,}, "
            f"reasoning {totals.reasoning_tokens:,}, запросов {totals.requests:,}"
        ).replace(",", " ")
