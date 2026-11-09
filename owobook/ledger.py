"""Ledger business logic: sales, payments, expenses, purchases from suppliers, savings, stock and notes;
balances, debtors, who the trader owes, summaries.

Pure database logic — no LLM or WhatsApp calls here. Every function takes a SQLAlchemy
session and a trader, so one trader can never see or touch another trader's ledger.
"""

import re
import string
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from owobook.currency import calling_code, currency_for_wa_id
from owobook.dates import period_bounds, to_local, today_local, utcnow
from owobook.models import (
    Customer,
    Entry,
    EntrySource,
    EntryStatus,
    EntryType,
    Note,
    SessionMode,
    StockItem,
    StockMove,
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
    trader = Trader(wa_id=wa_id, currency=currency_for_wa_id(wa_id))
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


def normalize_phone(text: str, default_country: str = "234") -> str | None:
    """A phone number as WhatsApp wants it: digits with country code, no '+' or leading 0.

    '0803 123 4567' → '2348031234567' (Nigerian local), '+234 803…' / '234803…' kept,
    '803 123 4567' → '2348031234567'. Anything that isn't a plausible number → None.
    """
    has_plus = text.strip().startswith("+")
    digits = re.sub(r"\D", "", text)
    if has_plus or digits.startswith(default_country):
        number = digits
    elif digits.startswith("0") and len(digits) in (10, 11):  # local format: 0803… (NG), 0712… (KE)
        number = default_country + digits[1:]
    elif len(digits) == 10 and digits[0] in "6789":  # local number without the 0
        number = default_country + digits
    else:
        return None
    if number.startswith(default_country + "0"):  # "+234 0803…"
        number = default_country + number[len(default_country) + 1:]
    return number if 10 <= len(number) <= 15 else None


def set_customer_phone(db: Session, trader: Trader, customer: Customer, phone: str) -> Customer:
    """Save a customer's WhatsApp number (normalized), for one-tap reminders. Local numbers ("0712…")
    get the trader's own country code."""
    number = normalize_phone(phone, calling_code(trader.wa_id))
    if not number:
        raise LedgerError(f"Not a phone number: {phone!r}", question="That number no complete. Abeg send am again?")
    customer.phone = number
    db.flush()
    return customer


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
    entry = _new_entry(
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
    apply_stock(db, trader, entry)
    return entry


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
        creates = entry.type not in (EntryType.PAYMENT, EntryType.SUPPLIER_PAYMENT) or new_customer
        entry.customer_id = resolve_customer(db, trader, customer_name, create=creates, new_customer=new_customer).id
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
    if entry.type == EntryType.PURCHASE:
        if paid is not None:
            entry.paid_now_kobo = entry.amount_kobo if paid else 0
        if paid_now_kobo is not None:
            entry.paid_now_kobo = paid_now_kobo
        _check_paid_now(entry.amount_kobo, entry.paid_now_kobo)
        if due_date is not _UNSET:
            entry.due_date = due_date
        if entry.paid_now_kobo == entry.amount_kobo:
            entry.due_date = None
    else:
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
    reverse_stock(db, trader, entry)  # quantity, item or unit may have changed
    apply_stock(db, trader, entry)
    return entry


def confirm_entry(db: Session, trader: Trader, entry_id: int) -> Entry:
    entry = get_entry(db, trader, entry_id)
    if entry.status == EntryStatus.DELETED:
        raise LedgerError("Entry was undone", question="That record don already cancel.")
    entry.status = EntryStatus.CONFIRMED
    db.flush()
    return entry


def delete_entry(db: Session, trader: Trader, entry_id: int) -> Entry:
    """Soft-delete (Undo). The row stays for the audit trail; any stock it moved goes back."""
    entry = get_entry(db, trader, entry_id)
    entry.status = EntryStatus.DELETED
    entry.updated_at = utcnow()
    reverse_stock(db, trader, entry)
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


def customer_balance(db: Session, trader: Trader, customer: Customer, *, upto_entry_id: int | None = None) -> int:
    """Credit sales (less anything paid on the spot) minus payments, in kobo (pending + confirmed; undone entries ignored).
    With `upto_entry_id`: the balance right after that entry (for receipts of earlier sales/payments)."""
    query = select(func.coalesce(func.sum(_signed_amount), 0)).where(
        Entry.trader_id == trader.id,
        Entry.customer_id == customer.id,
        Entry.status != EntryStatus.DELETED,
    )
    if upto_entry_id is not None:
        query = query.where(Entry.id <= upto_entry_id)
    return int(db.scalar(query) or 0)


RECEIPT_TYPES = (EntryType.CASH_SALE, EntryType.CREDIT_SALE, EntryType.PAYMENT)


def latest_receipt_entry(db: Session, trader: Trader, customer: Customer | None = None) -> Entry | None:
    """The newest sale or payment (for this customer, if given) — what "send me receipt" means."""
    query = select(Entry).where(
        Entry.trader_id == trader.id,
        Entry.status != EntryStatus.DELETED,
        Entry.type.in_(RECEIPT_TYPES),
    )
    if customer is not None:
        query = query.where(Entry.customer_id == customer.id)
    return db.scalars(query.order_by(Entry.created_at.desc(), Entry.id.desc()).limit(1)).first()


def _outstanding(db: Session, trader: Trader, person: Customer, debt: str, paying: str) -> list[tuple[Entry, int]]:
    """Debts (`debt` entries) not yet covered by `paying` entries, oldest first, as (entry, remaining_kobo).
    Payments go to the oldest debt first (FIFO), which tells us which due dates still matter."""
    entries = db.scalars(
        select(Entry)
        .where(
            Entry.trader_id == trader.id,
            Entry.customer_id == person.id,
            Entry.status != EntryStatus.DELETED,
            Entry.type.in_([debt, paying]),
        )
        .order_by(Entry.created_at, Entry.id)
    ).all()
    paid = sum(e.amount_kobo for e in entries if e.type == paying)
    out = []
    for item in (e for e in entries if e.type == debt):
        owed = item.amount_kobo - item.paid_now_kobo
        covered = min(paid, owed)
        paid -= covered
        if owed - covered > 0:
            out.append((item, owed - covered))
    return out


def outstanding_sales(db: Session, trader: Trader, customer: Customer) -> list[tuple[Entry, int]]:
    """Credit sales not yet covered by the customer's payments, oldest first."""
    return _outstanding(db, trader, customer, EntryType.CREDIT_SALE, EntryType.PAYMENT)


@dataclass
class PaymentRecord:
    """How a customer has paid their debts: each credit sale with a due date that's been paid off counts
    as on time or late; one still unpaid after its due date counts as overdue."""

    on_time: int = 0
    late: int = 0
    overdue: int = 0  # sales still unpaid after their due date
    overdue_kobo: int = 0  # what's still owed on them
    oldest_overdue: date | None = None

    @property
    def total(self) -> int:
        """Debts paid off (on time or late)."""
        return self.on_time + self.late


def payment_record(db: Session, trader: Trader, customer: Customer, today: date | None = None,
                   before_entry_id: int | None = None) -> PaymentRecord:
    """Replay the customer's sales and payments in order, paying off the oldest debt first (FIFO).
    `before_entry_id`: only look at history before that entry (e.g. the sale being recorded now)."""
    today = today or today_local()
    query = select(Entry).where(
        Entry.trader_id == trader.id,
        Entry.customer_id == customer.id,
        Entry.status != EntryStatus.DELETED,
        Entry.type.in_([EntryType.CREDIT_SALE, EntryType.PAYMENT]),
    )
    if before_entry_id is not None:
        query = query.where(Entry.id < before_entry_id)
    record, open_sales = PaymentRecord(), []  # [sale, still owed], oldest first
    for e in db.scalars(query.order_by(Entry.created_at, Entry.id)):
        if e.type == EntryType.CREDIT_SALE:
            if e.amount_kobo - e.paid_now_kobo > 0:
                open_sales.append([e, e.amount_kobo - e.paid_now_kobo])
            continue
        money, paid_on = e.amount_kobo, to_local(e.created_at).date()
        while money > 0 and open_sales:
            sale = open_sales[0]
            used = min(money, sale[1])
            sale[1] -= used
            money -= used
            if sale[1] == 0:
                open_sales.pop(0)
                if sale[0].due_date:
                    if paid_on <= sale[0].due_date:
                        record.on_time += 1
                    else:
                        record.late += 1
    for sale, owed in open_sales:
        if sale.due_date and sale.due_date < today:
            record.overdue += 1
            record.overdue_kobo += owed
            record.oldest_overdue = min(record.oldest_overdue or sale.due_date, sale.due_date)
    return record


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
    purchases_kobo: int = 0  # stock bought, cash or on credit
    supplier_paid_kobo: int = 0  # money paid to suppliers (on the spot or later)
    saved_kobo: int = 0

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
        if type_ == EntryType.CREDIT_SALE:
            s.collected_kobo += int(paid_now or 0)  # money paid on the spot for credit sales
        if type_ == EntryType.CASH_SALE:
            s.cash_sales_kobo, s.sale_count = total, s.sale_count + count
        elif type_ == EntryType.CREDIT_SALE:
            s.credit_sales_kobo, s.sale_count = total, s.sale_count + count
        elif type_ == EntryType.PAYMENT:
            s.collected_kobo += total
        elif type_ == EntryType.EXPENSE:
            s.expenses_kobo = total
        elif type_ == EntryType.PURCHASE:
            s.purchases_kobo = total
            s.supplier_paid_kobo += int(paid_now or 0)
        elif type_ == EntryType.SUPPLIER_PAYMENT:
            s.supplier_paid_kobo += total
        elif type_ == EntryType.SAVING:
            s.saved_kobo = total
    return s


# ---------- suppliers: what the trader owes ------------------------------------------------


def record_purchase(
    db: Session,
    trader: Trader,
    *,
    amount_kobo: int,
    paid_now_kobo: int,
    supplier_name: str | None = None,
    item_description: str | None = None,
    quantity: float | None = None,
    unit: str | None = None,
    due_date: date | None = None,
    new_supplier: bool = False,
    source: str = EntrySource.TEXT,
    raw_text: str | None = None,
    now: datetime | None = None,
) -> Entry:
    """Record goods the trader bought. `paid_now_kobo` is what they paid on the spot (all of it for a cash
    purchase); the rest is owed to the supplier. Bought items with a quantity go into stock."""
    if amount_kobo <= 0:
        raise LedgerError("Amount must be positive", question="How much you buy am?")
    _check_paid_now(amount_kobo, paid_now_kobo)
    owing = paid_now_kobo < amount_kobo
    if owing and not supplier_name:
        raise LedgerError("Credit purchase without supplier", question="Who you buy am from?")
    supplier = (
        resolve_customer(db, trader, supplier_name, create=True, new_customer=new_supplier) if supplier_name else None
    )
    entry = _new_entry(
        db, trader, now,
        customer_id=supplier.id if supplier else None,
        type=EntryType.PURCHASE,
        item_description=item_description,
        quantity=quantity,
        unit=unit,
        amount_kobo=amount_kobo,
        paid_now_kobo=paid_now_kobo,
        due_date=due_date if owing else None,
        source=source,
        raw_text=raw_text,
    )
    apply_stock(db, trader, entry)
    return entry


def record_supplier_payment(
    db: Session,
    trader: Trader,
    *,
    supplier_name: str,
    amount_kobo: int,
    new_supplier: bool = False,
    source: str = EntrySource.TEXT,
    raw_text: str | None = None,
    now: datetime | None = None,
) -> Entry:
    """Record money the trader paid a supplier towards what they owe."""
    if amount_kobo <= 0:
        raise LedgerError("Amount must be positive", question="How much you pay?")
    supplier = resolve_customer(db, trader, supplier_name, create=new_supplier, new_customer=new_supplier)
    return _new_entry(
        db, trader, now,
        customer_id=supplier.id, type=EntryType.SUPPLIER_PAYMENT, amount_kobo=amount_kobo,
        source=source, raw_text=raw_text,
    )


_owed_to_supplier = case(
    (Entry.type == EntryType.PURCHASE, Entry.amount_kobo - Entry.paid_now_kobo),
    (Entry.type == EntryType.SUPPLIER_PAYMENT, -Entry.amount_kobo),
    else_=0,
)


def supplier_balance(db: Session, trader: Trader, supplier: Customer) -> int:
    """What the trader owes this supplier, in kobo (negative: the trader overpaid)."""
    query = select(func.coalesce(func.sum(_owed_to_supplier), 0)).where(
        Entry.trader_id == trader.id, Entry.customer_id == supplier.id, Entry.status != EntryStatus.DELETED,
    )
    return int(db.scalar(query) or 0)


@dataclass
class Creditor:
    supplier: Customer
    owed_kobo: int
    next_due: date | None  # earliest due date among unpaid purchases
    overdue: bool
    due_today: bool


def creditor_for(db: Session, trader: Trader, supplier: Customer, today: date | None = None) -> Creditor:
    today = today or today_local()
    dues = [e.due_date for e, _ in _outstanding(db, trader, supplier, EntryType.PURCHASE, EntryType.SUPPLIER_PAYMENT)
            if e.due_date]
    next_due = min(dues) if dues else None
    return Creditor(supplier, supplier_balance(db, trader, supplier), next_due,
                    bool(next_due and next_due < today), next_due == today)


def list_creditors(db: Session, trader: Trader, *, due_only: bool = False, today: date | None = None) -> list[Creditor]:
    """Suppliers the trader owes, biggest first; `due_only`: only what's due today or overdue."""
    today = today or today_local()
    rows = db.execute(
        select(Customer, func.sum(_owed_to_supplier))
        .join(Entry, Entry.customer_id == Customer.id)
        .where(Entry.trader_id == trader.id, Entry.status != EntryStatus.DELETED)
        .group_by(Customer.id)
        .having(func.sum(_owed_to_supplier) > 0)
    ).all()
    creditors = [creditor_for(db, trader, supplier, today) for supplier, _ in rows]
    if due_only:
        creditors = [c for c in creditors if c.overdue or c.due_today]
    return sorted(creditors, key=lambda c: (-c.owed_kobo, c.supplier.display_name))


# ---------- savings: ajo, esusu, savings box ------------------------------------------------


def record_saving(
    db: Session,
    trader: Trader,
    *,
    amount_kobo: int,
    group: str | None = None,
    payout: bool = False,
    source: str = EntrySource.TEXT,
    raw_text: str | None = None,
    now: datetime | None = None,
) -> Entry:
    """Money put into savings (`payout=False`) or collected back out ("I collect my ajo 50k")."""
    if amount_kobo <= 0:
        raise LedgerError("Amount must be positive", question="How much?")
    return _new_entry(
        db, trader, now,
        type=EntryType.SAVING_PAYOUT if payout else EntryType.SAVING,
        item_description=(group or "savings").strip(), amount_kobo=amount_kobo, source=source, raw_text=raw_text,
    )


@dataclass
class SavingsPot:
    name: str
    saved_kobo: int = 0
    collected_kobo: int = 0

    @property
    def balance_kobo(self) -> int:
        return self.saved_kobo - self.collected_kobo


def savings_summary(db: Session, trader: Trader) -> list[SavingsPot]:
    """Each savings pot (ajo, esusu, …): put in, collected out, still in. Biggest first."""
    pots: dict[str, SavingsPot] = {}
    for e in db.scalars(select(Entry).where(
        Entry.trader_id == trader.id, Entry.status != EntryStatus.DELETED,
        Entry.type.in_([EntryType.SAVING, EntryType.SAVING_PAYOUT]),
    ).order_by(Entry.created_at, Entry.id)):
        key = normalize_name(e.item_description or "savings") or "savings"
        pot = pots.setdefault(key, SavingsPot(_display(e.item_description or "savings")))
        if e.type == EntryType.SAVING:
            pot.saved_kobo += e.amount_kobo
        else:
            pot.collected_kobo += e.amount_kobo
    return sorted(pots.values(), key=lambda p: (-p.balance_kobo, p.name))


# ---------- stock ----------------------------------------------------------------------------


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def stock_key(name: str | None) -> str:
    """'Bags of Rice' → 'bag of rice'; 'eggs' → 'egg'. How stock items and units are compared."""
    return " ".join(_singular(w) for w in normalize_name(name or "").split())


def list_stock(db: Session, trader: Trader) -> list[StockItem]:
    return list(db.scalars(select(StockItem).where(StockItem.trader_id == trader.id).order_by(StockItem.name)))


def find_stock_item(db: Session, trader: Trader, name: str | None) -> StockItem | None:
    """The stock item a name refers to: the same words, or the only item whose name is inside it
    (or it inside the item's): 'indomie' finds 'indomie noodles'."""
    key = stock_key(name)
    if not key:
        return None
    items = list_stock(db, trader)
    for item in items:
        if item.normalized_name == key:
            return item
    words = set(key.split())
    close = [i for i in items if set(i.normalized_name.split()) <= words or words <= set(i.normalized_name.split())]
    return close[0] if len(close) == 1 else None


def _units_match(a: str | None, b: str | None) -> bool:
    return not a or not b or stock_key(a) == stock_key(b)


def _move(db: Session, trader: Trader, item: StockItem, change: float, reason: str, entry: Entry | None = None) -> None:
    item.quantity = round(item.quantity + change, 3)
    item.updated_at = utcnow()
    db.add(StockMove(trader_id=trader.id, item_id=item.id, entry_id=entry.id if entry else None,
                     change=change, reason=reason))
    db.flush()


def change_stock(db: Session, trader: Trader, name: str, quantity: float, *, unit: str | None = None,
                 mode: str = "add") -> tuple[StockItem, float]:
    """Restock ("add"), count ("set": "I get 17 bags rice remaining") or take out ("remove": spoilt, used).
    Creates the item on first add or count. Returns (item, change)."""
    if quantity is None or quantity < 0 or (mode != "set" and quantity == 0):
        raise LedgerError("Bad quantity", question="How many?")
    item = find_stock_item(db, trader, name)
    if item is None:
        if mode == "remove":
            raise LedgerError(f"No stock item {name!r}", question=f"I no get {name} for your stock. Which item?")
        item = StockItem(trader_id=trader.id, name=_display(name), normalized_name=stock_key(name), unit=unit, quantity=0)
        db.add(item)
        db.flush()
    if unit and not item.unit:
        item.unit = unit
    change = {"add": quantity, "remove": -quantity, "set": quantity - item.quantity}[mode]
    _move(db, trader, item, change, {"add": "restock", "remove": "remove", "set": "count"}[mode])
    return item, change


def apply_stock(db: Session, trader: Trader, entry: Entry) -> StockItem | None:
    """A sale takes its quantity out of stock, if that item is tracked and the units agree. A purchase puts
    its quantity in, creating the item if needed. Returns the item that moved."""
    if entry.status == EntryStatus.DELETED or not entry.quantity or not entry.item_description:
        return None
    if entry.type in (EntryType.CASH_SALE, EntryType.CREDIT_SALE):
        item = find_stock_item(db, trader, entry.item_description)
        if item and _units_match(item.unit, entry.unit):
            _move(db, trader, item, -entry.quantity, "sale", entry)
            return item
    elif entry.type == EntryType.PURCHASE:
        item = find_stock_item(db, trader, entry.item_description)
        if item is None:
            item = StockItem(trader_id=trader.id, name=_display(entry.item_description),
                             normalized_name=stock_key(entry.item_description), unit=entry.unit, quantity=0)
            db.add(item)
            db.flush()
        if _units_match(item.unit, entry.unit):
            item.unit = item.unit or entry.unit
            _move(db, trader, item, entry.quantity, "restock", entry)
            return item
    return None


def reverse_stock(db: Session, trader: Trader, entry: Entry) -> None:
    """Put back whatever stock this entry moved (on Undo, or before re-applying a fixed entry)."""
    for move in db.scalars(select(StockMove).where(StockMove.trader_id == trader.id, StockMove.entry_id == entry.id)):
        item = db.get(StockItem, move.item_id)
        item.quantity = round(item.quantity - move.change, 3)
        db.delete(move)
    db.flush()


def stock_item_for(db: Session, trader: Trader, entry: Entry) -> StockItem | None:
    """The stock item this entry moved, if any (for the receipt's "left" line)."""
    move = db.scalars(select(StockMove).where(StockMove.trader_id == trader.id, StockMove.entry_id == entry.id)).first()
    return db.get(StockItem, move.item_id) if move else None


# ---------- notes ----------------------------------------------------------------------------


def save_note(db: Session, trader: Trader, text: str, *, source: str = EntrySource.TEXT,
              now: datetime | None = None) -> Note:
    text = (text or "").strip()
    if not text:
        raise LedgerError("Empty note", question="Wetin I go write down?")
    note = Note(trader_id=trader.id, text=text, source=source, created_at=now or utcnow())
    db.add(note)
    db.flush()
    return note


_STOP = {"the", "and", "for", "what", "did", "about", "note", "notes", "my", "i", "a", "an", "of", "to", "wey", "dey",
         "na", "say", "abeg", "show", "me", "all", "find"}


def find_notes(db: Session, trader: Trader, query: str | None = None, limit: int = 10) -> list[Note]:
    """Notes containing every word of the query (or any word, if none has them all); newest first."""
    base = select(Note).where(Note.trader_id == trader.id, Note.deleted.is_(False)).order_by(Note.created_at.desc(), Note.id.desc())
    words = [w for w in normalize_name(query or "").split() if w not in _STOP and len(w) > 1]
    if not words:
        return list(db.scalars(base.limit(limit)))
    every = list(db.scalars(base.where(*[Note.text.ilike(f"%{w}%") for w in words]).limit(limit)))
    if every:
        return every
    return list(db.scalars(base.where(or_(*[Note.text.ilike(f"%{w}%") for w in words])).limit(limit)))


def delete_note(db: Session, trader: Trader, note_id: int | None = None) -> Note:
    """Remove a note (the latest one when no id is given)."""
    query = select(Note).where(Note.trader_id == trader.id, Note.deleted.is_(False))
    query = query.where(Note.id == note_id) if note_id else query.order_by(Note.created_at.desc(), Note.id.desc())
    note = db.scalars(query).first()
    if note is None:
        raise LedgerError("No such note", question="I no see that note.")
    note.deleted = True
    db.flush()
    return note
