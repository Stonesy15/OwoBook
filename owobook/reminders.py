"""Daily debt reminders (08:00 Africa/Lagos).

For each trader with debts due today or overdue, OwoBook messages the TRADER (never the customer):
a short heads-up, then one polite ready-to-forward message per customer.

WhatsApp's 24-hour rule: free-form messages are only allowed within 24 hours of the trader's
last message to us. Outside that window we send the approved template named in
WA_REMINDER_TEMPLATE (if set). Its quick-reply button brings the drafts. With no template we log
and skip.

Each trader gets at most one batch per day (checked in messages_log), so running the job twice
is safe.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from owobook import ledger
from owobook.config import Settings
from owobook.dates import format_date, period_bounds, today_local, utcnow
from owobook.ledger import Debtor
from owobook.models import MessageLog, Trader
from owobook.money import format_naira
from owobook.whatsapp import formatting as fmt
from owobook.whatsapp.formatting import Lang, Reply

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
MAX_DRAFTS = 5  # forwardable drafts per morning; the rest are summarised
REMINDER_TYPE = "reminder"  # messages_log.type for sent reminders


# ---------- message text -------------------------------------------------------------------


def _items_for(db: Session, trader: Trader, debtor: Debtor) -> str | None:
    """'3 bags rice' (or two items joined) for the unpaid sales behind this debt."""
    items = [fmt.item_line(e) for e, _ in ledger.outstanding_sales(db, trader, debtor.customer)]
    items = [i for i in items if i]
    if not items:
        return None
    return " and ".join(items[:2]) + (" …" if len(items) > 2 else "")


def customer_draft(db: Session, trader: Trader, debtor: Debtor, lang: Lang, today: date) -> str:
    """A polite reminder the trader can forward to their customer as-is."""
    name, amount = debtor.customer.display_name, format_naira(debtor.balance_kobo)
    items = _items_for(db, trader, debtor)
    seller = trader.shop_name or trader.name
    when = format_date(debtor.next_due, today) if debtor.next_due else None
    if lang == "pidgin":
        who = f"Na {seller} dey remind you" if seller else "I just dey remind you"
        what = f"your balance of {amount}" + (f" for {items}" if items else "")
        status = "don due today" if debtor.due_today or not when else f"don pass since {when}"
        return (f"Good morning {name} 🙏\n\n{who} say {what} {status}.\n\n"
                "Abeg pay when you fit. Thank you for your patronage! 🙏")
    who = f"This is a friendly reminder from {seller}" if seller else "This is a friendly reminder"
    what = f"your balance of {amount}" + (f" for {items}" if items else "")
    status = "is due today" if debtor.due_today or not when else f"was due on {when}"
    return (f"Good morning {name} 🙏\n\n{who} that {what} {status}.\n\n"
            "Please pay when you can. Thank you for your patronage! 🙏")


def _heads_up(debtors: list[Debtor], lang: Lang, today: date) -> str:
    if len(debtors) == 1:
        d = debtors[0]
        name, amount = d.customer.display_name, format_naira(d.balance_kobo)
        if lang == "pidgin":
            status = "don due today" if d.due_today else f"don pass since {format_date(d.next_due, today)}"
            return f"⏰ {name} {amount} {status}. See message wey you fit forward give am 👇"
        status = "is due today" if d.due_today else f"has been overdue since {format_date(d.next_due, today)}"
        return f"⏰ {name}'s {amount} {status}. Here's a reminder you can forward 👇"

    total = format_naira(sum(d.balance_kobo for d in debtors))
    lines = [
        f"⏰ {len(debtors)} people get debt wey don due — {total} total. Messages wey you fit forward dey below 👇"
        if lang == "pidgin" else
        f"⏰ {len(debtors)} customers have debts due or overdue — {total} in total. Reminders you can forward are below 👇",
        "",
    ]
    for d in debtors:
        tag = "today" if d.due_today else f"⚠️ since {format_date(d.next_due, today)}"
        lines.append(f"• {d.customer.display_name} — {format_naira(d.balance_kobo)} · {tag}")
    return "\n".join(lines)


def reminder_replies(db: Session, trader: Trader, debtors: list[Debtor], lang: Lang, today: date) -> list[Reply]:
    """Heads-up for the trader, then one draft per customer (at most MAX_DRAFTS). The trader forwards it, or,
    if we have the customer's number, taps "Send to <name>" to open their own chat with it already typed."""
    if not debtors:
        return [Reply(fmt.t(lang, "no_overdue"))]
    shown = debtors[:MAX_DRAFTS]
    heads_up = _heads_up(debtors, lang, today)
    if not any(c.phone for c in ledger.list_customers(db, trader)):  # teach the one-tap send until they use it
        heads_up += "\n\n" + fmt.t(lang, "phone_tip", name=shown[0].customer.display_name)
    replies = [Reply(heads_up)]
    for d in shown:
        draft = customer_draft(db, trader, d, lang, today)
        phone = d.customer.phone
        replies.append(Reply(draft, link=fmt.send_to_link(d.customer.display_name, phone, draft) if phone else None))
    if len(debtors) > MAX_DRAFTS:
        more = len(debtors) - MAX_DRAFTS
        replies.append(Reply(
            f"…and {more} more. Talk \"who owe me\" to see everybody." if lang == "pidgin"
            else f"…and {more} more. Say \"who owes me\" to see everyone."
        ))
    return replies


