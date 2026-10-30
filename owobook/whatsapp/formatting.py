"""Turn ledger results into WhatsApp messages.

Every number shown to a trader comes from the database and is formatted here,
never written by the model. Replies are in the trader's chosen language (default English);
the texts themselves live in i18n.py.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote

from owobook.dates import format_date, today_local
from owobook.ledger import Debtor, PaymentRecord, SalesSummary
from owobook.models import Customer, Entry, EntryType
from owobook.money import format_money
from owobook.whatsapp.i18n import LANGUAGES, STRINGS

Lang = str  # one of i18n.LANGUAGES


@dataclass
class Button:
    id: str
    title: str  # WhatsApp limit: 20 characters


@dataclass
class Link:
    """A link button (WhatsApp `cta_url`): opens `url` when tapped."""

    title: str  # WhatsApp limit: 20 characters
    url: str


@dataclass
class Reply:
    """One outgoing WhatsApp message: plain text, text with up to 3 reply buttons, text with one link button,
    a PNG image (a picture receipt) or a PDF document (the business record), with `text` as the caption."""

    text: str
    buttons: list[Button] = field(default_factory=list)
    link: Link | None = None
    image: bytes | None = None
    document: bytes | None = None
    filename: str | None = None


# ---------- language ----------------------------------------------------------------------

def reply_language(preference: str | None) -> Lang:
    """The language OwoBook replies in: the trader's chosen one, else English — whatever language
    the message itself was in (a Pidgin voice note still gets an English reply by default)."""
    return preference if preference in LANGUAGES else "english"


def t(lang: Lang, key: str, **kw) -> str:
    table = STRINGS.get(lang, STRINGS["english"])
    return table[key].format(**kw)


# ---------- receipts ----------------------------------------------------------------------

BUTTON_CONFIRM, BUTTON_FIX, BUTTON_UNDO = "confirm", "fix", "undo"


def entry_buttons(entry_id: int, lang: Lang = "english") -> list[Button]:
    return [
        Button(f"{BUTTON_CONFIRM}:{entry_id}", t(lang, "btn_ok")),
        Button(f"{BUTTON_FIX}:{entry_id}", t(lang, "btn_fix")),
        Button(f"{BUTTON_UNDO}:{entry_id}", t(lang, "btn_undo")),
    ]


def parse_button_id(button_id: str) -> tuple[str, int] | None:
    """'confirm:12' → ('confirm', 12)."""
    action, _, raw_id = button_id.partition(":")
    if action in (BUTTON_CONFIRM, BUTTON_FIX, BUTTON_UNDO) and raw_id.isdigit():
        return action, int(raw_id)
    return None


def _qty(q: float | None) -> str:
    if q is None:
        return ""
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def item_line(entry: Entry) -> str | None:
    """'3 bags rice' from quantity/unit/description. A unit that just repeats the item
    ("3 charger charger", "2 crates crate") is dropped, and so is a quantity the description
    already has ("indomie 3 ctn", read off a photo)."""
    unit, item = entry.unit or "", entry.item_description or ""
    qty = _qty(entry.quantity)
    if qty and re.search(rf"(?<![\d.]){re.escape(qty)}(?![\d.])", item):
        return item
    if unit and item and unit.lower().rstrip("s") == item.lower().rstrip("s"):
        unit = ""
    parts = [_qty(entry.quantity), unit, item]
    text = " ".join(p for p in parts if p).strip()
    return text or None


def balance_sentence(name: str, balance_kobo: int, lang: Lang, *, total: bool = False) -> str:
    if balance_kobo > 0:
        return t(lang, "now_owes" if total else "still_owes", name=name, amount=format_money(balance_kobo))
    if balance_kobo == 0:
        return t(lang, "cleared", name=name)
    return t(lang, "credit", name=name, amount=format_money(-balance_kobo))


def trust_notes(name: str, record: PaymentRecord, lang: Lang, today: date | None = None) -> list[str]:
    """Heads-up lines for a new credit sale, from how this customer has paid before."""
    notes = []
    if record.overdue_kobo:
        notes.append(t(lang, "trust_overdue", name=name, amount=format_money(record.overdue_kobo),
                       date=format_date(record.oldest_overdue, today)))
    if record.late >= 2 or record.late > record.on_time:
        notes.append(t(lang, "trust_late", name=name, late=record.late, total=record.total))
    elif record.on_time >= 3 and not record.late:
        notes.append(t(lang, "trust_good", name=name, on_time=record.on_time, total=record.total))
    return notes


def trust_line(record: PaymentRecord, lang: Lang) -> str:
    if not record.total:
        return t(lang, "trust_new")
    return t(lang, "trust_record", on_time=record.on_time, total=record.total)


def sale_receipt(
    entry: Entry, customer: Customer | None, balance_kobo: int | None, lang: Lang, today: date | None = None,
    notes: list[str] | None = None,
) -> Reply:
    lines = [t(lang, "recorded"), ""]
    if customer:
        lines.append(f"👤 {customer.display_name}")
    if item := item_line(entry):
        lines.append(f"🛒 {item}")
    if entry.type == EntryType.CREDIT_SALE and entry.paid_now_kobo:
        # Part-paid: show the full price, what was paid on the spot, and what's left — all from the DB.
        lines += [
            f"💰 {format_money(entry.amount_kobo)} {t(lang, 'sale_total')}",
            f"💵 {format_money(entry.paid_now_kobo)} {t(lang, 'paid_now')}",
            f"⏳ {format_money(entry.amount_kobo - entry.paid_now_kobo)} — {t(lang, 'owing')}",
        ]
    elif entry.type == EntryType.CREDIT_SALE:
        lines.append(f"💰 {format_money(entry.amount_kobo)} — {t(lang, 'owing')}")
    if entry.type == EntryType.CREDIT_SALE:
        if entry.due_date:
            lines.append(f"📅 {t(lang, 'due')}: {format_date(entry.due_date, today)}")
        else:
            lines.append(f"📅 {t(lang, 'no_due')}")
    else:
        lines.append(f"💰 {format_money(entry.amount_kobo)} — {t(lang, 'paid_cash')}")
    if customer and balance_kobo is not None and entry.type == EntryType.CREDIT_SALE:
        lines += ["", balance_sentence(customer.display_name, balance_kobo, lang, total=True)]
    if notes:
        lines += ["", *notes]
    if customer and entry.type == EntryType.CREDIT_SALE and entry.paid_now_kobo:
        lines += ["", t(lang, "receipt_hint", name=customer.display_name)]
    return Reply("\n".join(lines), entry_buttons(entry.id, lang))


def payment_receipt(entry: Entry, customer: Customer, balance_kobo: int, lang: Lang) -> Reply:
    lines = [
        t(lang, "payment_recorded"),
        "",
        f"👤 {customer.display_name}",
        f"💰 {format_money(entry.amount_kobo)} {t(lang, 'received')}",
        "",
        balance_sentence(customer.display_name, balance_kobo, lang),
        "",
        t(lang, "receipt_hint", name=customer.display_name),
    ]
    return Reply("\n".join(lines), entry_buttons(entry.id, lang))


def expense_receipt(entry: Entry, lang: Lang) -> Reply:
    lines = [t(lang, "expense_recorded"), "", f"🧾 {entry.item_description}", f"💸 {format_money(entry.amount_kobo)}"]
    return Reply("\n".join(lines), entry_buttons(entry.id, lang))


def receipt_for(entry: Entry, balance_kobo: int | None, lang: Lang, today: date | None = None) -> Reply:
    """Pick the right receipt for any entry type (used after edits)."""
    if entry.type == EntryType.PAYMENT:
        return payment_receipt(entry, entry.customer, balance_kobo or 0, lang)
    if entry.type == EntryType.EXPENSE:
        return expense_receipt(entry, lang)
    return sale_receipt(entry, entry.customer, balance_kobo, lang, today)


# ---------- answers to questions ----------------------------------------------------------


def _due_text(d: Debtor, lang: Lang, today: date) -> str:
    if d.overdue:
        return t(lang, "overdue", date=format_date(d.next_due, today))
    if d.due_today:
        return t(lang, "due_today")
    if d.next_due:
        return t(lang, "due_on", date=format_date(d.next_due, today))
    return ""


def balance_reply(debtor: Debtor, lang: Lang, today: date | None = None, record: PaymentRecord | None = None) -> Reply:
    """What a customer owes, when it's due, and (if given) how they've paid before."""
    today = today or today_local()
    name = debtor.customer.display_name
    if debtor.balance_kobo <= 0:
        lines = [t(lang, "balance_zero", name=name)]
        if debtor.balance_kobo < 0:
            lines = [balance_sentence(name, debtor.balance_kobo, lang)]
    else:
        lines = [t(lang, "balance", name=name, amount=format_money(debtor.balance_kobo))]
        if due := _due_text(debtor, lang, today):
            lines.append(due)
    if record is not None:
        lines.append(trust_line(record, lang))
    return Reply("\n".join(lines))


def debtors_reply(debtors: list[Debtor], lang: Lang, *, overdue_only: bool = False, today: date | None = None) -> Reply:
    today = today or today_local()
    if not debtors:
        return Reply(t(lang, "no_overdue" if overdue_only else "no_debtors"))
    title = t(lang, "overdue_title" if overdue_only else "debtors_title", count=len(debtors))
    lines = [title, ""]
    for i, d in enumerate(debtors, 1):
        line = f"{i}. {d.customer.display_name} — {format_money(d.balance_kobo)}"
        if d.overdue:
            line += f" · ⚠️ {format_date(d.next_due, today)}"
        elif d.due_today:
            line += f" · {t(lang, 'list_today')}"
        elif d.next_due:
            line += f" · {t(lang, 'list_due', date=format_date(d.next_due, today))}"
        lines.append(line)
    total = sum(d.balance_kobo for d in debtors)
    lines += ["", f"{t(lang, 'total')}: {format_money(total)}"]
    return Reply("\n".join(lines))


def summary_reply(s: SalesSummary, lang: Lang) -> Reply:
    rows = [
        f"📊 {t(lang, 'p_' + s.period)}",
        "",
        t(lang, "s_cash", amount=format_money(s.cash_sales_kobo)),
        t(lang, "s_credit", amount=format_money(s.credit_sales_kobo)),
        t(lang, "s_total", amount=format_money(s.total_sales_kobo)),
        t(lang, "s_collected", amount=format_money(s.collected_kobo)),
    ]
    if s.expenses_kobo:
        rows.append(t(lang, "s_expenses", amount=format_money(s.expenses_kobo)))
    return Reply("\n".join(rows))


def onboarding_reply(lang: Lang) -> Reply:
    return Reply(t(lang, "onboarding"))


def format_phone(number: str) -> str:
    """'2348031234567' → '+234 803 123 4567' (other countries: '+' and the digits)."""
    if number.startswith("234") and len(number) == 13:
        return f"+234 {number[3:6]} {number[6:9]} {number[9:]}"
    return "+" + number


def send_to_link(name: str, phone: str, message: str) -> Link:
    """Link button that opens the trader's own chat with this customer, message already typed."""
    title = f"Send to {name}"
    return Link(title if len(title) <= 20 else "Send to customer", f"https://wa.me/{phone}?text={quote(message)}")


def render_plain(reply: Reply) -> str:
    """Text-only rendering (simulator, logs)."""
    if reply.image:
        return f"[🖼 receipt picture]\n{reply.text}"
    if reply.document:
        return f"[📄 {reply.filename or 'document'}]\n{reply.text}"
    if reply.link:
        return f"{reply.text}\n\n[🔗 {reply.link.title}] {reply.link.url}"
    if not reply.buttons:
        return reply.text
    buttons = "   ".join(f"[{i}] {b.title}" for i, b in enumerate(reply.buttons, 1))
    return f"{reply.text}\n\n{buttons}"
