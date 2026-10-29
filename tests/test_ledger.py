from datetime import date, datetime, timezone

import pytest

from owobook import ledger
from owobook.models import EntryStatus, EntryType

TUE = date(2026, 10, 27)


def at(day: int, hour: int = 10) -> datetime:
    """A UTC timestamp in October 2026."""
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def trader(db):
    t, created = ledger.get_or_create_trader(db, "2348012345678")
    assert created
    return t


def sale(db, trader, name, naira, due=None, now=None, paid=False):
    return ledger.record_sale(
        db, trader, customer_name=name, amount_kobo=naira * 100, paid=paid, due_date=due, now=now
    )


def test_get_or_create_trader_is_idempotent(db, trader):
    again, created = ledger.get_or_create_trader(db, "2348012345678")
    assert again.id == trader.id and not created


def test_normalize_name():
    assert ledger.normalize_name("  Mama  Bisi! ") == "mama bisi"


def test_credit_sale_then_payment_balance(db, trader):
    entry = sale(db, trader, "Mama Bisi", 45_000, due=date(2026, 10, 30))
    assert entry.type == EntryType.CREDIT_SALE
    assert entry.customer.display_name == "Mama Bisi"
    ledger.record_payment(db, trader, customer_name="mama bisi", amount_kobo=2_000_000)
    assert ledger.customer_balance(db, trader, entry.customer) == 2_500_000


def test_cash_sale_without_customer_has_no_debt(db, trader):
    entry = sale(db, trader, None, 12_000, paid=True, due=date(2026, 10, 30))
    assert entry.type == EntryType.CASH_SALE
    assert entry.customer_id is None and entry.due_date is None
    assert ledger.list_debtors(db, trader, today=TUE) == []


def test_credit_sale_needs_customer_and_amount(db, trader):
    with pytest.raises(ledger.LedgerError):
        sale(db, trader, None, 45_000)
    with pytest.raises(ledger.LedgerError):
        sale(db, trader, "Mama Bisi", 0)


def test_similar_name_is_ambiguous(db, trader):
    sale(db, trader, "Mama Bisi", 45_000)
    with pytest.raises(ledger.AmbiguousCustomer) as exc:
        sale(db, trader, "Iya Bisi", 10_000)
    assert [c.display_name for c in exc.value.candidates] == ["Mama Bisi"]
    with pytest.raises(ledger.AmbiguousCustomer):
        sale(db, trader, "Mama Bisii", 10_000)  # transcription typo
    # once the trader confirms it's a new person, it's allowed
    entry = ledger.record_sale(
        db, trader, customer_name="Iya Bisi", amount_kobo=1_000_000, paid=False, new_customer=True
    )
    assert entry.customer.display_name == "Iya Bisi"


def test_different_names_are_not_ambiguous(db, trader):
    sale(db, trader, "Mama Bisi", 45_000)
    assert sale(db, trader, "Chidi", 5_000).customer.display_name == "Chidi"


def test_payment_from_unknown_customer_is_refused(db, trader):
    with pytest.raises(ledger.UnknownCustomer):
        ledger.record_payment(db, trader, customer_name="Stranger", amount_kobo=100)


def test_deleted_entries_do_not_count(db, trader):
    entry = sale(db, trader, "Chidi", 10_000)
    ledger.delete_entry(db, trader, entry.id)
    assert entry.status == EntryStatus.DELETED
    assert ledger.customer_balance(db, trader, entry.customer) == 0


def test_debtors_sorted_with_fifo_due_dates(db, trader):
    sale(db, trader, "Mama Bisi", 45_000, due=date(2026, 10, 20), now=at(1))  # overdue
    sale(db, trader, "Mama Bisi", 10_000, due=date(2026, 11, 5), now=at(2))
    sale(db, trader, "Chidi", 80_000, due=date(2026, 10, 27), now=at(3))  # due today
    sale(db, trader, "Ada", 5_000, now=at(4))  # no due date
    sale(db, trader, "Paid Up", 5_000, now=at(4))
    ledger.record_payment(db, trader, customer_name="Paid Up", amount_kobo=500_000)

    debtors = ledger.list_debtors(db, trader, today=TUE)
    assert [d.customer.display_name for d in debtors] == ["Chidi", "Mama Bisi", "Ada"]
    chidi, bisi, ada = debtors
    assert chidi.due_today and not chidi.overdue
    assert bisi.overdue and bisi.next_due == date(2026, 10, 20) and bisi.balance_kobo == 5_500_000
    assert ada.next_due is None and not ada.overdue

    # paying off the oldest debt moves the due date to the next unpaid sale
    ledger.record_payment(db, trader, customer_name="Mama Bisi", amount_kobo=4_500_000, now=at(26))
    bisi = ledger.debtor_for(db, trader, bisi.customer, TUE)
    assert bisi.next_due == date(2026, 11, 5) and not bisi.overdue

    overdue = ledger.list_debtors(db, trader, overdue_only=True, today=TUE)
    assert [d.customer.display_name for d in overdue] == ["Chidi"]