# ---------- who to remind ------------------------------------------------------------------


def debtors_due(db: Session, trader: Trader, today: date) -> list[Debtor]:
    """Debts due today or overdue: due today first, then the longest overdue."""
    debtors = ledger.list_debtors(db, trader, overdue_only=True, today=today)
    return sorted(debtors, key=lambda d: (not d.due_today, d.next_due or today, -d.balance_kobo))


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def in_service_window(trader: Trader, now: datetime) -> bool:
    """True if the trader messaged us in the last 24 hours (free-form messages allowed)."""
    return bool(trader.last_inbound_at) and _as_utc(now) - _as_utc(trader.last_inbound_at) < WINDOW


def already_reminded(db: Session, trader: Trader, today: date) -> bool:
    start, _ = period_bounds("today", today)
    return db.scalar(
        select(MessageLog.id).where(
            MessageLog.trader_id == trader.id,
            MessageLog.type == REMINDER_TYPE,
            MessageLog.created_at >= start,
        ).limit(1)
    ) is not None


def _log(db: Session, trader: Trader, content: str) -> None:
    db.add(MessageLog(trader_id=trader.id, direction="out", type=REMINDER_TYPE, content=content))


# ---------- the job ------------------------------------------------------------------------


@dataclass
class ReminderResult:
    wa_id: str
    status: str  # sent | template | no_template | already_sent | dry_run | error
    debtors: int = 0
    replies: list[Reply] = field(default_factory=list)


async def run_reminders(
    session_factory: Callable[[], Session],
    whatsapp,
    settings: Settings,
    *,
    today: date | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    only_wa_id: str | None = None,
    force: bool = False,
) -> list[ReminderResult]:
    """Send today's reminders. `dry_run` builds the messages without sending or logging;
    `force` ignores the once-a-day check (for testing)."""
    from owobook.agent.handler import trader_language  # avoid an import cycle

    today = today or today_local()
    now = now or utcnow()
    results: list[ReminderResult] = []

    with session_factory() as db:
        query = select(Trader.id, Trader.wa_id).where(Trader.onboarded.is_(True))
        if only_wa_id:
            query = query.where(Trader.wa_id == only_wa_id)
        trader_rows = db.execute(query).all()

    for trader_id, wa_id in trader_rows:
        try:
            with session_factory() as db:
                trader = db.get(Trader, trader_id)
                debtors = debtors_due(db, trader, today)
                if not debtors:
                    continue
                if not force and not dry_run and already_reminded(db, trader, today):
                    results.append(ReminderResult(wa_id, "already_sent", len(debtors)))
                    continue
                lang = trader_language(db, trader)
                replies = reminder_replies(db, trader, debtors, lang, today)

                if dry_run:
                    results.append(ReminderResult(wa_id, "dry_run", len(debtors), replies))
                    continue
                if in_service_window(trader, now):
                    for reply in replies:
                        await whatsapp.send_reply(wa_id, reply)
                        _log(db, trader, reply.text)
                    results.append(ReminderResult(wa_id, "sent", len(debtors), replies))
                elif settings.wa_reminder_template:
                    total = format_naira(sum(d.balance_kobo for d in debtors))
                    params = [trader.name or "there", str(len(debtors)), total]
                    await whatsapp.send_template(
                        wa_id, settings.wa_reminder_template, settings.wa_reminder_template_lang, params
                    )
                    _log(db, trader, f"[template {settings.wa_reminder_template}] {params}")
                    results.append(ReminderResult(wa_id, "template", len(debtors)))
                else:
                    log.info("reminder for %s skipped: outside the 24h window and no WA_REMINDER_TEMPLATE", wa_id)
                    results.append(ReminderResult(wa_id, "no_template", len(debtors)))
                db.commit()
        except Exception:
            log.exception("reminders failed for trader %s", wa_id)
            results.append(ReminderResult(wa_id, "error"))

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("reminders for %s: %s", today, counts or "nothing due")
    return results
