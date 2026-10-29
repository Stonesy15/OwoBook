import base64

from sqlalchemy import func, select

from owobook import ledger
from owobook.agent.handler import handle_photo
from owobook.llm.base import LLMError, LLMResponse, ToolCall
from owobook.llm.fake import FakeProvider
from owobook.models import Entry, MessageLog, UsageLog
from owobook.usage import turns
from owobook.vision import MAX_LINES, build_photo_messages, parse_reading

from tests.test_agent import WA, onboard
from tests.test_webhook import TRADER, app_env, envelope, post  # noqa: F401 (fixture)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def reading(text):
    return LLMResponse(content=text)


def calls(*items):
    return LLMResponse(content=None, tool_calls=[ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(items)])


# ---------- reading the model's output ------------------------------------------------------


def test_parse_reading_cleans_lines():
    out = parse_reading("1. Mama Bisi took rice for 45000\n\n- Tunde paid 5000\n• Sold eggs for 12000 cash\n")
    assert out == ["Mama Bisi took rice for 45000", "Tunde paid 5000", "Sold eggs for 12000 cash"]


def test_parse_reading_not_a_receipt_and_cap():
    assert parse_reading("NOT_A_RECEIPT") == []
    assert parse_reading("") == [] and parse_reading(None) == []
    assert len(parse_reading("\n".join(f"Person {i} paid 1000" for i in range(25)))) == MAX_LINES