def test_sales_summary_by_period(db, trader):
    sale(db, trader, None, 12_000, paid=True, now=at(27, 9))  # today
    sale(db, trader, "Chidi", 30_000, now=at(27, 12))  # today
    ledger.record_payment(db, trader, customer_name="Chidi", amount_kobo=1_000_000, now=at(27, 13))
    ledger.record_expense(db, trader, description="transport", amount_kobo=200_000, now=at(27, 14))
    sale(db, trader, None, 7_000, paid=True, now=at(26, 9))  # yesterday (Monday, same week)
    sale(db, trader, None, 99_000, paid=True, now=at(20))  # last week
    # 23:30 UTC on the 26th is already the 27th in Lagos
    sale(db, trader, None, 1_000, paid=True, now=datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc))

    today = ledger.sales_summary(db, trader, "today", TUE)
    assert today.cash_sales_kobo == 1_300_000
    assert today.credit_sales_kobo == 3_000_000
    assert today.total_sales_kobo == 4_300_000
    assert today.collected_kobo == 1_000_000
    assert today.expenses_kobo == 200_000

    assert ledger.sales_summary(db, trader, "yesterday", TUE).cash_sales_kobo == 700_000
    assert ledger.sales_summary(db, trader, "this_week", TUE).total_sales_kobo == 5_000_000
    assert ledger.sales_summary(db, trader, "last_week", TUE).cash_sales_kobo == 9_900_000


def test_update_entry(db, trader):
    entry = sale(db, trader, "Chidi", 45_000, due=date(2026, 10, 30))
    ledger.confirm_entry(db, trader, entry.id)
    ledger.update_entry(db, trader, entry.id, quantity=4, unit="bags", amount_kobo=6_000_000)
    assert entry.amount_kobo == 6_000_000 and entry.quantity == 4
    assert entry.status == EntryStatus.PENDING  # needs confirming again
    ledger.update_entry(db, trader, entry.id, paid=True)
    assert entry.type == EntryType.CASH_SALE and entry.due_date is None


def test_traders_cannot_touch_each_others_entries(db, trader):
    entry = sale(db, trader, "Chidi", 45_000)
    other, _ = ledger.get_or_create_trader(db, "2349099999999")
    with pytest.raises(ledger.EntryNotFound):
        ledger.delete_entry(db, other, entry.id)
    assert ledger.list_debtors(db, other, today=TUE) == []
    assert ledger.list_customers(db, other) == []


def test_editing_session(db, trader):
    entry = sale(db, trader, "Chidi", 45_000)
    ledger.start_editing(db, trader, entry.id)
    sess = ledger.get_session(db, trader)
    assert sess.mode == "editing" and sess.editing_entry_id == entry.id
    ledger.stop_editing(db, trader)
    assert sess.mode == "normal" and sess.editing_entry_id is None


# ---------- part-payments ------------------------------------------------------------------


def test_part_paid_sale_only_owes_the_rest(db, trader):
    entry = ledger.record_sale(
        db, trader, customer_name="Iro", amount_kobo=130_000_00, paid_now_kobo=60_000_00, paid=False,
        due_date=date(2026, 10, 30), now=at(27),
    )
    assert entry.type == EntryType.CREDIT_SALE
    assert ledger.customer_balance(db, trader, entry.customer) == 70_000_00
    [(sale_, remaining)] = ledger.outstanding_sales(db, trader, entry.customer)
    assert sale_.id == entry.id and remaining == 70_000_00
    [debtor] = ledger.list_debtors(db, trader, today=TUE)
    assert debtor.balance_kobo == 70_000_00 and debtor.next_due == date(2026, 10, 30)

    # a later payment of the balance clears it
    ledger.record_payment(db, trader, customer_name="Iro", amount_kobo=70_000_00, now=at(28))
    assert ledger.customer_balance(db, trader, entry.customer) == 0
    assert ledger.list_debtors(db, trader, today=TUE) == []


def test_part_payment_counts_as_collected_in_summary(db, trader):
    ledger.record_sale(db, trader, customer_name="Iro", amount_kobo=130_000_00, paid_now_kobo=60_000_00,
                       paid=False, now=at(27))
    ledger.record_payment(db, trader, customer_name="Iro", amount_kobo=10_000_00, now=at(27, 12))
    s = ledger.sales_summary(db, trader, "today", TUE)
    assert s.credit_sales_kobo == 130_000_00
    assert s.collected_kobo == 70_000_00  # 60k on the spot + 10k payment


def test_paying_everything_on_the_spot_is_a_cash_sale(db, trader):
    entry = ledger.record_sale(db, trader, customer_name="Iro", amount_kobo=50_000_00, paid_now_kobo=50_000_00,
                               paid=False, due_date=date(2026, 10, 30))
    assert entry.type == EntryType.CASH_SALE and entry.paid_now_kobo == 0 and entry.due_date is None


def test_paid_now_more_than_price_is_rejected(db, trader):
    with pytest.raises(ledger.LedgerError):
        ledger.record_sale(db, trader, customer_name="Iro", amount_kobo=50_000_00, paid_now_kobo=60_000_00, paid=False)


def test_edit_paid_now(db, trader):
    entry = ledger.record_sale(db, trader, customer_name="Iro", amount_kobo=130_000_00, paid_now_kobo=60_000_00,
                               paid=False)
    ledger.update_entry(db, trader, entry.id, paid_now_kobo=80_000_00)
    assert ledger.customer_balance(db, trader, entry.customer) == 50_000_00
    ledger.update_entry(db, trader, entry.id, paid=True)  # "e don pay everything"
    assert entry.type == EntryType.CASH_SALE and entry.paid_now_kobo == 0
    assert ledger.customer_balance(db, trader, entry.customer) == 0


@pytest.mark.parametrize("raw, expected", [
    ("0803 123 4567", "2348031234567"),
    ("08031234567", "2348031234567"),
    ("+234 803 123 4567", "2348031234567"),
    ("+234 0803 123 4567", "2348031234567"),
    ("2348031234567", "2348031234567"),
    ("803-123-4567", "2348031234567"),
    ("+44 7911 123456", "447911123456"),
    ("0803 12", None),
    ("12345", None),
    ("call me", None),
])
def test_normalize_phone(raw, expected):
    assert ledger.normalize_phone(raw) == expected
