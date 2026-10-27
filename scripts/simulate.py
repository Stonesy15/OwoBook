"""Chat with OwoBook in the terminal — no WhatsApp needed.

    uv run python scripts/simulate.py                 # fake AI (no keys needed)
    uv run python scripts/simulate.py --verbose       # also show the tool calls
    uv run python scripts/simulate.py --wa-id 2348011112222   # pretend to be another trader

Type a message and press Enter. When a reply has buttons, type 1, 2 or 3 to tap one.
Commands: /help, /quit
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owobook.agent.handler import handle_message  # noqa: E402
from owobook.config import get_settings  # noqa: E402
from owobook.db import migrate, session_scope  # noqa: E402
from owobook.llm import get_provider  # noqa: E402
from owobook.whatsapp.formatting import render_plain  # noqa: E402

GREEN, DIM, BOLD, RESET = "\033[32m", "\033[2m", "\033[1m", "\033[0m"

HELP = f"""{DIM}Try:
  Mama Bisi collect 3 bags rice, she go pay 45k Friday
  Mama Bisi don pay 20k
  Sold 5 crates of eggs 12k cash
  Chidi carry 2 cartons indomie at 6500 each, he go pay tomorrow
  Who owe me money?
  How much I sell this week?
  How much Mama Bisi owe?
Type 1/2/3 to tap a button. /quit to exit.{RESET}"""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wa-id", default="2348000000001", help="pretend WhatsApp number of the trader")
    parser.add_argument("--provider", default="fake", choices=["fake", "meta", "baseten"])
    parser.add_argument("--verbose", "-v", action="store_true", help="show tool calls")
    args = parser.parse_args()

    migrate()  # runs first: Alembic's logging config would otherwise override ours
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format=f"{DIM}  · %(message)s{RESET}", force=True
    )
    for noisy in ("alembic", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    provider = get_provider(args.provider)
    print(f"{BOLD}OwoBook simulator{RESET} — trader {args.wa_id}, AI: {provider.name} ({provider.model}), "
          f"DB: {get_settings().database_url}")
    print(HELP)

    buttons: list = []
    message_no = 0
    while True:
        try:
            line = input(f"\n{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/help":
            print(HELP)
            continue

        message_no += 1
        kwargs = {"text": line, "msg_type": "text"}
        if line in ("1", "2", "3") and buttons and int(line) <= len(buttons):
            button = buttons[int(line) - 1]
            print(f"{DIM}  (tapped {button.title}){RESET}")
            kwargs = {"button_id": button.id, "msg_type": "button"}

        with session_scope() as db:
            replies = await handle_message(db, provider, args.wa_id, wa_message_id=None, **kwargs)

        buttons = []
        for reply in replies:
            print(f"\n{GREEN}OwoBook:{RESET}\n{render_plain(reply)}")
            if reply.buttons:
                buttons = reply.buttons


if __name__ == "__main__":
    asyncio.run(main())
