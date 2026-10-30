"""Fill a trader's ledger with a realistic week of Lagos market trading, for demos.

    uv run python scripts/seed_demo.py                          # trader = first ALLOWED_WA_IDS number
    uv run python scripts/seed_demo.py --wa-id 2348012345678 --reset

Dates are relative to today (Africa/Lagos), so there is always something due today, something
overdue, something due later, a cleared customer, cash sales across the week and two expenses.
--reset wipes that trader's ledger (entries, customers, today's reminder log) first.
"""

import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from owobook import ledger  # noqa: E402
from owobook.config import get_settings  # noqa: E402
from owobook.dates import TZ, format_date, today_local  # noqa: E402
from owobook.db import migrate, session_scope  # noqa: E402
from owobook.models import Customer, Entry, EntryStatus, MessageLog, Trader, TraderSession  # noqa: E402
from owobook.money import format_money  # noqa: E402
from owobook.reminders import REMINDER_TYPE  # noqa: E402

K = 1000 * 100  # ₦1,000 in kobo


def at(today: date, days_ago: int, hour: int) -> datetime:
    """A Lagos shop-hours time, `days_ago` days before today, as an aware datetime."""
    return datetime.combine(today - timedelta(days=days_ago), time(hour, 15), tzinfo=TZ)


def seed(db: Session, trader: Trader, today: date) -> None:
    def sale(name, item, qty, unit, total_k, *, days_ago, hour=10, paid=False, due_in=None, paid_now_k=0):
        ledger.record_sale(
            db, trader, customer_name=name, item_description=item, quantity=qty, unit=unit,
            amount_kobo=round(total_k * K), paid=paid, paid_now_kobo=round(paid_now_k * K),
            due_date=today + timedelta(days=due_in) if due_in is not None else None,
            now=at(today, days_ago, hour), source="voice",
        )

    def payment(name, amount_k, *, days_ago, hour=15):
        ledger.record_payment(db, trader, customer_name=name, amount_kobo=amount_k * K, now=at(today, days_ago, hour))

    # Due TODAY — took rice on credit, paid a little since
    sale("Mama Bisi", "rice", 3, "bags", 45, days_ago=5, due_in=0)
    payment("Mama Bisi", 10, days_ago=2)
    # OVERDUE
    sale("Alhaji Musa", "cement", 10, "bags", 250, days_ago=12, hour=9, due_in=-3)
    payment("Alhaji Musa", 100, days_ago=6)
    sale("Iya Tola", "semovita", 1, "bag", 32, days_ago=9, hour=11, due_in=-2)
    # Due LATER
    sale("Chidi", "indomie", 2, "cartons", 13, days_ago=2, hour=12, due_in=3)
    sale("Emeka", "beans", 2, "bags", 60, days_ago=1, hour=16, due_in=5, paid_now_k=20)  # part-paid
    # CLEARED
    sale("Blessing", "eggs", 5, "crates", 15, days_ago=8, hour=13, due_in=-4)
    payment("Blessing", 15, days_ago=4)
    # Cash sales across the week (no customer names — walk-in buyers)
    for days_ago, item, qty, unit, total_k in [
        (6, "garri", 2, "bags", 38), (5, "eggs", 5, "crates", 12), (4, "palm oil", 2, "kegs", 28),
        (3, "rice", 3, "paint", 9.5), (2, "noodles", 2, "cartons", 13), (1, "beans", 4, "paint", 11),
        (0, "rice", 1, "bag", 15), (0, "groundnut oil", 1, "keg", 24),
    ]:
        sale(None, item, qty, unit, total_k, days_ago=days_ago, hour=11 + days_ago % 5, paid=True)
    # Expenses
    ledger.record_expense(db, trader, description="transport to Mile 12", amount_kobo=3 * K, now=at(today, 3, 8))
    ledger.record_expense(db, trader, description="shop levy", amount_kobo=5 * K, now=at(today, 1, 9))

    # The demo trader has confirmed everything so far.
    for entry in db.scalars(select(Entry).where(Entry.trader_id == trader.id)):
        entry.status = EntryStatus.CONFIRMED


def reset(db: Session, trader: Trader) -> None:
    db.execute(delete(Entry).where(Entry.trader_id == trader.id))
    db.execute(delete(Customer).where(Customer.trader_id == trader.id))
    db.execute(delete(TraderSession).where(TraderSession.trader_id == trader.id))
    db.execute(delete(MessageLog).where(MessageLog.trader_id == trader.id, MessageLog.type == REMINDER_TYPE))


def main() -> None:
    settings = get_settings()
    default_wa = next(iter(sorted(settings.allowed_wa_id_set)), "2348000000001")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wa-id", default=default_wa, help=f"trader's WhatsApp number (default {default_wa})")
    parser.add_argument("--name", default="Hosea")
    parser.add_argument("--shop", default="Hosea Provisions, Ikotun Market")
    parser.add_argument("--reset", action="store_true", help="wipe this trader's ledger first")
    args = parser.parse_args()

    migrate()
    today = today_local()
    with session_scope() as db:
        trader, _ = ledger.get_or_create_trader(db, args.wa_id)
        existing = db.scalar(select(func.count()).select_from(Entry).where(Entry.trader_id == trader.id))
        if existing and not args.reset:
            sys.exit(f"Trader {args.wa_id} already has {existing} entries. Add --reset to wipe and re-seed.")
        if args.reset:
            reset(db, trader)
        trader.name = trader.name or args.name
        trader.shop_name = trader.shop_name or args.shop
        trader.onboarded = True
        seed(db, trader, today)
        db.flush()

        print(f"Seeded trader {args.wa_id} ({trader.name}, {trader.shop_name}) for {format_date(today, today)}:\n")
        for d in ledger.list_debtors(db, trader, today=today):
            tag = "DUE TODAY" if d.due_today else "OVERDUE" if d.overdue else f"due {format_date(d.next_due, today)}"
            print(f"  {d.customer.display_name:<12} owes {format_money(d.balance_kobo):>9}  {tag}")
        week = ledger.sales_summary(db, trader, "this_week", today)
        print(f"\n  This week: sales {format_money(week.total_sales_kobo)}, collected {format_money(week.collected_kobo)}, "
              f"expenses {format_money(week.expenses_kobo)}")


if __name__ == "__main__":
    main()
