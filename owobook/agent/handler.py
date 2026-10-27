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


def _trader_language(db: Session, trader: Trader) -> Lang:
    """Language for replies that have no text to mirror (button taps): use the last thing they wrote."""
    last_text = db.scalar(
        select(MessageLog.content)
        .where(MessageLog.trader_id == trader.id, MessageLog.direction == "in", MessageLog.type.in_(["text", "audio"]))
        .order_by(MessageLog.id.desc())
        .limit(1)
    )
    return fmt.detect_language(last_text or "", trader.language_pref)


def handle_button(db: Session, trader: Trader, button_id: str, lang: Lang) -> list[Reply]:
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
) -> list[Reply]:
    """Process one inbound message and return the replies to send.

    `text` is the typed text or the voice-note transcript. `msg_type` is 'text', 'audio',
    'image' or 'button'. Commits nothing — the caller owns the transaction.
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

    lang = fmt.detect_language(text, trader.language_pref) if text else _trader_language(db, trader)
    try:
        if button_id:
            replies = handle_button(db, trader, button_id, lang)
        elif not trader.onboarded:
            trader.onboarded = True
            replies = [fmt.onboarding_reply(lang)]
            # If their very first message already looks like a record ("... 45k"), process it too.
            if text and re.search(r"\d", text):
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
