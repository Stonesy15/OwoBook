"""Ledger business logic: record sales/payments/expenses, balances, debtors, summaries.

Pure database logic — no LLM or WhatsApp calls here. Every function takes a SQLAlchemy
session and a trader, so one trader can never see or touch another trader's ledger.
"""

import re
import string
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from owobook.dates import period_bounds, today_local, utcnow
from owobook.models import (
    Customer,
    Entry,
    EntrySource,
    EntryStatus,
    EntryType,
    SessionMode,
    Trader,
    TraderSession,
)

# Names that mean the same person: "Iya Bisi" = "Mama Bisi" in Yoruba.
_NAME_ALIASES = {"iya": "mama", "mummy": "mama", "mommy": "mama", "baba": "papa", "daddy": "papa"}
FUZZY_THRESHOLD = 0.8


# ---------- errors -------------------------------------------------------------------------


class LedgerError(Exception):
    """Something the trader needs to clarify; `question` is safe to show them."""

    def __init__(self, message: str, question: str | None = None):
        super().__init__(message)
        self.question = question or message


class AmbiguousCustomer(LedgerError):
    def __init__(self, name: str, candidates: list[Customer]):
        self.name = name
        self.candidates = candidates
        options = " or ".join(c.display_name for c in candidates[:3])
        super().__init__(
            f"'{name}' is close to existing customer(s): {options}",
            question=f"You mean {options}? Or is {_display(name)} a new customer?",
        )


class UnknownCustomer(LedgerError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"No customer called '{name}'",
            question=f"I no see {_display(name)} for your book. Who be that?",
        )


class EntryNotFound(LedgerError):
    def __init__(self, entry_id: int):
        super().__init__(f"Entry {entry_id} not found", question="I no fit find that record.")


# ---------- traders & sessions ------------------------------------------------------------


def get_or_create_trader(db: Session, wa_id: str) -> tuple[Trader, bool]:
    """Return (trader, created)."""
    trader = db.scalar(select(Trader).where(Trader.wa_id == wa_id))
    if trader:
        return trader, False
    trader = Trader(wa_id=wa_id)
    db.add(trader)
    db.flush()
    return trader, True


def get_session(db: Session, trader: Trader) -> TraderSession:
    sess = db.get(TraderSession, trader.id)
    if not sess:
        sess = TraderSession(trader_id=trader.id, mode=SessionMode.NORMAL)
        db.add(sess)
        db.flush()
    return sess


def start_editing(db: Session, trader: Trader, entry_id: int) -> None:
    get_entry(db, trader, entry_id)  # ownership check
    sess = get_session(db, trader)
    sess.mode, sess.editing_entry_id = SessionMode.EDITING, entry_id
    db.flush()


def stop_editing(db: Session, trader: Trader) -> None:
    sess = get_session(db, trader)
    sess.mode, sess.editing_entry_id = SessionMode.NORMAL, None
    db.flush()


# ---------- customers ---------------------------------------------------------------------


def normalize_name(name: str) -> str:
    """'  Mama  Bisi! ' → 'mama bisi'."""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _canonical(normalized: str) -> str:
    return " ".join(_NAME_ALIASES.get(w, w) for w in normalized.split())


def _display(name: str) -> str:
    return string.capwords(re.sub(r"\s+", " ", name.strip()))


def list_customers(db: Session, trader: Trader) -> list[Customer]:
    return list(db.scalars(select(Customer).where(Customer.trader_id == trader.id).order_by(Customer.display_name)))


def find_customer(db: Session, trader: Trader, name: str) -> tuple[Customer | None, list[Customer]]:
    """Return (exact match, close candidates). Only one of them is ever non-empty."""
    norm = normalize_name(name)
    customers = list_customers(db, trader)
    for c in customers:
        if c.normalized_name == norm:
            return c, []
    target = _canonical(norm)
    scored = []
    for c in customers:
        other = _canonical(c.normalized_name)
        score = 1.0 if other == target else SequenceMatcher(None, target, other).ratio()
        if score >= FUZZY_THRESHOLD:
            scored.append((score, c))
    scored.sort(key=lambda sc: -sc[0])
    return None, [c for _, c in scored]


