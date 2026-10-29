"""Entry point for every inbound message — used by the WhatsApp webhook and the simulator.

Handles: dedupe, new-trader onboarding, button taps (Correct / Fix it / Undo),
editing mode, the agent loop, message logging, and friendly errors.
"""

import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from owobook import ledger
from owobook.agent.loop import recent_history, run_agent
from owobook.agent.tools import AgentContext
from owobook.dates import today_local, utcnow
from owobook.llm.base import LLMProvider
from owobook.models import MessageLog, Trader
from owobook.transcribe import Transcriber
from owobook.usage import VISION_TAG, log_usage
from owobook.vision import PHOTO_MARKER, read_photo
from owobook.whatsapp import formatting as fmt
from owobook.whatsapp.formatting import Lang, Reply

log = logging.getLogger(__name__)


def _log_message(db: Session, trader: Trader | None, direction: str, type_: str, content: str | None,
                 wa_message_id: str | None = None) -> MessageLog:
    row = MessageLog(
        trader_id=trader.id if trader else None,
        wa_message_id=wa_message_id,
        direction=direction,
        type=type_,
        content=content,
    )
    db.add(row)
    db.flush()
    return row


def already_seen(db: Session, wa_message_id: str | None) -> bool:
    return bool(wa_message_id) and db.scalar(
        select(MessageLog.id).where(MessageLog.wa_message_id == wa_message_id)
    ) is not None


def trader_language(db: Session, trader: Trader) -> Lang:
    """Language for replies that have no text to mirror (button taps): use the last thing they wrote."""
    last_text = db.scalar(
        select(MessageLog.content)
        .where(MessageLog.trader_id == trader.id, MessageLog.direction == "in", MessageLog.type.in_(["text", "audio"]))
        .order_by(MessageLog.id.desc())
        .limit(1)
    )
    return fmt.detect_language(last_text or "", trader.language_pref)


def handle_button(db: Session, trader: Trader, button_id: str, lang: Lang) -> list[Reply]:
    if "remind" in button_id.lower():  # quick reply on the reminder template ("Show reminders")
        from owobook import reminders

        today = today_local()
        return reminders.reminder_replies(db, trader, reminders.debtors_due(db, trader, today), lang, today)
    parsed = fmt.parse_button_id(button_id)
    if not parsed:
        return [Reply(fmt.t(lang, "fallback"))]
    action, entry_id = parsed
    try:
        if action == fmt.BUTTON_CONFIRM:
            ledger.confirm_entry(db, trader, entry_id)
            ledger.stop_editing(db, trader)
            return [Reply(fmt.t(lang, "confirmed"))]
        if action == fmt.BUTTON_UNDO:
            entry = ledger.get_entry(db, trader, entry_id)
            if entry.status == "deleted":
                return [Reply(fmt.t(lang, "already_undone"))]
            ledger.delete_entry(db, trader, entry_id)
            ledger.stop_editing(db, trader)
            text = fmt.t(lang, "undone")
            if entry.customer:
                balance = ledger.customer_balance(db, trader, entry.customer)
                text += "\n" + fmt.balance_sentence(entry.customer.display_name, balance, lang)
            return [Reply(text)]
        # Fix it
        entry = ledger.get_entry(db, trader, entry_id)
        if entry.status == "deleted":
            return [Reply(fmt.t(lang, "already_undone"))]
        ledger.start_editing(db, trader, entry_id)
        return [Reply(fmt.t(lang, "fix_prompt"))]
    except ledger.LedgerError as exc:
        return [Reply(exc.question)]


async def handle_message(
    db: Session,
    provider: LLMProvider,
    wa_id: str,
    *,
    text: str | None = None,
    button_id: str | None = None,
    msg_type: str = "text",
    wa_message_id: str | None = None,
    now: datetime | None = None,
    lang: Lang | None = None,
) -> list[Reply]:
    """Process one inbound message and return the replies to send.

    `text` is the typed text, the voice-note transcript or what was read from a photo. `msg_type` is
    'text', 'audio', 'image' or 'button'. `lang` overrides language detection (photos are read in
    English). Commits nothing — the caller owns the transaction.
    """
    if already_seen(db, wa_message_id):
        log.info("duplicate message %s ignored", wa_message_id)
        return []

    trader, _ = ledger.get_or_create_trader(db, wa_id)
    trader.last_inbound_at = now or utcnow()
    content = text if not button_id else f"[button] {button_id}"
    try:
        with db.begin_nested():
            inbound = _log_message(db, trader, "in", msg_type, content, wa_message_id)
    except IntegrityError:  # same message id arrived twice at the same moment
        log.info("duplicate message %s ignored (race)", wa_message_id)
        return []

    lang = lang or (fmt.detect_language(text, trader.language_pref) if text else trader_language(db, trader))
    try:
        if button_id:
            replies = handle_button(db, trader, button_id, lang)
        elif not trader.onboarded:
            trader.onboarded = True
            replies = [fmt.onboarding_reply(lang)]
            # Process the very first message too if it's a voice note or photo (a real attempt — amounts
            # are often spelled out, "sixty thousand") or text that looks like a record ("... 45k").
            looks_like_record = bool(text and re.search(r"\d", text) and len(text.split()) >= 3)
            if text and text.strip() and (msg_type in ("audio", "image") or looks_like_record):
                replies += await _run(db, provider, trader, text, msg_type, lang, inbound.id)
        elif not text or not text.strip():
            replies = [Reply(fmt.t(lang, "fallback"))]
        else:
            replies = await _run(db, provider, trader, text, msg_type, lang, inbound.id)
    except Exception:
        log.exception("failed to handle message from trader %s", trader.id)
        replies = [Reply(fmt.t(lang, "error"))]

    for r in replies:
        _log_message(db, trader, "out", "text", fmt.render_plain(r))
    return replies