def test_photo_message_is_openai_vision_shaped():
    messages = build_photo_messages(JPEG, "image/jpeg", "Mama Bisi never pay", "Hosea", "Hosea Provisions")
    parts = messages[-1]["content"]
    assert messages[-1]["role"] == "user"  # images only go in user messages
    assert 'caption: "Mama Bisi never pay"' in parts[0]["text"] and "Hosea Provisions" in parts[0]["text"]
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,") and base64.b64decode(url.split(",", 1)[1]) == JPEG
    weird = build_photo_messages(JPEG, "application/octet-stream", None, None, None)
    assert weird[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


# ---------- photo → ledger -------------------------------------------------------------------


async def test_debt_book_photo_records_every_line(db):
    await onboard(db)
    provider = FakeProvider(script=[
        reading("Mama Bisi took 3 bags of rice for 45000, will pay Friday\nSold 5 crates of eggs for 12000 cash"),
        calls(("record_sale", {"customer_name": "Mama Bisi", "item_description": "rice", "quantity": 3,
                               "unit": "bags", "amount_naira": 45000, "paid": False, "due_date": "Friday"}),
              ("record_sale", {"item_description": "eggs", "quantity": 5, "unit": "crates",
                               "amount_naira": 12000, "paid": True})),
    ])
    replies = await handle_photo(db, provider, WA, JPEG, "image/jpeg", wa_message_id="wamid.P1")

    assert len(replies) == 2
    assert replies[0].text.startswith("🧾 From your photo:\n• Mama Bisi took 3 bags")
    assert "₦45,000" in replies[0].text and replies[0].buttons
    assert "₦12,000" in replies[1].text
    entries = db.scalars(select(Entry)).all()
    assert [e.source for e in entries] == ["image", "image"]
    agent_text = [m for m in provider.calls[1] if m["role"] == "user"][-1]["content"]
    assert agent_text.startswith("[photo]\nMama Bisi took")
    assert db.scalar(select(UsageLog.provider).order_by(UsageLog.id)) == "fake:vision"
    assert db.scalar(select(MessageLog.type).where(MessageLog.wa_message_id == "wamid.P1")) == "image"

    again = await handle_photo(db, provider, WA, JPEG, "image/jpeg", wa_message_id="wamid.P1")
    assert again == []  # Meta retry: no second (paid) vision call


async def test_photo_with_nothing_to_record(db):
    await onboard(db)
    replies = await handle_photo(db, FakeProvider(script=[reading("NOT_A_RECEIPT")]), WA, JPEG, "image/jpeg",
                                 wa_message_id="wamid.P2")
    assert "couldn't find a sale" in replies[0].text
    assert db.scalar(select(func.count()).select_from(Entry)) == 0
    assert db.scalar(select(MessageLog.content).where(MessageLog.wa_message_id == "wamid.P2")).startswith("[photo")


async def test_goods_photo_with_caption_uses_the_caption(db):
    await onboard(db)
    replies = await handle_photo(db, FakeProvider(), WA, JPEG, "image/jpeg",
                                 caption="Tunde collect 2 bags rice, he go pay 30k tomorrow")
    assert "₦30,000" in replies[0].text and "Tunde" in replies[0].text
    assert db.scalar(select(Entry.source)) == "image"


async def test_photo_reply_is_in_the_chosen_language(db):
    await onboard(db)
    trader, _ = ledger.get_or_create_trader(db, WA)
    trader.language_pref = "pidgin"
    provider = FakeProvider(script=[
        reading("Iya Tope took 2 tins of oil for 9000"),
        calls(("record_sale", {"customer_name": "Iya Tope", "amount_naira": 9000, "paid": False})),
    ])
    replies = await handle_photo(db, provider, WA, JPEG, "image/jpeg", caption="abeg write am, she never pay")
    assert replies[0].text.startswith("🧾 Wetin I see for the picture:")


async def test_vision_failure_is_a_friendly_error(db):
    await onboard(db)

    class Broken(FakeProvider):
        async def chat(self, *a, **kw):
            raise LLMError("meta down")

    replies = await handle_photo(db, Broken(), WA, JPEG, "image/jpeg")
    assert "try again" in replies[0].text.lower()
    assert db.scalar(select(func.count()).select_from(Entry)) == 0


async def test_first_message_photo_is_onboarded_and_recorded(db):
    replies = await handle_photo(db, FakeProvider(), WA, JPEG, "image/jpeg",
                                 caption="Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    assert "Welcome to OwoBook" in replies[0].text
    assert any("₦45,000" in r.text for r in replies)


# ---------- webhook & cost -------------------------------------------------------------------


def image_msg(caption=None, mid="wamid.I1"):
    image = {"id": "MEDIA2", "mime_type": "image/jpeg", "sha256": "x", **({"caption": caption} if caption else {})}
    return {"from": TRADER, "id": mid, "timestamp": "1761650000", "type": "image", "image": image}


def test_whatsapp_photo_is_downloaded_read_and_recorded(app_env):  # noqa: F811
    client, wa, services = app_env
    wa.media = JPEG
    post(client, envelope(image_msg("Mama Bisi collect 3 bags rice, she go pay 45k Friday")))
    assert any("₦45,000" in r.text for _, r in wa.sent)
    with services.session_factory() as db:
        assert db.scalar(select(Entry.source)) == "image"


def test_photo_turns_are_costed_separately(db):
    from tests.test_usage import at
    from owobook import ledger

    trader, _ = ledger.get_or_create_trader(db, "2348011112222")
    db.add_all([
        UsageLog(trader_id=trader.id, provider="meta:vision", model="muse-spark-1.3", input_tokens=1600,
                 output_tokens=40, est_cost_usd=0.003, created_at=at(0)),
        MessageLog(trader_id=trader.id, direction="in", type="image", content="[photo]\n...", created_at=at(1)),
        UsageLog(trader_id=trader.id, provider="meta", model="muse-spark-1.3", input_tokens=3000,
                 output_tokens=100, est_cost_usd=0.004, created_at=at(2)),
        MessageLog(trader_id=trader.id, direction="in", type="text", content="who owe me", created_at=at(10)),
        UsageLog(trader_id=trader.id, provider="meta", model="muse-spark-1.3", input_tokens=3000,
                 output_tokens=100, est_cost_usd=0.004, created_at=at(11)),
    ])
    db.flush()
    photo, text = turns(db)
    assert photo.kind == "photo" and photo.llm_calls == 2 and round(photo.usd, 6) == 0.007
    assert text.kind == "text" and text.llm_calls == 1