def resolve_customer(db: Session, trader: Trader, name: str, *, create: bool, new_customer: bool = False) -> Customer:
    """Find a customer by name, creating one if allowed.

    Close-but-not-exact names raise AmbiguousCustomer unless `new_customer=True`
    (the trader confirmed it's a different person).
    """
    if not normalize_name(name):
        raise LedgerError("Empty customer name", question="Which customer?")
    exact, candidates = find_customer(db, trader, name)
    if exact:
        return exact
    if candidates and not new_customer:
        raise AmbiguousCustomer(name, candidates)
    if not create:
        raise UnknownCustomer(name)
    customer = Customer(trader_id=trader.id, display_name=_display(name), normalized_name=normalize_name(name))
    db.add(customer)
    db.flush()
    return customer


# ---------- writes ------------------------------------------------------------------------


def _new_entry(db: Session, trader: Trader, now: datetime | None, **fields) -> Entry:
    now = now or utcnow()
    entry = Entry(trader_id=trader.id, created_at=now, updated_at=now, **fields)
    db.add(entry)
    db.flush()
    return entry


def record_sale(
    db: Session,
    trader: Trader,
    *,
    amount_kobo: int,
    paid: bool,
    paid_now_kobo: int = 0,
    customer_name: str | None = None,
    item_description: str | None = None,
    quantity: float | None = None,
    unit: str | None = None,
    due_date: date | None = None,
    new_customer: bool = False,
    source: str = EntrySource.TEXT,
    raw_text: str | None = None,
    now: datetime | None = None,
) -> Entry:
    """Record a cash sale (paid=True) or credit sale (paid=False, needs a customer).

    A credit sale can be part-paid: `paid_now_kobo` is what the customer paid on the spot.
    Paying it all on the spot makes it a cash sale."""
    if amount_kobo <= 0:
        raise LedgerError("Amount must be positive", question="How much e cost?")
    _check_paid_now(amount_kobo, paid_now_kobo)
    if paid_now_kobo == amount_kobo:
        paid, paid_now_kobo = True, 0
    if paid:
        paid_now_kobo = 0
    if not paid and not customer_name:
        raise LedgerError("Credit sale without customer", question="Who collect am on credit?")
    customer = (
        resolve_customer(db, trader, customer_name, create=True, new_customer=new_customer) if customer_name else None
    )
    return _new_entry(
        db,
        trader,
        now,
        customer_id=customer.id if customer else None,
        type=EntryType.CASH_SALE if paid else EntryType.CREDIT_SALE,
        item_description=item_description,
        quantity=quantity,
        unit=unit,
        amount_kobo=amount_kobo,
        paid_now_kobo=paid_now_kobo,
        due_date=None if paid else due_date,
        source=source,
        raw_text=raw_text,
    )


def _check_paid_now(amount_kobo: int, paid_now_kobo: int) -> None:
    if paid_now_kobo < 0:
        raise LedgerError("Paid-now amount is negative", question="How much dem pay for hand?")
    if paid_now_kobo > amount_kobo:
        raise LedgerError(
            "Paid-now amount is more than the sale total",
            question="The money wey dem pay pass the price. How much be the full price?",
        )


def record_payment(
    db: Session,
    trader: Trader,
    *,
    customer_name: str,
    amount_kobo: int,
    new_customer: bool = False,
    source: str = EntrySource.TEXT,
    raw_text: str | None = None,
    now: datetime | None = None,
) -> Entry:
    """Record money a customer paid towards their debt. The customer must already exist
    unless the trader confirmed `new_customer`."""
    if amount_kobo <= 0:
        raise LedgerError("Amount must be positive", question="How much dem pay?")
    customer = resolve_customer(db, trader, customer_name, create=new_customer, new_customer=new_customer)
    return _new_entry(
        db,
        trader,
        now,
        customer_id=customer.id,
        type=EntryType.PAYMENT,
        amount_kobo=amount_kobo,
        source=source,
        raw_text=raw_text,
    )


def record_expense(
    db: Session,
    trader: Trader,
    *,
    description: str,
    amount_kobo: int,
    source: str = EntrySource.TEXT,
    raw_text: str | None = None,
    now: datetime | None = None,
) -> Entry:
    if amount_kobo <= 0:
        raise LedgerError("Amount must be positive", question="How much you spend?")
    return _new_entry(
        db,
        trader,
        now,
        type=EntryType.EXPENSE,
        item_description=description,
        amount_kobo=amount_kobo,
        source=source,
        raw_text=raw_text,
    )


