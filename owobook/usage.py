"""Usage and cost tracking — one usage_log row per model call, for 'cost per voice note'."""

from sqlalchemy.orm import Session

from owobook.models import UsageLog

# USD per 1M tokens (input, output). Filled in with real prices in Phase 2.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "fake-rules-1": (0.0, 0.0),
}
# USD per audio minute for transcription models. Filled in Phase 2.
PRICES_PER_AUDIO_MIN: dict[str, float] = {}


def estimate_cost(model: str, input_tokens: int = 0, output_tokens: int = 0, audio_seconds: float = 0.0) -> float:
    in_price, out_price = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    cost = input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price
    cost += audio_seconds / 60 * PRICES_PER_AUDIO_MIN.get(model, 0.0)
    return round(cost, 8)


def log_usage(
    db: Session,
    *,
    trader_id: int | None,
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    audio_seconds: float = 0.0,
) -> UsageLog:
    row = UsageLog(
        trader_id=trader_id,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        audio_seconds=audio_seconds,
        est_cost_usd=estimate_cost(model, input_tokens, output_tokens, audio_seconds),
    )
    db.add(row)
    db.flush()
    return row
