from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from owobook.models import Customer, Entry, EntryStatus, EntryType, Trader


def test_create_trader_customer_and_entry(db):
    trader = Trader(wa_id="2348012345678", name="Hosea")
    db.add(trader)
    db.flush()
    customer = Customer(trader_id=trader.id, display_name="Mama Bisi", normalized_name="mama bisi")
    db.add(customer)
    db.flush()
    entry = Entry(
        trader_id=trader.id,
        customer_id=customer.id,
        type=EntryType.CREDIT_SALE,
        item_description="rice",
        quantity=3,
        unit="bags",
        amount_kobo=4_500_000,
        due_date=date(2026, 10, 30),
    )
    db.add(entry)
    db.commit()

    saved = db.get(Entry, entry.id)
    assert saved.status == EntryStatus.PENDING
    assert saved.amount_kobo == 4_500_000
    assert saved.customer.display_name == "Mama Bisi"
    assert trader.onboarded is False


def test_customer_name_unique_per_trader(db):
    a, b = Trader(wa_id="1"), Trader(wa_id="2")
    db.add_all([a, b])
    db.flush()
    db.add_all([
        Customer(trader_id=a.id, display_name="Mama Bisi", normalized_name="mama bisi"),
        Customer(trader_id=b.id, display_name="Mama Bisi", normalized_name="mama bisi"),
    ])
    db.flush()  # same name under different traders is fine

    db.add(Customer(trader_id=a.id, display_name="mama bisi", normalized_name="mama bisi"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_wa_id_is_unique(db):
    db.add_all([Trader(wa_id="234"), Trader(wa_id="234")])
    with pytest.raises(IntegrityError):
        db.flush()
