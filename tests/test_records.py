"""More than credit sales: notes, buying from suppliers (and what the trader owes), stock, and savings."""

from datetime import date, datetime, timedelta

from owobook import business_record, ledger, ledger_page, reminders
from owobook.dates import today_local

from tests.test_agent import WA, onboard, say, tap


def trader(db):
    return ledger.get_or_create_trader(db, WA)[0]


# ---------- notes ---------------------------------------------------------------------------


async def test_notes_are_saved_found_and_removed(db):
    await onboard(db)
    replies = await say(db, "Note say supplier go come Tuesday with the new rice")
    assert replies[0].text.startswith("📝 Noted: supplier go come Tuesday")
    await say(db, "Remember Mama Bisi like the big bags")

    found = await say(db, "What did I note about the supplier?")
    assert "supplier go come Tuesday" in found[0].text and "big bags" not in found[0].text
    everything = await say(db, "Show my notes")
    assert "(2)" in everything[0].text

    await say(db, "Delete that note")
    left = ledger.find_notes(db, trader(db))
    assert [n.text for n in left] == ["supplier go come Tuesday with the new rice"]


def test_note_search_falls_back_to_any_word(db):
    tr = ledger.get_or_create_trader(db, WA)[0]
    ledger.save_note(db, tr, "Shop rent due on the 15th")
    assert len(ledger.find_notes(db, tr, "rent landlord")) == 1  # not both words, so any word
    assert ledger.find_notes(db, tr, "electricity") == []


# ---------- suppliers -----------------------------------------------------------------------


async def test_buying_on_credit_and_paying_the_supplier_back(db):
    await onboard(db)
    replies = await say(db, "I collect 20 cartons indomie from Alhaji, I go pay 150k Monday")
    text = replies[0].text
    assert "Purchase recorded" in text and "🏪 Alhaji" in text and "₦150,000 — you owe" in text
    assert "You now owe Alhaji ₦150,000 in total." in text and "Due:" in text
    assert replies[0].buttons  # Correct / Fix it / Undo like any record

    paid = await say(db, "I don pay Alhaji 100k")
    assert "Payment to supplier recorded" in paid[0].text and "You now owe Alhaji ₦50,000" in paid[0].text

    owe = await say(db, "Who I owe?")
    assert "Who you owe (1)" in owe[0].text and "Alhaji — ₦50,000" in owe[0].text
    debtors = await say(db, "Who owe me money?")
    assert "Alhaji" not in debtors[0].text  # what the trader owes is not a customer debt


async def test_cash_purchase_needs_no_supplier_and_shows_in_the_summary(db):
    await onboard(db)
    replies = await say(db, "I buy 10 bags rice 300k cash")
    assert "₦300,000 — paid (cash)" in replies[0].text
    summary = await say(db, "How much I sell today?")
    assert "₦300,000" in summary[0].text  # stock bought line


async def test_undoing_a_purchase_clears_what_is_owed(db):
    await onboard(db)
    replies = await say(db, "I collect 5 bags beans from Iya Oja, I go pay 75k Friday")
    entry_id = replies[0].buttons[2].id.split(":")[1]
    undone = await tap(db, f"undo:{entry_id}")
    assert "You don't owe Iya Oja anything now" in undone[0].text
    assert ledger.list_creditors(db, trader(db)) == []


def test_creditors_due_and_overdue(db):
    tr = ledger.get_or_create_trader(db, WA)[0]
    today = date(2026, 11, 9)
    ledger.record_purchase(db, tr, amount_kobo=100_000_00, paid_now_kobo=0, supplier_name="Alhaji",
                           due_date=today - timedelta(days=2))
    ledger.record_purchase(db, tr, amount_kobo=50_000_00, paid_now_kobo=10_000_00, supplier_name="Chika",
                           due_date=today + timedelta(days=5))
    due = ledger.list_creditors(db, tr, due_only=True, today=today)
    assert [(c.supplier.display_name, c.owed_kobo, c.overdue) for c in due] == [("Alhaji", 100_000_00, True)]
    everyone = ledger.list_creditors(db, tr, today=today)
    assert [c.owed_kobo for c in everyone] == [100_000_00, 40_000_00]


def test_morning_reminder_includes_suppliers_to_pay(db):
    tr = ledger.get_or_create_trader(db, WA)[0]
    today = today_local()
    ledger.record_purchase(db, tr, amount_kobo=80_000_00, paid_now_kobo=0, supplier_name="Alhaji", due_date=today)
    replies = reminders.reminder_replies(db, tr, [], "english", today)
    assert "Your own payments due" in replies[0].text and "Alhaji — ₦80,000" in replies[0].text


# ---------- stock ---------------------------------------------------------------------------