def get_entry(db: Session, trader: Trader, entry_id: int) -> Entry:
    entry = db.get(Entry, entry_id)
    if not entry or entry.trader_id != trader.id:
        raise EntryNotFound(entry_id)
    return entry


_UNSET = object()


def update_entry(
    db: Session,
    trader: Trader,
    entry_id: int,
    *,
    customer_name: str | None = None,
    new_customer: bool = False,
    item_description: str | None = None,
    quantity: float | None = None,
    unit: str | None = None,
    amount_kobo: int | None = None,
    paid_now_kobo: int | None = None,
    due_date: date | None | object = _UNSET,
    paid: bool | None = None,
) -> Entry:
    """Apply a correction to an entry. Only the fields given are changed.
    Editing puts the entry back to `pending` so the trader can confirm it again."""
    entry = get_entry(db, trader, entry_id)
    if entry.status == EntryStatus.DELETED:
        raise LedgerError("Entry was undone", question="That record don already cancel.")
    if customer_name:
        entry.customer_id = resolve_customer(
            db, trader, customer_name, create=entry.type != EntryType.PAYMENT or new_customer, new_customer=new_customer
        ).id
    if item_description is not None:
        entry.item_description = item_description
    if quantity is not None:
        entry.quantity = quantity
    if unit is not None:
        entry.unit = unit
    if amount_kobo is not None:
        if amount_kobo <= 0:
            raise LedgerError("Amount must be positive", question="How much e be?")
        entry.amount_kobo = amount_kobo
    if paid is not None and entry.type in (EntryType.CASH_SALE, EntryType.CREDIT_SALE):
        if not paid and entry.customer_id is None:
            raise LedgerError("Credit sale without customer", question="Who collect am on credit?")
        entry.type = EntryType.CASH_SALE if paid else EntryType.CREDIT_SALE
        if paid:
            entry.due_date = None
    if paid_now_kobo is not None and entry.type == EntryType.CREDIT_SALE:
        entry.paid_now_kobo = paid_now_kobo
    if entry.type == EntryType.CREDIT_SALE:
        _check_paid_now(entry.amount_kobo, entry.paid_now_kobo)
        if entry.paid_now_kobo == entry.amount_kobo:  # fully paid after all → cash sale
            entry.type, entry.due_date = EntryType.CASH_SALE, None
    if entry.type != EntryType.CREDIT_SALE:
        entry.paid_now_kobo = 0
    if due_date is not _UNSET and entry.type == EntryType.CREDIT_SALE:
        entry.due_date = due_date
    entry.status = EntryStatus.PENDING
    entry.updated_at = utcnow()
    db.flush()
    return entry


def confirm_entry(db: Session, trader: Trader, entry_id: int) -> Entry:
    entry = get_entry(db, trader, entry_id)
    if entry.status == EntryStatus.DELETED:
        raise LedgerError("Entry was undone", question="That record don already cancel.")
    entry.status = EntryStatus.CONFIRMED
    db.flush()
    return entry


def delete_entry(db: Session, trader: Trader, entry_id: int) -> Entry:
    """Soft-delete (Undo). The row stays for the audit trail."""
    entry = get_entry(db, trader, entry_id)
    entry.status = EntryStatus.DELETED
    entry.updated_at = utcnow()
    db.flush()
    return entry


def recent_entries(db: Session, trader: Trader, limit: int = 5) -> list[Entry]:
    return list(
        db.scalars(
            select(Entry)
            .where(Entry.trader_id == trader.id, Entry.status != EntryStatus.DELETED)
            .order_by(Entry.created_at.desc(), Entry.id.desc())
            .limit(limit)
        )
    )


# ---------- reads -------------------------------------------------------------------------

_signed_amount = case(
    (Entry.type == EntryType.CREDIT_SALE, Entry.amount_kobo - Entry.paid_now_kobo),
    (Entry.type == EntryType.PAYMENT, -Entry.amount_kobo),
    else_=0,
)


