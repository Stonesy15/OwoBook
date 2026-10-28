"""Turn ledger results into WhatsApp messages.

Every number shown to a trader comes from the database and is formatted here,
never written by the model. Replies mirror the trader's language (English or Pidgin).
"""

import re
from dataclasses import dataclass, field
from datetime import date

from owobook.dates import format_date, today_local
from owobook.ledger import Debtor, SalesSummary
from owobook.models import Customer, Entry, EntryType
from owobook.money import format_naira

Lang = str  # "english" | "pidgin"


@dataclass
class Button:
    id: str
    title: str  # WhatsApp limit: 20 characters


@dataclass
class Reply:
    """One outgoing WhatsApp message: plain text, or text with up to 3 reply buttons."""

    text: str
    buttons: list[Button] = field(default_factory=list)


# ---------- language ----------------------------------------------------------------------

_PIDGIN_MARKERS = re.compile(
    r"\b(don|dey|wetin|abeg|abi|una|oga|sef|wahala|dem|go pay|collect|carry|na|no be|"
    r"how far|wey|sabi|fit|e don|make i|o!|jare|shey|biko|walahi|owe me)\b"
)


def detect_language(text: str, preference: str = "auto") -> Lang:
    """Use the trader's saved preference, otherwise guess from the message."""
    if preference in ("english", "pidgin"):
        return preference
    return "pidgin" if _PIDGIN_MARKERS.search(text.lower()) else "english"


_T = {
    "english": {
        "recorded": "✅ Recorded",
        "payment_recorded": "✅ Payment recorded",
        "expense_recorded": "✅ Expense recorded",
        "owing": "owing",
        "paid_cash": "paid (cash)",
        "sale_total": "total",
        "paid_now": "paid now",
        "received": "received",
        "due": "Due",
        "no_due": "No due date",
        "now_owes": "{name} now owes you {amount} in total.",
        "still_owes": "{name} now owes you {amount}.",
        "cleared": "{name} has cleared everything 🎉",
        "credit": "{name} has {amount} extra with you (overpaid).",
        "balance": "👤 {name} owes you {amount}.",
        "balance_zero": "👤 {name} doesn't owe you anything 👍",
        "due_on": "📅 Due: {date}",
        "overdue": "⚠️ Overdue since {date}",
        "due_today": "📅 Due today",
        "debtors_title": "📒 Who owes you ({count})",
        "overdue_title": "⚠️ Overdue / due today ({count})",
        "no_debtors": "Nobody owes you money right now 🎉",
        "no_overdue": "No overdue debts 👍",
        "total": "Total",
        "confirmed": "👍 Saved.",
        "undone": "↩️ Undone. That record has been removed.",
        "fix_prompt": "✏️ Okay — send the correction (voice or text), e.g. \"no, 4 bags, 60k\".",
        "already_undone": "That record was already removed.",
        "fallback": "Sorry, I didn't get that 🙏 Try something like \"Mama Bisi took 3 bags of rice, she'll pay 45k Friday\".",
        "error": "Sorry, something went wrong on my side 🙏 Please try again.",
        "didnt_hear": "Sorry, I couldn't hear that voice note well 🙏 Please send it again.",
        "unsupported": "For now I understand voice notes and text 🙏 Just tell me what you sold or who paid.",
        "profile_saved": "Nice to meet you, {name}! 🙌 You can start recording sales now.",
    },
    "pidgin": {
        "recorded": "✅ I don write am",
        "payment_recorded": "✅ Payment don enter",
        "expense_recorded": "✅ Expense don enter",
        "owing": "owing",
        "paid_cash": "paid (cash)",
        "sale_total": "total",
        "paid_now": "don pay for hand",
        "received": "collected",
        "due": "Due",
        "no_due": "No date to pay",
        "now_owes": "{name} dey owe you {amount} total.",
        "still_owes": "{name} still dey owe you {amount}.",
        "cleared": "{name} don clear everything 🎉",
        "credit": "{name} get {amount} extra for your hand (overpay).",
        "balance": "👤 {name} dey owe you {amount}.",
        "balance_zero": "👤 {name} no dey owe you anything 👍",
        "due_on": "📅 Due: {date}",
        "overdue": "⚠️ E don pass since {date}",
        "due_today": "📅 Na today e due",
        "debtors_title": "📒 People wey dey owe you ({count})",
        "overdue_title": "⚠️ Debt wey don due ({count})",
        "no_debtors": "Nobody dey owe you money now 🎉",
        "no_overdue": "No debt don pass due date 👍",
        "total": "Total",
        "confirmed": "👍 E don set.",
        "undone": "↩️ I don cancel am.",
        "fix_prompt": "✏️ Oya, send the correction (voice or text), like \"no, na 4 bags, 60k\".",
        "already_undone": "That record don already cancel.",
        "fallback": "Abeg I no understand 🙏 Try like \"Mama Bisi collect 3 bags rice, she go pay 45k Friday\".",
        "error": "Abeg try again 🙏 Something no work well for my side.",
        "didnt_hear": "Abeg I no hear the voice note well 🙏 Send am again.",
        "unsupported": "For now I fit understand voice note and text 🙏 Just talk wetin you sell or who pay.",
        "profile_saved": "Nice to meet you, {name}! 🙌 You fit start to record your sales now.",
    },
}