async def test_purchase_fills_stock_and_sales_take_it_out(db):
    await onboard(db)
    bought = await say(db, "I buy 20 bags rice 600k cash")
    assert "📦 Rice: 20 bags left" in bought[0].text

    sold = await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    assert "📦 Rice: 17 bags left" in sold[0].text

    left = await say(db, "How many bags rice remain?")
    assert left[0].text == "📦 Rice: 17 bags left"

    sale_id = sold[0].buttons[2].id.split(":")[1]
    await tap(db, f"undo:{sale_id}")
    assert ledger.find_stock_item(db, trader(db), "rice").quantity == 20  # undo puts the bags back


async def test_fixing_a_sale_quantity_corrects_stock(db):
    await onboard(db)
    await say(db, "I get 10 cartons indomie for shop")
    sold = await say(db, "Tunde collect 2 cartons indomie, he go pay 12k Friday")
    sale_id = sold[0].buttons[1].id.split(":")[1]
    await tap(db, f"fix:{sale_id}")
    fixed = await say(db, "no, na 4 cartons, 24k")
    assert "📦 Indomie: 6 cartons left" in fixed[0].text


async def test_counting_and_removing_stock(db):
    await onboard(db)
    await say(db, "I get 20 bags rice for shop")
    counted = await say(db, "17 bags rice remain")
    assert "Stock updated" in counted[0].text and "17 bags left" in counted[0].text
    await say(db, "2 bags rice spoil")
    stock = await say(db, "Show my stock")
    assert "Rice — 15 bags" in stock[0].text


def test_sales_of_untracked_items_or_other_units_leave_stock_alone(db):
    tr = ledger.get_or_create_trader(db, WA)[0]
    ledger.change_stock(db, tr, "rice", 20, unit="bags")
    ledger.record_sale(db, tr, amount_kobo=3_000_00, paid=True, item_description="rice", quantity=2, unit="paint")
    ledger.record_sale(db, tr, amount_kobo=5_000_00, paid=True, item_description="beans", quantity=1, unit="bag")
    assert ledger.find_stock_item(db, tr, "rice").quantity == 20
    assert ledger.find_stock_item(db, tr, "beans") is None


def test_stock_names_match_plurals_and_partial_names(db):
    tr = ledger.get_or_create_trader(db, WA)[0]
    ledger.change_stock(db, tr, "Indomie noodles", 10, unit="cartons")
    ledger.record_sale(db, tr, amount_kobo=6_000_00, paid=True, item_description="indomie", quantity=1, unit="carton")
    assert ledger.find_stock_item(db, tr, "indomie noodle").quantity == 9


async def test_running_out_is_flagged(db):
    await onboard(db)
    await say(db, "I get 2 crates eggs for shop")
    sold = await say(db, "Sold 2 crates eggs 6k cash")
    assert "⚠️ Eggs has run out" in sold[0].text


# ---------- savings -------------------------------------------------------------------------


async def test_savings_and_payouts(db):
    await onboard(db)
    first = await say(db, "I put 5k for ajo today")
    assert "Savings recorded" in first[0].text and "Ajo: ₦5,000 saved so far." in first[0].text
    await say(db, "I put 5k for ajo")
    await say(db, "I save 20k")
    out = await say(db, "I collect my ajo 8k")
    assert "Savings payout recorded" in out[0].text and "Ajo: ₦2,000 saved so far." in out[0].text

    savings = await say(db, "How much I don save?")
    text = savings[0].text
    assert "Savings: put in ₦20,000" in text and "Ajo: put in ₦10,000, collected ₦8,000, still in ₦2,000" in text
    assert "Total still saved: ₦22,000" in text
    summary = await say(db, "How much I sell today?")
    assert "Saved: ₦30,000" in summary[0].text


async def test_collecting_esusu_before_paying_it_all_in(db):
    await onboard(db)
    out = await say(db, "I collect my esusu money 50k")
    assert "Esusu: you've collected ₦50,000 more than you've put in." in out[0].text
    await say(db, "I put 5k for ajo")
    savings = await say(db, "How much I don save?")
    assert "₦50,000 more than you put in" in savings[0].text and "Total still saved: ₦5,000" in savings[0].text


# ---------- the rest of the app knows the new records ---------------------------------------


def test_business_record_and_ledger_page_with_new_records(db):
    tr = ledger.get_or_create_trader(db, WA)[0]
    ledger.record_purchase(db, tr, amount_kobo=150_000_00, paid_now_kobo=50_000_00, supplier_name="Alhaji",
                           item_description="indomie", quantity=20, unit="cartons")
    ledger.record_saving(db, tr, amount_kobo=5_000_00, group="ajo")
    ledger.record_sale(db, tr, amount_kobo=12_000_00, paid=True, item_description="indomie", quantity=2, unit="cartons")

    rec = business_record.build_record(db, tr, 30, today_local())
    assert rec.purchases == 150_000_00 and rec.owed_to_suppliers == 100_000_00 and rec.savings == 5_000_00
    assert rec.total_sales == 12_000_00  # purchases and savings are not sales
    assert business_record.render_pdf(rec, datetime.now()).startswith(b"%PDF")

    html = ledger_page.render_page(db, tr)
    assert "Bought 20 cartons indomie from Alhaji, owing ₦100,000" in html and "Saved: ajo" in html
