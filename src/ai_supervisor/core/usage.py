from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
    duration_ms: int = 0
    errors: int = 0
    cost: float = 0.0
    currency: str = ""

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
        duration_ms: int = 0,
        error_category: str | None = None,
        role: str | None = None,
        prompt_version: str | None = None,
        routing_level: int | None = None,
        context_chars: int | None = None,
        raw_usage: dict[str, Any] | None = None,
    ) -> None:
        if usage.requests == 0 and usage.total_tokens == 0:
            return
        cost, currency = self.estimate_cost_amount(usage, provider, model)
        await self.db.execute(
            """
            INSERT INTO usage_events(task_id, project_id, provider, model, input_tokens,
                output_tokens, cached_tokens, cache_write_tokens, reasoning_tokens, requests,
                duration_ms, error_category, cost, currency, prompt_version, role,
                routing_level, context_chars, raw_usage_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                max(0, duration_ms or usage.duration_ms),
                error_category,
                cost,
                currency,
                prompt_version,
                role,
                routing_level,
                context_chars,
                __import__("json").dumps(raw_usage, ensure_ascii=False)[:20000]
                if raw_usage
                else None,
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
                   COALESCE(SUM(requests),0) AS requests,
                   COALESCE(SUM(duration_ms),0) AS duration_ms,
                   COALESCE(SUM(CASE WHEN error_category IS NOT NULL THEN 1 ELSE 0 END),0) AS errors,
                   COALESCE(SUM(cost),0) AS cost,
                   COALESCE(MAX(currency), '') AS currency
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
            duration_ms=int(row["duration_ms"]),
            errors=int(row["errors"]),
            cost=float(row["cost"] or 0.0),
            currency=str(row["currency"] or ""),
        )

    async def task_totals(self, task_id: int) -> UsageTotals:
        return await self._totals("WHERE task_id = ?", (task_id,))

    async def project_totals(self, project_id: int) -> UsageTotals:
        return await self._totals("WHERE project_id = ?", (project_id,))

    async def daily_totals(self, day: datetime | None = None) -> UsageTotals:
        if day is None:
            return await self.today_totals()
        zone = ZoneInfo(self.settings.app.timezone)
        local = day.astimezone(zone)
        start = datetime.combine(local.date(), time.min, tzinfo=zone).astimezone(UTC)
        return await self.range_totals(start, start + timedelta(days=1))

    def _local_day_start(self, *, days_ago: int = 0) -> datetime:
        zone = ZoneInfo(self.settings.app.timezone)
        local_now = datetime.now(zone)
        local_start = datetime.combine(
            local_now.date() - timedelta(days=days_ago), time.min, tzinfo=zone
        )
        return local_start.astimezone(UTC)

    async def range_totals(self, start: datetime, end: datetime) -> UsageTotals:
        return await self._totals(
            "WHERE created_at >= ? AND created_at < ?",
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        )

    async def today_totals(self) -> UsageTotals:
        start = self._local_day_start()
        return await self.range_totals(start, start + timedelta(days=1))

    async def week_totals(self) -> UsageTotals:
        start = self._local_day_start(days_ago=6)
        return await self.range_totals(start, start + timedelta(days=7))

    async def month_totals(self) -> UsageTotals:
        start = self._local_day_start(days_ago=29)
        return await self.range_totals(start, start + timedelta(days=30))

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
                   COALESCE(SUM(requests),0) AS requests,
                   COALESCE(SUM(duration_ms),0) AS duration_ms,
                   COALESCE(SUM(CASE WHEN error_category IS NOT NULL THEN 1 ELSE 0 END),0) AS errors,
                   COALESCE(SUM(cost),0) AS cost,
                   COALESCE(MAX(currency), '') AS currency
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
                duration_ms=int(row["duration_ms"]),
                errors=int(row["errors"]),
                cost=float(row["cost"] or 0.0),
                currency=str(row["currency"] or ""),
            )
        return result

    async def by_role(self, *, task_id: int | None = None) -> dict[str, UsageTotals]:
        where = "WHERE task_id = ?" if task_id is not None else ""
        params: tuple[Any, ...] = (task_id,) if task_id is not None else ()
        rows = await self.db.fetchall(
            f"SELECT COALESCE(role, 'unknown') AS role, COUNT(*) AS events, "
            f"COALESCE(SUM(input_tokens),0) AS input_tokens, "
            f"COALESCE(SUM(output_tokens),0) AS output_tokens, "
            f"COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
            f"COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
            f"COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens, "
            f"COALESCE(SUM(requests),0) AS requests, COALESCE(SUM(duration_ms),0) AS duration_ms, "
            f"COALESCE(SUM(cost),0) AS cost, COALESCE(MAX(currency),'') AS currency "
            f"FROM usage_events {where} GROUP BY role",
            params,
        )
        result: dict[str, UsageTotals] = {}
        for row in rows:
            result[str(row["role"])] = UsageTotals(
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cached_tokens=int(row["cached_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
                reasoning_tokens=int(row["reasoning_tokens"]),
                requests=int(row["requests"]),
                duration_ms=int(row["duration_ms"]),
                cost=float(row["cost"] or 0),
                currency=str(row["currency"] or ""),
            )
        return result

    async def by_model(
        self,
        *,
        task_id: int | None = None,
        project_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[tuple[str, str], UsageTotals]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if start is not None:
            clauses.append("created_at >= ?")
            params.append(start.isoformat(timespec="seconds"))
        if end is not None:
            clauses.append("created_at < ?")
            params.append(end.isoformat(timespec="seconds"))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self.db.fetchall(
            f"""
            SELECT provider, model,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(cached_tokens),0) AS cached_tokens,
                   COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,
                   COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                   COALESCE(SUM(requests),0) AS requests,
                   COALESCE(SUM(duration_ms),0) AS duration_ms,
                   COALESCE(SUM(CASE WHEN error_category IS NOT NULL THEN 1 ELSE 0 END),0) AS errors,
                   COALESCE(SUM(cost),0) AS cost,
                   COALESCE(MAX(currency), '') AS currency
            FROM usage_events {where} GROUP BY provider, model
            """,
            params,
        )
        result: dict[tuple[str, str], UsageTotals] = {}
        for row in rows:
            result[(str(row["provider"]), str(row["model"]))] = UsageTotals(
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cached_tokens=int(row["cached_tokens"]),
                cache_write_tokens=int(row["cache_write_tokens"]),
                reasoning_tokens=int(row["reasoning_tokens"]),
                requests=int(row["requests"]),
                duration_ms=int(row["duration_ms"]),
                errors=int(row["errors"]),
                cost=float(row["cost"] or 0.0),
                currency=str(row["currency"] or ""),
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

    def estimate_cost(
        self, totals: UsageTotals, provider: str, model: str | None = None
    ) -> tuple[float, str] | None:
        price = self.settings.usage.price_for(provider, model or "")
        if price is None or not price.configured:
            return None
        cache_read = totals.cached_tokens
        cache_write = totals.cache_write_tokens
        uncached = max(0, totals.input_tokens - cache_read - cache_write)
        input_rate = price.input_per_mtok or price.cache_read_per_mtok
        cost = (
            uncached / 1_000_000 * input_rate
            + cache_read / 1_000_000 * (price.cache_read_per_mtok or input_rate)
            + cache_write / 1_000_000 * (price.cache_write_per_mtok or input_rate)
            + totals.output_tokens / 1_000_000 * price.output_per_mtok
        )
        return cost, price.currency

    def estimate_cost_amount(
        self, usage: TokenUsage, provider: str, model: str
    ) -> tuple[float, str]:
        totals = UsageTotals(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            requests=usage.requests,
        )
        estimate = self.estimate_cost(totals, provider, model)
        return estimate if estimate is not None else (0.0, "")

    def format_totals(self, totals: UsageTotals) -> str:
        text = (
            f"вход {totals.input_tokens:,}, выход {totals.output_tokens:,}, "
            f"кэш-чтение {totals.cached_tokens:,}, кэш-запись {totals.cache_write_tokens:,}, "
            f"reasoning {totals.reasoning_tokens:,}, запросов {totals.requests:,}"
        ).replace(",", " ")
        if totals.cost > 0 and totals.currency:
            text += f", факт. стоимость {totals.cost:.4f} {totals.currency}"
            display = self.settings.usage.display_currency
            rate = (
                self.settings.usage.fx_rates.get(totals.currency)
                or self.settings.usage.fx_rates.get(f"{totals.currency}/{display}")
                if display
                else None
            )
            if display and rate:
                text += f" (≈ {totals.cost * rate:.2f} {display}, локальная оценка)"
        return text
