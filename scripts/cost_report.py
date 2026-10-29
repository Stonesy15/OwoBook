"""Cost per voice note, photo and text message, from usage_log (real model calls only).

    uv run python scripts/cost_report.py
    uv run python scripts/cost_report.py --days 7 --usd-ngn 1550     # last week, also in naira

Prices are in owobook/usage.py; cached-input discounts are ignored, so these are upper bounds.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owobook.db import migrate, session_scope  # noqa: E402
from owobook.usage import turns  # noqa: E402


def money(usd: float, rate: float | None) -> str:
    return f"${usd:.4f}" + (f" (₦{usd * rate:,.2f})" if rate else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, help="only the last N days")
    parser.add_argument("--usd-ngn", type=float, help="exchange rate, to also show naira")
    args = parser.parse_args()

    migrate()
    since = datetime.now(timezone.utc) - timedelta(days=args.days) if args.days else None
    with session_scope() as db:
        all_turns = turns(db, since)
    voice = [t for t in all_turns if t.kind == "voice"]
    text = [t for t in all_turns if t.kind == "text"]
    photo = [t for t in all_turns if t.kind == "photo"]
    rate = args.usd_ngn

    if not all_turns:
        print("No real model calls logged yet. Send OwoBook a voice note or text first.")
        return
    print(f"Messages that used the models: {len(voice)} voice notes, {len(photo)} photos, "
          f"{len(text)} text messages\n")
    if voice:
        avg = mean(t.usd for t in voice)
        print("Per voice note (average):")
        print(f"  Muse Voice Transcribe  {money(mean(t.asr_usd for t in voice), rate)}"
              f"  · {mean(t.audio_seconds for t in voice):.1f}s of audio")
        print(f"  Muse Spark             {money(mean(t.llm_usd for t in voice), rate)}"
              f"  · {mean(t.llm_calls for t in voice):.1f} calls, {mean(t.input_tokens for t in voice):,.0f} in"
              f" / {mean(t.output_tokens for t in voice):,.0f} out tokens")
        print(f"  Total                  {money(avg, rate)}   → 1,000 voice notes ≈ {money(avg * 1000, rate)}\n")
    if photo:
        avg = mean(t.usd for t in photo)
        print(f"Per photo (average): {money(avg, rate)}  · {mean(t.llm_calls for t in photo):.1f} Muse Spark calls,"
              f" {mean(t.input_tokens for t in photo):,.0f} in / {mean(t.output_tokens for t in photo):,.0f} out tokens\n")
    if text:
        avg = mean(t.usd for t in text)
        print(f"Per text message (average): {money(avg, rate)}"
              f"  · {mean(t.input_tokens for t in text):,.0f} in / {mean(t.output_tokens for t in text):,.0f} out tokens")
    print(f"\nTotal spent: {money(sum(t.usd for t in all_turns), rate)}")


if __name__ == "__main__":
    main()
