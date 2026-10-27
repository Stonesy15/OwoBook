"""Usage and cost tracking — one usage_log row per model call, for 'cost per voice note'."""

import logging

from sqlalchemy.orm import Session

from owobook.models import UsageLog

log = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Meta prices from dev.meta.ai/docs/pricing-rate-limits (27 Oct 2026).
# Cached-input discounts are ignored, so these are upper-bound estimates.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-spark-1.2": (1.25, 4.25),
    "muse-spark-1.1": (1.25, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.20),
    "muse-spark-1.2-contributor": (0.10, 0.20),
    "fake-rules-1": (0.0, 0.0),
    # Baseten: add your model's price from the Baseten Model APIs page.
}
# USD per audio minute. Muse Voice Transcribe: $0.18/hour, billed in whole seconds (rounded down).
PRICES_PER_AUDIO_MIN: dict[str, float] = {
    "muse-voice-transcribe-1.0": 0.18 / 60,
    "fake-asr-1": 0.0,
}
_warned: set[str] = set()


def estimate_cost(model: str, input_tokens: int = 0, output_tokens: int = 0, audio_seconds: float = 0.0) -> float:
    if model not in PRICES_PER_MTOK and model not in PRICES_PER_AUDIO_MIN and model not in _warned:
        _warned.add(model)
        log.warning("no price known for model %r; cost logged as $0 (add it in usage.py)", model)
    in_price, out_price = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    cost = input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price
    cost += int(audio_seconds) / 60 * PRICES_PER_AUDIO_MIN.get(model, 0.0)
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
