from __future__ import annotations

import pytest

from ai_supervisor.config import ModelPrice
from ai_supervisor.core.usage import UsageService
from ai_supervisor.models import TokenUsage


@pytest.mark.asyncio
async def test_per_model_pricing_and_period_totals(database, settings) -> None:
    settings.usage.prices = {
        "kimi/k3.1-pro": ModelPrice(
            input_per_mtok=10, output_per_mtok=30, cache_read_per_mtok=1, currency="USD"
        )
    }
    service = UsageService(database, settings)
    await service.record(
        provider="kimi",
        model="k3.1-pro",
        usage=TokenUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cached_tokens=200_000,
            requests=1,
            duration_ms=1234,
        ),
    )
    totals = await service.by_model()
    value = totals[("kimi", "k3.1-pro")]
    assert value.cost == pytest.approx(10 * 0.8 + 1 * 0.2 + 30)
    assert value.currency == "USD"
    assert value.duration_ms == 1234
    today = await service.today_totals()
    assert today.total_tokens == 2_000_000
    assert today.cost == pytest.approx(value.cost)
    assert await service.week_totals()
    assert await service.month_totals()
    await service.record(
        provider="kimi",
        model="k3.1-pro",
        usage=TokenUsage(input_tokens=10, output_tokens=2, requests=1),
        role="planning",
        prompt_version="2.6.0",
        routing_level=2,
        context_chars=123,
    )
    by_role = await service.by_role()
    assert by_role["planning"].total_tokens == 12


@pytest.mark.asyncio
async def test_display_currency_is_explicit_estimate(database, settings) -> None:
    settings.usage.prices = {
        "kimi/k3.1-pro": ModelPrice(input_per_mtok=10, output_per_mtok=30, currency="USD")
    }
    settings.usage.display_currency = "RUB"
    settings.usage.fx_rates = {"USD": 90.0}
    service = UsageService(database, settings)
    await service.record(
        provider="kimi",
        model="k3.1-pro",
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0, requests=1),
    )
    totals = (await service.by_model())[("kimi", "k3.1-pro")]
    rendered = service.format_totals(totals)
    assert "факт. стоимость 10.0000 USD" in rendered
    assert "≈ 900.00 RUB" in rendered
    assert "локальная оценка" in rendered
