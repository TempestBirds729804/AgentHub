from decimal import Decimal

from app.agent.pricing import estimate_cost_usd


def test_known_price() -> None:
    assert estimate_cost_usd(
        model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500
    ) == Decimal("0.000450")


def test_unknown_price() -> None:
    assert (
        estimate_cost_usd(
            model="local-model", prompt_tokens=1000, completion_tokens=500
        )
        is None
    )


def test_zero_tokens() -> None:
    assert estimate_cost_usd(
        model="gpt-4o", prompt_tokens=0, completion_tokens=0
    ) == Decimal("0")


def test_deepseek_flash_estimate() -> None:
    assert estimate_cost_usd(
        model="deepseek-flash", prompt_tokens=1000, completion_tokens=500
    ) == Decimal("0.000900")
