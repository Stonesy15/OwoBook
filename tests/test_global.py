"""Global positioning: currency from the phone's country code, auto language, customer trust score,
and the business record PDF for loans."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx

from owobook import business_record, currency, ledger
from owobook.agent.handler import handle_message
from owobook.dates import resolve_due_date
from owobook.language import detect_language
from owobook.llm.base import LLMResponse
from owobook.llm.fake import FakeProvider
from owobook.money import format_money, parse_amount
from owobook.whatsapp.client import WhatsAppClient
from owobook.whatsapp.formatting import Reply

from tests.test_agent import WA, call, onboard, say
from tests.test_webhook import GRAPH, TRADER, settings

KENYA, BRAZIL = "254712345678", "5511987654321"
TODAY = date(2026, 10, 30)


# ---------- currency ------------------------------------------------------------------------


@pytest.mark.parametrize("wa_id, code", [
    ("2348031234567", "NGN"), (KENYA, "KES"), ("919876543210", "INR"), (BRAZIL, "BRL"),
    ("2250712345678", "XOF"), ("237612345678", "XAF"), ("233241234567", "GHS"), ("999123", "XXX"),
])
def test_currency_from_country_code(wa_id, code):
    assert currency.currency_for_wa_id(wa_id) == code


@pytest.mark.parametrize("code, kobo, shown", [
    ("NGN", 45_000_00, "₦45,000"), ("KES", 45_000_00, "KSh 45,000"), ("INR", 45_000_00, "₹45,000"),
    ("BRL", 45_000_50, "R$ 45.000,50"), ("XOF", 45_000_00, "45 000 FCFA"), ("XXX", 45_000_00, "45,000"),
])
def test_money_is_shown_in_the_traders_currency(code, kobo, shown):
    with currency.use_currency(code):
        assert format_money(kobo) == shown


@pytest.mark.parametrize("code, text, kobo", [
    ("KES", "KSh 45,000", 45_000_00), ("KES", "4500 shillings", 4_500_00), ("INR", "₹2,000", 2_000_00),
    ("INR", "1.5 lakh", 150_000_00), ("BRL", "R$ 45.000", 45_000_00), ("BRL", "50 mil reais", 50_000_00),
    ("XOF", "45 000 FCFA", 45_000_00), ("XOF", "45 mille francs", 45_000_00), ("NGN", "2 mil", 2_000_000_00),
])
def test_amounts_in_other_currencies_parse(code, text, kobo):
    with currency.use_currency(code):
        assert parse_amount(text) == kobo


async def test_kenyan_trader_gets_shillings_and_local_phone_numbers(db):
    await handle_message(db, FakeProvider(), KENYA, text="hello")
    replies = await handle_message(db, FakeProvider(script=[call(
        "record_sale", customer_name="Wanjiku", item_description="maize", quantity=3, unit="bags",
        amount="4500", paid=False, due_date="Friday")]), KENYA, text="Wanjiku took 3 bags of maize, 4500 shillings")
    assert "KSh 4,500" in replies[0].text and "₦" not in replies[0].text
    trader, _ = ledger.get_or_create_trader(db, KENYA)
    assert trader.currency == "KES"
    customer = ledger.resolve_customer(db, trader, "Wanjiku", create=False)
    ledger.set_customer_phone(db, trader, customer, "0712 345 678")
    assert customer.phone == "254712345678"


async def test_trader_can_set_their_currency(db):
    await onboard(db)
    replies = await say(db, "Minha moeda é real", FakeProvider(script=[call("save_profile", currency="brl")]))
    assert "BRL (R$)" in replies[0].text
    replies = await say(db, "Sold 5 crates of eggs 1200 cash")
    assert "R$ 1.200" in replies[0].text
    replies = await say(db, "x", FakeProvider(script=[call("save_profile", currency="zzz"), LLMResponse(content="")]))
    assert ledger.get_or_create_trader(db, WA)[0].currency == "BRL"


# ---------- language ------------------------------------------------------------------------


@pytest.mark.parametrize("text, lang", [
    ("A Maria levou 3 sacos de arroz, vai pagar 450 reais na sexta", "portuguese"),
    ("Quem me deve?", "portuguese"),
    ("Awa a pris 3 sacs de riz, elle paiera 45 000 vendredi", "french"),
    ("Ramesh ne 3 bori chawal liya, shukravar ko 4500 dega", "hindi"),
    ("रमेश ने तीन बोरी चावल लिए", "hindi"),
    ("Bonjour", "french"),
    ("Mama Bisi collect 3 bags rice, she go pay 45k Friday", None),  # Pidgin → English replies
    ("Who owe me money?", None),
    ("Iya Tope ra agolo epo meji, o maa san 9000 lola", None),  # Yorùbá is chosen, not guessed
])
def test_detect_language(text, lang):
    assert detect_language(text) == lang


async def test_portuguese_voice_note_gets_portuguese_replies_in_reais(db):
    script = [call("record_sale", customer_name="Maria", item_description="arroz", quantity=3, unit="sacos",
                   amount="450", paid=False, due_date="sexta")]
    replies = await handle_message(db, FakeProvider(script=script), BRAZIL, msg_type="audio",
                                   text="A Maria levou 3 sacos de arroz, vai pagar 450 reais na sexta")
    assert "Bem-vindo ao OwoBook" in replies[0].text
    assert "✅ Registrado" in replies[1].text and "R$ 450" in replies[1].text and "Fri, 6 Nov" in replies[1].text
    trader, _ = ledger.get_or_create_trader(db, BRAZIL)
    assert trader.language_pref == "portuguese" and trader.currency == "BRL"


async def test_a_chosen_language_is_never_overridden(db):
    await onboard(db)
    trader, _ = ledger.get_or_create_trader(db, WA)
    trader.language_pref = "english"
    replies = await say(db, "Quem me deve?", FakeProvider(script=[call("list_debtors")]))
    assert trader.language_pref == "english" and "Nobody owes you" in replies[0].text


async def test_pidgin_still_gets_english(db):
    await onboard(db)
    replies = await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    assert "✅ Recorded" in replies[0].text
    assert ledger.get_or_create_trader(db, WA)[0].language_pref == "auto"


async def test_voice_note_hints_follow_the_trader():
    from owobook.agent.handler import handle_voice_note
    from owobook.transcribe.fake import FakeTranscriber
    from owobook.db import Base, make_engine
    from sqlalchemy.orm import Session

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        asr = FakeTranscriber("A Maria levou arroz")
        await handle_voice_note(db, FakeProvider(), asr, BRAZIL, b"ogg")
        await handle_voice_note(db, FakeProvider(), asr, WA, b"ogg")
    assert asr.languages == [["Portuguese"], ["English"]]
    assert "naira" not in asr.calls[0] and "naira" in asr.calls[1]  # Pidgin/Nigerian words only for +234


@pytest.mark.parametrize("words", ["sexta", "sexta-feira", "vendredi", "shukravar", "शुक्रवार"])
def test_friday_in_other_languages(words):
    assert resolve_due_date(words, TODAY) == date(2026, 11, 6)


@pytest.mark.parametrize("words", ["amanhã", "demain", "kal", "कल"])
def test_tomorrow_in_other_languages(words):
    assert resolve_due_date(words, TODAY) == date(2026, 10, 31)


# ---------- trust score ---------------------------------------------------------------------


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


async def chidi_paid_late_twice(db):
    await onboard(db)
    tr = ledger.get_or_create_trader(db, WA)[0]
    d0 = TODAY - timedelta(days=40)
    for i in range(2):  # due after 5 days, paid after 10
        start = d0 + timedelta(days=15 * i)
        ledger.record_sale(db, tr, amount_kobo=10_000_00, paid=False, customer_name="Chidi", new_customer=True,
                           due_date=start + timedelta(days=5), now=at(start))
        ledger.record_payment(db, tr, customer_name="Chidi", amount_kobo=10_000_00, now=at(start + timedelta(days=10)))
    return tr


async def test_payment_record_counts_on_time_late_and_overdue(db):
    tr = await chidi_paid_late_twice(db)
    chidi = ledger.resolve_customer(db, tr, "Chidi", create=False)
    rec = ledger.payment_record(db, tr, chidi, TODAY)
    assert (rec.on_time, rec.late, rec.overdue_kobo) == (0, 2, 0)

    ledger.record_sale(db, tr, amount_kobo=12_000_00, paid=False, customer_name="Chidi",
                       due_date=TODAY - timedelta(days=3), now=at(TODAY - timedelta(days=8)))
    rec = ledger.payment_record(db, tr, chidi, TODAY)
    assert (rec.late, rec.overdue, rec.overdue_kobo) == (2, 1, 12_000_00)
    assert rec.oldest_overdue == TODAY - timedelta(days=3)


async def test_new_credit_sale_to_a_late_payer_gets_a_heads_up(db):
    await chidi_paid_late_twice(db)
    replies = await say(db, "Chidi collect 2 cartons indomie 13k, he go pay Monday")
    assert "Heads up: Chidi has paid late 2 of 2 times." in replies[0].text


async def test_one_overdue_debt_is_one_warning_not_two(db):
    await onboard(db)
    tr = ledger.get_or_create_trader(db, WA)[0]
    ledger.record_sale(db, tr, amount_kobo=32_000_00, paid=False, customer_name="Iya Tola", new_customer=True,
                       due_date=date.today() - timedelta(days=2), now=at(date.today() - timedelta(days=9)))
    replies = await say(db, "Iya Tola collect 1 bag rice 30k, she go pay Friday")
    assert replies[0].text.count("Heads up") == 1 and "₦32,000 overdue" in replies[0].text


async def test_reliable_customer_gets_a_good_word_and_new_customer_nothing(db):
    await onboard(db)
    tr = ledger.get_or_create_trader(db, WA)[0]
    for i in range(3):
        start = TODAY - timedelta(days=30 - 8 * i)
        ledger.record_sale(db, tr, amount_kobo=5_000_00, paid=False, customer_name="Blessing", new_customer=i == 0,
                           due_date=start + timedelta(days=5), now=at(start))
        ledger.record_payment(db, tr, customer_name="Blessing", amount_kobo=5_000_00, now=at(start + timedelta(days=2)))
    replies = await say(db, "Blessing collect 5 crates eggs 15k, she go pay Friday")
    assert "Blessing has paid on time 3 of 3 times." in replies[0].text
    replies = await say(db, "Tunde collect 2 bags beans 30k, he go pay Friday")
    assert "Heads up" not in replies[0].text and "on time" not in replies[0].text


async def test_balance_question_shows_the_payment_record(db):
    await chidi_paid_late_twice(db)
    ledger.record_sale(db, ledger.get_or_create_trader(db, WA)[0], amount_kobo=12_000_00, paid=False,
                       customer_name="Chidi", due_date=TODAY + timedelta(days=4))
    replies = await say(db, "How much Chidi owe?")
    assert "Chidi owes you ₦12,000" in replies[0].text and "on time 0 of 2 times" in replies[0].text


# ---------- business record PDF -------------------------------------------------------------


async def test_business_record_is_a_pdf_with_the_right_totals(db):
    await onboard(db)
    tr = ledger.get_or_create_trader(db, WA)[0]
    await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    await say(db, "Sold 5 crates of eggs 12k cash")
    await say(db, "Mama Bisi don pay 20k")
    await say(db, "I spend 2k for transport")

    replies = await say(db, "send me my business record for 90 days")
    reply = replies[0]
    assert reply.document.startswith(b"%PDF") and reply.filename.endswith(".pdf")
    assert "last 90 days" in reply.text and reply.image is None

    rec = business_record.build_record(db, tr, 90, date.today())
    assert (rec.cash_sales, rec.credit_sales, rec.collected, rec.expenses) == (12_000_00, 45_000_00, 20_000_00, 2_000_00)
    assert rec.total_sales == 57_000_00 and rec.money_in == 32_000_00 and rec.outstanding == 25_000_00
    assert len(rec.entries) == 4 and rec.active_days == 1


async def test_business_record_with_nothing_recorded(db):
    await onboard(db)
    replies = await say(db, "send me my business record")
    assert replies[0].document is None and "Nothing recorded in the last 30 days" in replies[0].text


async def test_business_record_in_another_currency_renders(db):
    await handle_message(db, FakeProvider(), BRAZIL, text="Olá")
    tr = ledger.get_or_create_trader(db, BRAZIL)[0]
    currency.set_current("BRL")
    ledger.record_sale(db, tr, amount_kobo=450_00, paid=True, item_description="arroz")
    pdf = business_record.render_pdf(business_record.build_record(db, tr, 30, date.today()), datetime.now())
    assert pdf.startswith(b"%PDF") and len(pdf) > 5_000


def test_devanagari_is_spelled_in_latin_for_pictures_and_pdfs():
    from owobook.script import drawable

    assert [drawable(x) for x in ["रमेश", "सुनीता देवी", "3 बोरी चावल", "Mama Bisi"]] == [
        "Ramesh", "Sunita Devi", "3 Bori Chaval", "Mama Bisi"]


@respx.mock
async def test_client_uploads_then_sends_the_pdf():
    upload = respx.post(f"{GRAPH}/555/media").mock(return_value=httpx.Response(200, json={"id": "DOC1"}))
    send = respx.post(f"{GRAPH}/555/messages").mock(return_value=httpx.Response(
        200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.DOC"}]}))
    wa = WhatsAppClient(settings())
    await wa.send_reply(TRADER, Reply("📄 Your business record", document=b"%PDF-fake", filename="record.pdf"))
    assert b"application/pdf" in upload.calls.last.request.content
    body = json.loads(send.calls.last.request.content)
    assert body["type"] == "document"
    assert body["document"] == {"id": "DOC1", "filename": "record.pdf", "caption": "📄 Your business record"}
    await wa.aclose()