def t(lang: Lang, key: str, **kw) -> str:
    table = _T.get(lang, _T["english"])
    return table[key].format(**kw)


# ---------- receipts ----------------------------------------------------------------------

BUTTON_CONFIRM, BUTTON_FIX, BUTTON_UNDO = "confirm", "fix", "undo"


def entry_buttons(entry_id: int) -> list[Button]:
    return [
        Button(f"{BUTTON_CONFIRM}:{entry_id}", "Correct ✓"),
        Button(f"{BUTTON_FIX}:{entry_id}", "Fix it ✏️"),
        Button(f"{BUTTON_UNDO}:{entry_id}", "Undo ↩️"),
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
    ("3 charger charger", "2 crates crate") is dropped."""
    unit, item = entry.unit or "", entry.item_description or ""
    if unit and item and unit.lower().rstrip("s") == item.lower().rstrip("s"):
        unit = ""
    parts = [_qty(entry.quantity), unit, item]
    text = " ".join(p for p in parts if p).strip()
    return text or None


def balance_sentence(name: str, balance_kobo: int, lang: Lang, *, total: bool = False) -> str:
    if balance_kobo > 0:
        return t(lang, "now_owes" if total else "still_owes", name=name, amount=format_naira(balance_kobo))
    if balance_kobo == 0:
        return t(lang, "cleared", name=name)
    return t(lang, "credit", name=name, amount=format_naira(-balance_kobo))


def sale_receipt(
    entry: Entry, customer: Customer | None, balance_kobo: int | None, lang: Lang, today: date | None = None
) -> Reply:
    lines = [t(lang, "recorded"), ""]
    if customer:
        lines.append(f"👤 {customer.display_name}")
    if item := item_line(entry):
        lines.append(f"🛒 {item}")
    if entry.type == EntryType.CREDIT_SALE and entry.paid_now_kobo:
        # Part-paid: show the full price, what was paid on the spot, and what's left — all from the DB.
        lines += [
            f"💰 {format_naira(entry.amount_kobo)} {t(lang, 'sale_total')}",
            f"💵 {format_naira(entry.paid_now_kobo)} {t(lang, 'paid_now')}",
            f"⏳ {format_naira(entry.amount_kobo - entry.paid_now_kobo)} — {t(lang, 'owing')}",
        ]
    elif entry.type == EntryType.CREDIT_SALE:
        lines.append(f"💰 {format_naira(entry.amount_kobo)} — {t(lang, 'owing')}")
    if entry.type == EntryType.CREDIT_SALE:
        if entry.due_date:
            lines.append(f"📅 {t(lang, 'due')}: {format_date(entry.due_date, today)}")
        else:
            lines.append(f"📅 {t(lang, 'no_due')}")
    else:
        lines.append(f"💰 {format_naira(entry.amount_kobo)} — {t(lang, 'paid_cash')}")
    if customer and balance_kobo is not None and entry.type == EntryType.CREDIT_SALE:
        lines += ["", balance_sentence(customer.display_name, balance_kobo, lang, total=True)]
    return Reply("\n".join(lines), entry_buttons(entry.id))


def payment_receipt(entry: Entry, customer: Customer, balance_kobo: int, lang: Lang) -> Reply:
    lines = [
        t(lang, "payment_recorded"),
        "",
        f"👤 {customer.display_name}",
        f"💰 {format_naira(entry.amount_kobo)} {t(lang, 'received')}",
        "",
        balance_sentence(customer.display_name, balance_kobo, lang),
    ]
    return Reply("\n".join(lines), entry_buttons(entry.id))


def expense_receipt(entry: Entry, lang: Lang) -> Reply:
    lines = [t(lang, "expense_recorded"), "", f"🧾 {entry.item_description}", f"💸 {format_naira(entry.amount_kobo)}"]
    return Reply("\n".join(lines), entry_buttons(entry.id))


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


def balance_reply(debtor: Debtor, lang: Lang, today: date | None = None) -> Reply:
    today = today or today_local()
    name = debtor.customer.display_name
    if debtor.balance_kobo <= 0:
        text = t(lang, "balance_zero", name=name)
        if debtor.balance_kobo < 0:
            text = balance_sentence(name, debtor.balance_kobo, lang)
        return Reply(text)
    lines = [t(lang, "balance", name=name, amount=format_naira(debtor.balance_kobo))]
    if due := _due_text(debtor, lang, today):
        lines.append(due)
    return Reply("\n".join(lines))


def debtors_reply(debtors: list[Debtor], lang: Lang, *, overdue_only: bool = False, today: date | None = None) -> Reply:
    today = today or today_local()
    if not debtors:
        return Reply(t(lang, "no_overdue" if overdue_only else "no_debtors"))
    title = t(lang, "overdue_title" if overdue_only else "debtors_title", count=len(debtors))
    lines = [title, ""]
    for i, d in enumerate(debtors, 1):
        line = f"{i}. {d.customer.display_name} — {format_naira(d.balance_kobo)}"
        if d.overdue:
            line += f" · ⚠️ {format_date(d.next_due, today)}"
        elif d.due_today:
            line += " · 📅 today"
        elif d.next_due:
            line += f" · due {format_date(d.next_due, today)}"
        lines.append(line)
    total = sum(d.balance_kobo for d in debtors)
    lines += ["", f"{t(lang, 'total')}: {format_naira(total)}"]
    return Reply("\n".join(lines))


_PERIOD_LABELS = {
    "english": {
        "today": "Today", "yesterday": "Yesterday", "this_week": "This week",
        "last_week": "Last week", "this_month": "This month", "last_month": "Last month",
    },
    "pidgin": {
        "today": "Today", "yesterday": "Yesterday", "this_week": "This week",
        "last_week": "Last week", "this_month": "This month", "last_month": "Last month",
    },
}


def summary_reply(s: SalesSummary, lang: Lang) -> Reply:
    label = _PERIOD_LABELS.get(lang, _PERIOD_LABELS["english"])[s.period]
    if lang == "pidgin":
        rows = [
            f"📊 {label}",
            "",
            f"💵 Cash sales: {format_naira(s.cash_sales_kobo)}",
            f"📝 Credit sales: {format_naira(s.credit_sales_kobo)}",
            f"🧮 Total wey you sell: {format_naira(s.total_sales_kobo)}",
            f"✅ Debt money wey you collect: {format_naira(s.collected_kobo)}",
        ]
    else:
        rows = [
            f"📊 {label}",
            "",
            f"💵 Cash sales: {format_naira(s.cash_sales_kobo)}",
            f"📝 Credit sales: {format_naira(s.credit_sales_kobo)}",
            f"🧮 Total sales: {format_naira(s.total_sales_kobo)}",
            f"✅ Debt payments collected: {format_naira(s.collected_kobo)}",
        ]
    if s.expenses_kobo:
        rows.append(f"🧾 Expenses: {format_naira(s.expenses_kobo)}")
    return Reply("\n".join(rows))


def onboarding_reply(lang: Lang) -> Reply:
    if lang == "pidgin":
        text = (
            "👋 Welcome to OwoBook!\n\n"
            "I be your book-keeper inside WhatsApp. Just send voice note or text, I go write am down.\n\n"
            "Try am:\n"
            "🎤 \"Mama Bisi collect 3 bags rice, she go pay 45k Friday\"\n"
            "🎤 \"Who dey owe me?\"\n\n"
            "Wetin be your name and your shop name?"
        )
    else:
        text = (
            "👋 Welcome to OwoBook!\n\n"
            "I'm your bookkeeper inside WhatsApp. Just send a voice note or text and I'll record it.\n\n"
            "Try:\n"
            "🎤 \"Mama Bisi took 3 bags of rice, she'll pay 45k on Friday\"\n"
            "🎤 \"Who owes me money?\"\n\n"
            "What's your name and your shop's name?"
        )
    return Reply(text)


def render_plain(reply: Reply) -> str:
    """Text-only rendering (simulator, logs)."""
    if not reply.buttons:
        return reply.text
    buttons = "   ".join(f"[{i}] {b.title}" for i, b in enumerate(reply.buttons, 1))
    return f"{reply.text}\n\n{buttons}"
