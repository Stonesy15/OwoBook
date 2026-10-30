import io
import json

import httpx
import pytest
import respx
from PIL import Image

from owobook import ledger
from owobook.llm.base import LLMResponse, ToolCall
from owobook.llm.fake import FakeProvider
from owobook.models import EntryType
from owobook.receipt import build_receipt, render_png
from owobook.whatsapp.client import WhatsAppClient
from owobook.whatsapp.formatting import Reply
from owobook.whatsapp.i18n import LANGUAGES

from tests.test_agent import WA, call, onboard, say
from tests.test_webhook import GRAPH, TRADER, settings

PART_PAID = {"customer_name": "Mama Bisi", "item_description": "rice", "quantity": 3, "unit": "bags",
             "amount": 45000, "paid": False, "amount_paid": 20000, "due_date": "2026-11-06"}


def png(reply: Reply) -> Image.Image:
    assert reply.image and reply.image.startswith(b"\x89PNG")
    return Image.open(io.BytesIO(reply.image))


async def trader(db):
    await onboard(db)
    return ledger.get_or_create_trader(db, WA)[0]


async def test_part_paid_sale_receipt_shows_paid_balance_and_due_date(db):
    tr = await trader(db)
    replies = await say(db, "Mama Bisi collect 3 bags rice 45k, she pay 20k, balance on 6 Nov",
                        FakeProvider(script=[call("record_sale", **PART_PAID)]))
    assert "receipt" in replies[0].text.lower()  # the hint on part-paid sale receipts

    replies = await say(db, "send me receipt for Mama Bisi")
    assert len(replies) == 1 and "Mama Bisi" in replies[0].text
    img = png(replies[0])
    assert img.mode == "RGB" and img.width == 800

    entry = ledger.latest_receipt_entry(db, tr)
    info = build_receipt(db, tr, entry)
    assert info.kind == "sale" and info.customer == "Mama Bisi"
    assert info.rows == [("rc_item", "3 bags rice"), ("rc_total", "₦45,000"), ("rc_paid", "₦20,000")]
    assert info.balance_kobo == 25_000_00 and info.due.isoformat() == "2026-11-06"
    assert info.all_owing_kobo is None  # she owes nothing else


async def test_payment_receipt_shows_before_paid_and_what_is_left(db):
    tr = await trader(db)
    await say(db, "x", FakeProvider(script=[call("record_sale", **PART_PAID)]))
    replies = await say(db, "Mama Bisi don pay 10k")
    assert "receipt" in replies[0].text.lower()  # hint on payment receipts

    replies = await say(db, "receipt")  # no name: the latest sale or payment
    png(replies[0])
    info = build_receipt(db, tr, ledger.latest_receipt_entry(db, tr))
    assert info.kind == "payment"
    assert info.rows == [("rc_prev", "₦25,000"), ("rc_paid", "₦10,000")]
    assert info.balance_kobo == 15_000_00 and info.due.isoformat() == "2026-11-06"


async def test_paid_in_full_and_earlier_receipts_dont_change(db):
    tr = await trader(db)
    await say(db, "x", FakeProvider(script=[call("record_sale", **PART_PAID)]))
    await say(db, "Mama Bisi don pay 10k")
    first_payment = ledger.latest_receipt_entry(db, tr)
    await say(db, "Mama Bisi don pay 15k")

    cleared = build_receipt(db, tr, ledger.latest_receipt_entry(db, tr))
    assert cleared.balance_kobo == 0 and cleared.due is None  # → PAID IN FULL box
    earlier = build_receipt(db, tr, first_payment)
    assert earlier.balance_kobo == 15_000_00  # as it was then, not what's owed now


async def test_total_balance_shows_when_customer_owes_for_other_things(db):
    tr = await trader(db)
    await say(db, "Mama Bisi collect 2 bags beans 30k, she go pay Friday")
    await say(db, "x", FakeProvider(script=[call("record_sale", **PART_PAID)]))
    info = build_receipt(db, tr, ledger.latest_receipt_entry(db, tr))
    assert info.balance_kobo == 25_000_00 and info.all_owing_kobo == 55_000_00


async def test_cash_sale_receipt_is_paid_in_full(db):
    tr = await trader(db)
    await say(db, "Sold 5 crates of eggs 12k cash")
    replies = await say(db, "receipt")
    png(replies[0])
    info = build_receipt(db, tr, ledger.latest_receipt_entry(db, tr))
    assert info.rows[-2:] == [("rc_total", "₦12,000"), ("rc_paid", "₦12,000")] and info.balance_kobo == 0


async def test_record_and_receipt_in_one_message(db):
    await onboard(db)
    both = LLMResponse(content=None, tool_calls=[
        ToolCall("c1", "record_payment", {"customer_name": "Mama Bisi", "amount": 5000, "new_customer": True}),
        ToolCall("c2", "make_receipt", {"customer_name": "Mama Bisi"}),
    ])
    replies = await say(db, "Mama Bisi pay 5k, send her receipt", FakeProvider(script=[both]))
    assert [bool(r.image) for r in replies] == [False, True]
    assert "Say \"receipt\"" not in replies[0].text and "₦5,000" in replies[0].text  # no hint: picture is right there


async def test_nothing_to_make_a_receipt_for(db):
    tr = await trader(db)
    replies = await say(db, "send me receipt")
    assert replies[0].image is None and "can't find" in replies[0].text
    expense = ledger.record_expense(db, tr, description="transport", amount_kobo=2000_00)
    replies = await say(db, "x", FakeProvider(script=[call("make_receipt", entry_id=expense.id)]))
    assert replies[0].image is None and "can't find" in replies[0].text
    assert expense.type == EntryType.EXPENSE


async def test_unknown_customer_gets_a_question_not_a_receipt(db):
    await trader(db)
    replies = await say(db, "x", FakeProvider(script=[call("make_receipt", customer_name="Nobody Here"), LLMResponse(content="")]))
    assert all(r.image is None for r in replies)


@pytest.mark.parametrize("lang", LANGUAGES)
async def test_receipt_draws_in_every_language(db, lang):
    tr = await trader(db)
    await say(db, "x", FakeProvider(script=[call("record_sale", **PART_PAID)]))
    info = build_receipt(db, tr, ledger.latest_receipt_entry(db, tr))
    img = Image.open(io.BytesIO(render_png(info, lang)))
    assert img.format == "PNG" and 600 < img.height < 2400


@respx.mock
async def test_client_uploads_then_sends_the_picture():
    upload = respx.post(f"{GRAPH}/555/media").mock(return_value=httpx.Response(200, json={"id": "MEDIA9"}))
    send = respx.post(f"{GRAPH}/555/messages").mock(return_value=httpx.Response(
        200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.IMG"}]}))
    wa = WhatsAppClient(settings())
    await wa.send_reply(TRADER, Reply("🧾 Receipt for Mama Bisi", image=b"\x89PNG-fake"))

    req = upload.calls.last.request
    assert req.headers["authorization"] == "Bearer EAAG-test"
    assert b'name="messaging_product"' in req.content and b"\x89PNG-fake" in req.content
    body = json.loads(send.calls.last.request.content)
    assert body["type"] == "image" and body["image"] == {"id": "MEDIA9", "caption": "🧾 Receipt for Mama Bisi"}
    await wa.aclose()
