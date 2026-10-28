"""Usage and cost tracking — one usage_log row per model call, for 'cost per voice note'."""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from owobook.models import MessageLog, UsageLog

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
    # Baseten Model APIs, from baseten.co/pricing (28 Oct 2026).
    "meta-models/muse-spark-open-1026": (1.25, 4.25),
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


@dataclass
class Turn:
    """One inbound message and every model call it caused."""

    kind: str  # "voice" | "text"
    llm_usd: float = 0.0
    asr_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    audio_seconds: float = 0.0
    llm_calls: int = 0

    @property
    def usd(self) -> float:
        return self.llm_usd + self.asr_usd


def turns(db: Session, since: datetime | None = None) -> list[Turn]:
    """Group usage_log rows by the inbound message that caused them.

    usage_log has no message id, so we replay each trader's events in time order. A voice note logs its
    transcription first, then the inbound audio message, then the Muse Spark calls; a text message logs
    the message, then the calls. Messages that made no model call (button taps, onboarding) are left out.
    """
    events: dict[int | None, list[tuple[datetime, int, object]]] = defaultdict(list)
    usage = select(UsageLog).where(UsageLog.provider != "fake")  # simulator/test runs cost nothing
    inbound = select(MessageLog).where(MessageLog.direction == "in", MessageLog.type.in_(("text", "audio")))
    if since:
        usage, inbound = usage.where(UsageLog.created_at >= since), inbound.where(MessageLog.created_at >= since)
    for u in db.scalars(usage):
        events[u.trader_id].append((u.created_at, 1, u))
    for m in db.scalars(inbound):
        events[m.trader_id].append((m.created_at, 0, m))

    out: list[Turn] = []
    for rows in events.values():
        current: Turn | None = None
        pending_voice = False  # a transcription was seen, its audio message not yet
        for _, _, ev in sorted(rows, key=lambda r: (r[0], r[1])):
            if isinstance(ev, MessageLog):
                if ev.type == "audio" and pending_voice:
                    pending_voice = False  # same voice note as the transcription just before it
                    continue
                current = Turn("voice" if ev.type == "audio" else "text")
                out.append(current)
            elif ev.audio_seconds:
                current, pending_voice = Turn("voice"), True
                out.append(current)
                current.asr_usd += ev.est_cost_usd
                current.audio_seconds += ev.audio_seconds
            elif current is not None:
                current.llm_usd += ev.est_cost_usd
                current.input_tokens += ev.input_tokens
                current.output_tokens += ev.output_tokens
                current.llm_calls += 1
    return [t for t in out if t.usd > 0 or t.llm_calls]
