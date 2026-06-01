# Pricing per million tokens — update when Anthropic changes rates.
# Source: https://www.anthropic.com/pricing
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku": {
        "input":       0.80,
        "output":      4.00,
        "cache_read":  0.08,
        "cache_write": 1.00,
    },
    "claude-sonnet": {
        "input":       3.00,
        "output":     15.00,
        "cache_read":  0.30,
        "cache_write": 3.75,
    },
    "claude-opus": {
        "input":      15.00,
        "output":     75.00,
        "cache_read":  1.50,
        "cache_write": 18.75,
    },
}

# ET multipliers (relative cost index — kept for spike detection ratios)
MODEL_MULTIPLIERS = {
    "claude-haiku":  0.25,
    "claude-sonnet": 1.0,
    "claude-opus":   5.0,
}

_DEFAULT_PRICING = MODEL_PRICING["claude-sonnet"]
_DEFAULT_MULTIPLIER = 1.0


def _match(model_string: str) -> str | None:
    for key in MODEL_PRICING:
        if key in model_string:
            return key
    return None


def get_multiplier(model_string: str) -> float:
    key = _match(model_string)
    return MODEL_MULTIPLIERS.get(key, _DEFAULT_MULTIPLIER) if key else _DEFAULT_MULTIPLIER


def get_pricing(model_string: str) -> dict[str, float]:
    key = _match(model_string)
    return MODEL_PRICING.get(key, _DEFAULT_PRICING) if key else _DEFAULT_PRICING


def calculate_et(usage: dict, model: str) -> float:
    m = get_multiplier(model)
    i = usage.get("input_tokens", 0)
    c = usage.get("cache_read_input_tokens", 0)
    o = usage.get("output_tokens", 0)
    return m * (1.0 * i + 0.1 * c + 4.0 * o)


def calculate_cache_write_et(usage: dict, model: str) -> float:
    m = get_multiplier(model)
    w = usage.get("cache_creation_input_tokens", 0)
    return m * w


def calculate_cost_usd(usage: dict, model: str) -> float:
    """Compute actual dollar cost from token counts and model pricing."""
    p = get_pricing(model)
    i   = usage.get("input_tokens", 0)
    cr  = usage.get("cache_read_input_tokens", 0)
    cw  = usage.get("cache_creation_input_tokens", 0)
    o   = usage.get("output_tokens", 0)
    return (
        i  * p["input"]       / 1_000_000
        + cr * p["cache_read"]  / 1_000_000
        + cw * p["cache_write"] / 1_000_000
        + o  * p["output"]      / 1_000_000
    )


def cost_usd_from_totals(
    input_tokens: int,
    cache_read: int,
    cache_write: int,
    output_tokens: int,
    model: str,
) -> float:
    """Compute dollar cost from already-aggregated token totals."""
    return calculate_cost_usd(
        {
            "input_tokens":              input_tokens,
            "cache_read_input_tokens":   cache_read,
            "cache_creation_input_tokens": cache_write,
            "output_tokens":             output_tokens,
        },
        model,
    )


def fmt_usd(amount: float) -> str:
    """Format a dollar amount for display."""
    if amount >= 100:
        return f"${amount:.2f}"
    if amount >= 1:
        return f"${amount:.3f}"
    if amount >= 0.001:
        return f"${amount:.4f}"
    return f"${amount:.6f}"
