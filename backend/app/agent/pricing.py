from decimal import Decimal

# Estimated USD per 1,000 tokens, using the phase 03 reference price table.
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # Conservative peak/cache-miss estimate, checked 2026-09-12:
    # https://api-docs.deepseek.com/quick_start/pricing/
    "deepseek-flash": (Decimal("0.0003"), Decimal("0.0012")),
    "gpt-4o": (Decimal("0.0025"), Decimal("0.01")),
    "gpt-4o-mini": (Decimal("0.00015"), Decimal("0.0006")),
    "deepseek-chat": (Decimal("0.00027"), Decimal("0.0011")),
}


def estimate_cost_usd(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> Decimal | None:
    """Estimate cost with Decimal; unknown model prices remain unknown."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    prompt_price, completion_price = pricing
    return (
        prompt_price * Decimal(prompt_tokens) / 1000
        + completion_price * Decimal(completion_tokens) / 1000
    ).quantize(Decimal("0.000001"))