async def handle_voice_note(
    db: Session,
    provider: LLMProvider,
    transcriber: Transcriber,
    wa_id: str,
    audio: bytes,
    *,
    wa_message_id: str | None = None,
    now: datetime | None = None,
) -> list[Reply]:
    """Transcribe a voice note with Muse Voice Transcribe, then handle it like a text message.
    The first reply starts with what we heard, so the trader can spot mis-hearings."""
    if already_seen(db, wa_message_id):
        return []
    trader, _ = ledger.get_or_create_trader(db, wa_id)
    lang = trader_language(db, trader)
    keywords = [c.display_name for c in ledger.list_customers(db, trader)][:60]
    keywords += [x for x in (trader.name, trader.shop_name) if x]
    try:
        transcript = await transcriber.transcribe(audio, keywords=keywords)
    except Exception:
        log.exception("transcription failed for trader %s", trader.id)
        return [Reply(fmt.t(lang, "error"))]
    log_usage(
        db, trader_id=trader.id, provider=transcript.provider, model=transcript.model,
        audio_seconds=transcript.audio_seconds,
    )
    if not transcript.text:
        return [Reply(fmt.t(lang, "didnt_hear"))]

    replies = await handle_message(
        db, provider, wa_id, text=transcript.text, msg_type="audio", wa_message_id=wa_message_id, now=now
    )
    if replies:
        replies[0] = Reply(f"🎤 \"{transcript.text}\"\n\n{replies[0].text}", replies[0].buttons, replies[0].link)
    return replies


async def handle_photo(
    db: Session,
    provider: LLMProvider,
    wa_id: str,
    image: bytes,
    mime_type: str,
    *,
    caption: str | None = None,
    wa_message_id: str | None = None,
    now: datetime | None = None,
) -> list[Reply]:
    """Read a receipt / debt-book photo with Muse Spark vision, then handle the lines like a message.
    The first reply starts with what we read, so the trader can spot mistakes."""
    if already_seen(db, wa_message_id):
        return []
    trader, _ = ledger.get_or_create_trader(db, wa_id)
    lang = fmt.detect_language(caption, trader.language_pref) if caption and caption.strip() \
        else trader_language(db, trader)
    try:
        reading = await read_photo(provider, image, mime_type, caption=caption,
                                   trader_name=trader.name, shop_name=trader.shop_name)
    except Exception:
        log.exception("photo reading failed for trader %s", trader.id)
        return [Reply(fmt.t(lang, "error"))]
    log_usage(  # the ":vision" tag lets usage.turns() count this call as a photo
        db, trader_id=trader.id, provider=f"{reading.provider}{VISION_TAG}", model=reading.model,
        input_tokens=reading.usage.input_tokens, output_tokens=reading.usage.output_tokens,
    )
    if not reading.lines:
        if caption and caption.strip():  # a photo of goods + "Tunde collect 2 of these, 10k"
            return await handle_message(db, provider, wa_id, text=caption, msg_type="image",
                                        wa_message_id=wa_message_id, now=now)
        trader.last_inbound_at = now or utcnow()
        _log_message(db, trader, "in", "image", "[photo: nothing to record]", wa_message_id)
        reply = Reply(fmt.t(lang, "no_receipt"))
        _log_message(db, trader, "out", "text", reply.text)
        return [reply]

    replies = await handle_message(db, provider, wa_id, text=f"{PHOTO_MARKER}\n{reading.text}", msg_type="image",
                                   wa_message_id=wa_message_id, now=now, lang=lang)
    if replies:
        read = "\n".join(f"• {line}" for line in reading.lines)
        replies[0] = Reply(f"🧾 {fmt.t(lang, 'photo_read')}\n{read}\n\n{replies[0].text}", replies[0].buttons,
                           replies[0].link)
    return replies


async def handle_unsupported(
    db: Session, wa_id: str, *, msg_type: str, wa_message_id: str | None = None, now: datetime | None = None
) -> list[Reply]:
    """Stickers, locations, documents, videos...: a short friendly pointer to what works."""
    if already_seen(db, wa_message_id):
        return []
    trader, _ = ledger.get_or_create_trader(db, wa_id)
    trader.last_inbound_at = now or utcnow()
    lang = trader_language(db, trader)
    try:
        with db.begin_nested():
            _log_message(db, trader, "in", msg_type, f"[{msg_type}]", wa_message_id)
    except IntegrityError:
        return []
    replies = []
    if not trader.onboarded:
        trader.onboarded = True
        replies.append(fmt.onboarding_reply(lang))
    replies.append(Reply(fmt.t(lang, "unsupported")))
    for r in replies:
        _log_message(db, trader, "out", "text", fmt.render_plain(r))
    return replies


async def _run(db: Session, provider: LLMProvider, trader: Trader, text: str, msg_type: str, lang: Lang,
               inbound_id: int) -> list[Reply]:
    ctx = AgentContext(
        db=db,
        trader=trader,
        lang=lang,
        today=today_local(),
        source="voice" if msg_type == "audio" else "image" if msg_type == "image" else "text",
        raw_text=text,
    )
    sess = ledger.get_session(db, trader)
    was_editing = sess.mode == "editing"
    try:
        with db.begin_nested():  # a crash mid-agent rolls back half-written entries
            return await run_agent(ctx, provider, text, recent_history(db, trader, before_id=inbound_id))
    finally:
        if was_editing:
            ledger.stop_editing(db, trader)  # a correction is one message; then back to normal
