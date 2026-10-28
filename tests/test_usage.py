from datetime import datetime, timedelta, timezone

from owobook import ledger
from owobook.models import MessageLog, UsageLog
from owobook.usage import estimate_cost, turns

T0 = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_usage_is_grouped_by_the_message_that_caused_it(db):
    trader, _ = ledger.get_or_create_trader(db, "2348011112222")
    tid = trader.id
    db.add_all([
        # voice note: transcription is logged before the inbound audio message, then two Muse Spark calls
        UsageLog(trader_id=tid, provider="meta", model="muse-voice-transcribe-1.0", audio_seconds=20,
                 est_cost_usd=0.001, created_at=at(0)),
        MessageLog(trader_id=tid, direction="in", type="audio", content="Mama Bisi collect", created_at=at(1)),
        UsageLog(trader_id=tid, provider="meta", model="muse-spark-1.3", input_tokens=3000, output_tokens=200,
                 est_cost_usd=0.005, created_at=at(2)),
        UsageLog(trader_id=tid, provider="meta", model="muse-spark-1.3", input_tokens=3100, output_tokens=50,
                 est_cost_usd=0.004, created_at=at(3)),
        # button tap: no model call, left out
        MessageLog(trader_id=tid, direction="in", type="button", content="confirm:1", created_at=at(10)),
        # text message with one call
        MessageLog(trader_id=tid, direction="in", type="text", content="who owe me", created_at=at(20)),
        UsageLog(trader_id=tid, provider="meta", model="muse-spark-1.3", input_tokens=3000, output_tokens=100,
                 est_cost_usd=0.004, created_at=at(21)),
        # simulator run with the fake provider: ignored
        MessageLog(trader_id=tid, direction="in", type="text", content="hi", created_at=at(30)),
        UsageLog(trader_id=tid, provider="fake", model="fake-rules-1", created_at=at(31)),
    ])
    db.flush()

    voice, text = turns(db)
    assert voice.kind == "voice" and voice.llm_calls == 2 and voice.audio_seconds == 20
    assert round(voice.usd, 6) == 0.010 and voice.input_tokens == 6100
    assert text.kind == "text" and text.llm_calls == 1 and round(text.usd, 6) == 0.004


def test_estimate_cost_uses_meta_prices():
    assert estimate_cost("muse-spark-1.3", 1_000_000, 1_000_000) == 1.25 + 4.25
    assert estimate_cost("muse-voice-transcribe-1.0", audio_seconds=3600) == 0.18
