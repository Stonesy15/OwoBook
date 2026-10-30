"""Chat with OwoBook in the terminal — no WhatsApp needed.

    uv run python scripts/simulate.py                       # uses LLM_PROVIDER / TRANSCRIBER from .env
    uv run python scripts/simulate.py --provider fake       # rule-based fake AI, no keys needed
    uv run python scripts/simulate.py --provider baseten    # Meta open-weights model on Baseten
    uv run python scripts/simulate.py -v                    # also show tool calls
    uv run python scripts/simulate.py --wa-id 2348011112222 # pretend to be another trader

Type a message and press Enter. To send a voice note, type (or drag in) the path to an
audio file (.ogg .opus .m4a .mp3 .wav); for a receipt photo, the path to a .jpg/.png, optionally
followed by a caption. When a reply has buttons, type 1, 2 or 3 to tap one.
After each turn the simulator prints the model/transcription cost of that message.
Commands: /reminders (show this morning's debt reminders), /help, /quit
"""

import argparse
import asyncio
import logging
import shlex
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from owobook.agent.handler import handle_message, handle_photo, handle_voice_note  # noqa: E402
from owobook.config import get_settings  # noqa: E402
from owobook.db import SessionLocal, migrate, session_scope  # noqa: E402
from owobook.llm import LLMError, get_provider  # noqa: E402
from owobook.models import UsageLog  # noqa: E402
from owobook.reminders import run_reminders  # noqa: E402
from owobook.transcribe import TranscriptionError, get_transcriber  # noqa: E402
from owobook.whatsapp.formatting import render_plain  # noqa: E402

GREEN, DIM, BOLD, RED, RESET = "\033[32m", "\033[2m", "\033[1m", "\033[31m", "\033[0m"
AUDIO_EXTS = {".ogg", ".opus", ".oga", ".m4a", ".mp3", ".wav", ".aac", ".amr", ".webm"}
IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

HELP = f"""{DIM}Try:
  Mama Bisi collect 3 bags rice, she go pay 45k Friday
  Mama Bisi don pay 20k
  Sold 5 crates of eggs 12k cash
  Chidi carry 2 cartons indomie at 6500 each, he go pay tomorrow
  Who owe me money?
  How much I sell this week?
  How much Mama Bisi owe?
  ~/Downloads/voice-note.ogg      ← a path to an audio file sends it as a voice note
  ~/Downloads/receipt.jpg Tunde   ← a path to a photo sends it (any words after it are the caption)
Type 1/2/3 to tap a button. /reminders shows the 08:00 debt reminders. /quit to exit.{RESET}"""


def audio_path(line: str) -> Path | None:
    """Accept a typed or drag-and-dropped audio path (possibly quoted or with escaped spaces)."""
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) != 1:
        return None
    path = Path(parts[0]).expanduser()
    return path if path.suffix.lower() in AUDIO_EXTS and path.is_file() else None


def photo_path(line: str) -> tuple[Path, str | None] | None:
    """A photo path, optionally followed by a caption: `receipt.jpg Mama Bisi never pay`."""
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if not parts:
        return None
    path = Path(parts[0]).expanduser()
    if path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
        return None
    return path, " ".join(parts[1:]) or None


async def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wa-id", default="2348000000001", help="pretend WhatsApp number of the trader")
    parser.add_argument("--provider", default=settings.llm_provider, choices=["fake", "meta", "baseten"])
    parser.add_argument("--transcriber", default=None, choices=["fake", "meta"],
                        help="default: fake with --provider fake, otherwise TRANSCRIBER from .env")
    parser.add_argument("--verbose", "-v", action="store_true", help="show tool calls")
    args = parser.parse_args()
    transcriber_name = args.transcriber or ("fake" if args.provider == "fake" else settings.transcriber)

    migrate()  # runs first: Alembic's logging config would otherwise override ours
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format=f"{DIM}  · %(message)s{RESET}", force=True
    )
    for noisy in ("alembic", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        provider = get_provider(args.provider)
    except LLMError as exc:
        sys.exit(f"{RED}{exc}{RESET}\nTip: run with --provider fake to try without keys.")
    transcriber = None

    print(f"{BOLD}OwoBook simulator{RESET} — trader {args.wa_id}, AI: {provider.name} ({provider.model}), "
          f"voice: {transcriber_name}, DB: {settings.database_url}")
    print(HELP)

    buttons: list = []
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
        if line == "/reminders":
            results = await run_reminders(SessionLocal, None, settings, dry_run=True, only_wa_id=args.wa_id)
            if not results:
                print(f"{DIM}  (no debts due today or overdue — nothing would be sent at 08:00){RESET}")
            for reply in (results[0].replies if results else []):
                print(f"\n{GREEN}OwoBook (08:00 reminder):{RESET}\n{render_plain(reply)}")
            continue

        with session_scope() as db:
            usage_before = db.scalar(select(func.coalesce(func.max(UsageLog.id), 0)))

        try:
            with session_scope() as db:
                if line in ("1", "2", "3") and buttons and int(line) <= len(buttons):
                    button = buttons[int(line) - 1]
                    print(f"{DIM}  (tapped {button.title}){RESET}")
                    replies = await handle_message(db, provider, args.wa_id, button_id=button.id, msg_type="button")
                elif path := audio_path(line):
                    if transcriber is None:
                        transcriber = get_transcriber(transcriber_name)
                    print(f"{DIM}  (sending voice note {path.name}, {path.stat().st_size // 1024} KB){RESET}")
                    replies = await handle_voice_note(db, provider, transcriber, args.wa_id, path.read_bytes())
                elif photo := photo_path(line):
                    path, caption = photo
                    print(f"{DIM}  (sending photo {path.name}, {path.stat().st_size // 1024} KB){RESET}")
                    replies = await handle_photo(db, provider, args.wa_id, path.read_bytes(),
                                                 IMAGE_EXTS[path.suffix.lower()], caption=caption)
                else:
                    replies = await handle_message(db, provider, args.wa_id, text=line, msg_type="text")
        except (LLMError, TranscriptionError) as exc:
            print(f"{RED}  {exc}{RESET}")
            continue

        buttons = []
        for reply in replies:
            print(f"\n{GREEN}OwoBook:{RESET}\n{render_plain(reply)}")
            if reply.image:  # a picture receipt: save it so you can open it
                out = ROOT / "receipts" / f"receipt-{datetime.now():%H%M%S}.png"
                out.parent.mkdir(exist_ok=True)
                out.write_bytes(reply.image)
                print(f"{DIM}  (picture saved: {out}){RESET}")
            if reply.buttons:
                buttons = reply.buttons

        with session_scope() as db:
            rows = db.scalars(select(UsageLog).where(UsageLog.id > usage_before)).all()
        if rows and any(not r.provider.startswith("fake") for r in rows):
            cost = sum(r.est_cost_usd for r in rows)
            toks = sum(r.input_tokens + r.output_tokens for r in rows)
            audio = sum(r.audio_seconds for r in rows)
            calls = sum(1 for r in rows if r.input_tokens or r.output_tokens)
            extra = f", {audio:.1f}s audio" if audio else ""
            print(f"{DIM}  cost: ${cost:.5f} ({calls} model call(s), {toks} tokens{extra}){RESET}")


if __name__ == "__main__":
    asyncio.run(main())