def customer_balance(db: Session, trader: Trader, customer: Customer) -> int:
    """Credit sales (less anything paid on the spot) minus payments, in kobo (pending + confirmed; undone entries ignored)."""
    total = db.scalar(
        select(func.coalesce(func.sum(_signed_amount), 0)).where(
            Entry.trader_id == trader.id,
            Entry.customer_id == customer.id,
            Entry.status != EntryStatus.DELETED,
        )
    )
    return int(total or 0)


def outstanding_sales(db: Session, trader: Trader, customer: Customer) -> list[tuple[Entry, int]]:
    """Credit sales not yet covered by payments, oldest first, as (entry, remaining_kobo).

    Payments are applied to the oldest debts first (FIFO), which tells us which due dates still matter.
    """
    entries = db.scalars(
        select(Entry)
        .where(
            Entry.trader_id == trader.id,
            Entry.customer_id == customer.id,
            Entry.status != EntryStatus.DELETED,
            Entry.type.in_([EntryType.CREDIT_SALE, EntryType.PAYMENT]),
        )
        .order_by(Entry.created_at, Entry.id)
    ).all()
    paid = sum(e.amount_kobo for e in entries if e.type == EntryType.PAYMENT)
    out = []
    for sale in (e for e in entries if e.type == EntryType.CREDIT_SALE):
        owed = sale.amount_kobo - sale.paid_now_kobo
        covered = min(paid, owed)
        paid -= covered
        if owed - covered > 0:
            out.append((sale, owed - covered))
    return out


@dataclass
class Debtor:
    customer: Customer
    balance_kobo: int
    next_due: date | None  # earliest due date among unpaid sales
    overdue: bool
    due_today: bool


def debtor_for(db: Session, trader: Trader, customer: Customer, today: date | None = None) -> Debtor:
    today = today or today_local()
    dues = [e.due_date for e, _ in outstanding_sales(db, trader, customer) if e.due_date]
    next_due = min(dues) if dues else None
    return Debtor(
        customer=customer,
        balance_kobo=customer_balance(db, trader, customer),
        next_due=next_due,
        overdue=bool(next_due and next_due < today),
        due_today=next_due == today,
    )


def list_debtors(db: Session, trader: Trader, *, overdue_only: bool = False, today: date | None = None) -> list[Debtor]:
    """Customers who owe money, biggest debt first."""
    today = today or today_local()
    rows = db.execute(
        select(Customer, func.sum(_signed_amount).label("bal"))
        .join(Entry, Entry.customer_id == Customer.id)
        .where(Entry.trader_id == trader.id, Entry.status != EntryStatus.DELETED)
        .group_by(Customer.id)
        .having(func.sum(_signed_amount) > 0)
    ).all()
    debtors = [debtor_for(db, trader, customer, today) for customer, _ in rows]
    if overdue_only:
        debtors = [d for d in debtors if d.overdue or d.due_today]
    return sorted(debtors, key=lambda d: (-d.balance_kobo, d.customer.display_name))


@dataclass
class SalesSummary:
    period: str
    cash_sales_kobo: int = 0
    credit_sales_kobo: int = 0
    collected_kobo: int = 0
    expenses_kobo: int = 0
    sale_count: int = 0

    @property
    def total_sales_kobo(self) -> int:
        return self.cash_sales_kobo + self.credit_sales_kobo


def sales_summary(db: Session, trader: Trader, period: str, today: date | None = None) -> SalesSummary:
    start, end = period_bounds(period, today)
    rows = db.execute(
        select(Entry.type, func.sum(Entry.amount_kobo), func.count(), func.sum(Entry.paid_now_kobo))
        .where(
            Entry.trader_id == trader.id,
            Entry.status != EntryStatus.DELETED,
            Entry.created_at >= start,
            Entry.created_at < end,
        )
        .group_by(Entry.type)
    ).all()
    s = SalesSummary(period=period)
    for type_, total, count, paid_now in rows:
        total = int(total or 0)
        s.collected_kobo += int(paid_now or 0)  # money paid on the spot for credit sales
        if type_ == EntryType.CASH_SALE:
            s.cash_sales_kobo, s.sale_count = total, s.sale_count + count
        elif type_ == EntryType.CREDIT_SALE:
            s.credit_sales_kobo, s.sale_count = total, s.sale_count + count
        elif type_ == EntryType.PAYMENT:
            s.collected_kobo += total
        elif type_ == EntryType.EXPENSE:
            s.expenses_kobo = total
    return s
