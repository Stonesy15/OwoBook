"""Run the daily debt reminders now, instead of waiting for 08:00.

    uv run python scripts/run_reminders.py --dry-run                 # print what would be sent
    uv run python scripts/run_reminders.py --dry-run --date 2026-11-02   # pretend it's another day
    uv run python scripts/run_reminders.py --wa-id 2348012345678     # really send, to one trader
    uv run python scripts/run_reminders.py --wa-id 2348012345678 --force  # send again today

Real sends go through WhatsApp, so the trader must have messaged OwoBook in the last 24 hours
(or WA_REMINDER_TEMPLATE must be set).
"""

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owobook.config import get_settings  # noqa: E402
from owobook.db import SessionLocal, migrate  # noqa: E402
from owobook.reminders import run_reminders  # noqa: E402
from owobook.whatsapp.client import WhatsAppClient  # noqa: E402
from owobook.whatsapp.formatting import render_plain  # noqa: E402

BOLD, DIM, GREEN, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[0m"
STATUS = {
    "sent": "sent ✅",
    "template": "sent as template ✅ (outside the 24h window)",
    "no_template": "skipped: trader hasn't messaged in 24h and WA_REMINDER_TEMPLATE is not set",
    "already_sent": "skipped: already reminded today (use --force)",
    "dry_run": "dry run (nothing sent)",
    "error": "error — see log above",
}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the messages, send nothing")
    parser.add_argument("--wa-id", help="only this trader")
    parser.add_argument("--date", type=date.fromisoformat, help="pretend today is YYYY-MM-DD (Lagos)")
    parser.add_argument("--force", action="store_true", help="send even if already reminded today")
    args = parser.parse_args()

    migrate()
    logging.basicConfig(level=logging.INFO, format=f"{DIM}  · %(message)s{RESET}", force=True)
    for noisy in ("alembic", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = get_settings()
    whatsapp = None if args.dry_run else WhatsAppClient(settings)
    try:
        results = await run_reminders(
            SessionLocal, whatsapp, settings, today=args.date, dry_run=args.dry_run,
            only_wa_id=args.wa_id, force=args.force,
        )
    finally:
        if whatsapp:
            await whatsapp.aclose()

    if not results:
        print("Nobody has debts due today or overdue." + (" (Did you seed? scripts/seed_demo.py)" if not args.wa_id else ""))
    for r in results:
        print(f"\n{BOLD}Trader {r.wa_id}{RESET} — {r.debtors} customer(s) due/overdue — {STATUS.get(r.status, r.status)}")
        for reply in r.replies:
            print(f"\n{GREEN}OwoBook:{RESET}\n{render_plain(reply)}")


if __name__ == "__main__":
    asyncio.run(main())
