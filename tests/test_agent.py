import pytest
from sqlalchemy import func, select

from owobook import ledger
from owobook.agent.handler import handle_message
from owobook.agent.tools import TOOL_SPECS
from owobook.llm.base import LLMResponse, ToolCall
from owobook.llm.fake import FakeProvider
from owobook.models import Entry, EntryStatus, UsageLog

WA = "2348012345678"


def call(name, **args):
    return LLMResponse(content=None, tool_calls=[ToolCall(f"c_{name}", name, args)])


async def onboard(db):
    """Send a first message so the trader is past onboarding."""
    replies = await handle_message(db, FakeProvider(), WA, text="hello")
    assert "Welcome to OwoBook" in replies[0].text


async def say(db, text, provider=None, **kw):
    return await handle_message(db, provider or FakeProvider(), WA, text=text, **kw)


async def tap(db, button_id):
    return await handle_message(db, FakeProvider(), WA, button_id=button_id, msg_type="button")


def test_tool_specs_are_openai_shaped():
    names = {t["function"]["name"] for t in TOOL_SPECS}
    assert {"record_sale", "record_payment", "get_customer_balance", "list_debtors", "get_sales_summary",
            "update_entry", "delete_entry", "ask_clarification"} <= names
    sale = next(t for t in TOOL_SPECS if t["function"]["name"] == "record_sale")
    assert sale["type"] == "function"
    assert "paid" in sale["function"]["parameters"]["required"]


async def test_first_message_gets_onboarding(db):
    await onboard(db)
    replies = await say(db, "hello again")
    assert "Welcome" not in replies[0].text


async def test_first_message_with_a_record_is_not_lost(db):
    replies = await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    assert "Welcome" in replies[0].text
    assert "₦45,000" in replies[1].text


async def test_credit_sale_receipt_with_buttons(db):
    await onboard(db)
    replies = await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    receipt = replies[-1]
    assert "👤 Mama Bisi" in receipt.text
    assert "🛒 3 bags rice" in receipt.text
    assert "₦45,000 — owing" in receipt.text
    assert "📅 Due: Fri," in receipt.text
    assert [b.title for b in receipt.buttons] == ["Correct ✓", "Fix it ✏️", "Undo ↩️"]
    assert all(len(b.title) <= 20 for b in receipt.buttons)


async def test_unit_price_is_multiplied_in_code(db):
    await onboard(db)
    provider = FakeProvider(script=[call(
        "record_sale", customer_name="Chidi", quantity=3, unit="bags", item_description="rice",
        unit_price_naira="15k", paid=False,
    )])
    replies = await say(db, "Chidi collect 3 bags rice at 15k each", provider)
    assert "₦45,000" in replies[0].text
    assert db.scalar(select(Entry.amount_kobo)) == 4_500_000


async def test_receipt_numbers_come_from_db_not_model(db):
    await onboard(db)
    resp = call("record_sale", customer_name="Ada", amount_naira=5000, paid=False)
    resp.content = "Ada now owes you ₦999,999"  # a hallucinating model
    replies = await say(db, "Ada collect 5k", FakeProvider(script=[resp]))
    assert len(replies) == 1
    assert "₦5,000" in replies[0].text and "999" not in replies[0].text


async def test_payment_and_balance_question(db):
    await onboard(db)
    await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    replies = await say(db, "Mama Bisi don pay 20k")
    assert "₦20,000" in replies[0].text and "₦25,000" in replies[0].text
    replies = await say(db, "How much Mama Bisi owe?")
    assert "₦25,000" in replies[0].text


async def test_who_owes_me_and_summary(db):
    await onboard(db)
    await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    await say(db, "Chidi carry 2 cartons indomie at 6500 each, he go pay tomorrow")
    await say(db, "Sold 5 crates of eggs 12k cash")
    debtors = (await say(db, "Who owe me money?"))[0].text
    assert debtors.index("Mama Bisi") < debtors.index("Chidi")
    assert "₦58,000" in debtors  # total
    summary = (await say(db, "How much I sell this week?"))[0].text
    assert "Cash sales: ₦12,000" in summary and "Credit sales: ₦58,000" in summary


async def test_missing_amount_asks_instead_of_guessing(db):
    await onboard(db)
    replies = await say(db, "Tunde collect rice")
    assert replies[0].text == "How much e cost?"
    assert db.scalar(select(func.count(Entry.id))) == 0


async def test_ambiguous_customer_asks(db):
    await onboard(db)
    await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    replies = await say(db, "Iya Bisi collect 1 bag beans 30k")
    assert "Mama Bisi" in replies[0].text and "?" in replies[0].text
    assert db.scalar(select(func.count(Entry.id))) == 1


async def test_buttons_confirm_undo(db):
    await onboard(db)
    receipt = (await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday"))[-1]
    confirm, _, undo = (b.id for b in receipt.buttons)
    await tap(db, confirm)
    assert db.scalar(select(Entry.status)) == EntryStatus.CONFIRMED
    replies = await tap(db, undo)
    assert "↩️" in replies[0].text
    assert db.scalar(select(Entry.status)) == EntryStatus.DELETED
    assert "already" in (await tap(db, undo))[0].text  # second tap is harmless


async def test_fix_it_flow(db):
    await onboard(db)
    receipt = (await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday"))[-1]
    await tap(db, receipt.buttons[1].id)
    trader, _ = ledger.get_or_create_trader(db, WA)
    assert ledger.get_session(db, trader).mode == "editing"

    replies = await say(db, "no, na 4 bags, 60k")
    assert "4 bags rice" in replies[0].text and "₦60,000" in replies[0].text
    assert ledger.get_session(db, trader).mode == "normal"
    assert db.scalar(select(func.count(Entry.id))) == 1  # corrected, not duplicated


async def test_duplicate_message_id_is_ignored(db):
    await onboard(db)
    first = await say(db, "Sold 5 crates of eggs 12k cash", wa_message_id="wamid.1")
    second = await say(db, "Sold 5 crates of eggs 12k cash", wa_message_id="wamid.1")
    assert first and second == []
    assert db.scalar(select(func.count(Entry.id))) == 1


class ExplodingProvider(FakeProvider):
    """Records a sale, fails the next tool, then crashes on the follow-up model call."""

    def __init__(self):
        super().__init__(script=[LLMResponse(content=None, tool_calls=[
            ToolCall("a", "record_sale", {"customer_name": "Ada", "amount_naira": 5000, "paid": False}),
            ToolCall("b", "record_payment", {"customer_name": "Nobody", "amount_naira": 100}),
        ])])

    async def chat(self, messages, tools=None, tool_choice="auto"):
        if messages[-1]["role"] == "tool":
            raise RuntimeError("model API down")
        return await super().chat(messages, tools, tool_choice)


async def test_crash_gives_friendly_error_and_rolls_back(db):
    await onboard(db)
    replies = await say(db, "Ada collect 5k, Nobody pay 100", ExplodingProvider())
    assert "try again" in replies[0].text.lower()
    assert db.scalar(select(func.count(Entry.id))) == 0  # half-done work was rolled back


async def test_usage_is_logged(db):
    await onboard(db)
    await say(db, "Sold 5 crates of eggs 12k cash")
    row = db.scalar(select(UsageLog))
    assert row.provider == "fake" and row.input_tokens > 0


@pytest.mark.parametrize(
    "text, tool, expected",
    [
        ("Mama Bisi collect 3 bags rice, she go pay 45k Friday", "record_sale",
         {"customer_name": "Mama Bisi", "paid": False, "amount_naira": "45k", "due_date": "Friday", "quantity": 3.0}),
        ("Sold 5 crates of eggs 12k cash", "record_sale", {"paid": True, "amount_naira": "12k"}),
        ("Chidi carry 2 cartons indomie at 6500 each, he go pay tomorrow", "record_sale",
         {"customer_name": "Chidi", "unit_price_naira": "6500", "quantity": 2.0}),
        ("Mama Bisi don pay 20k", "record_payment", {"customer_name": "Mama Bisi", "amount_naira": "20k"}),
        ("Who owe me money?", "list_debtors", {"overdue_only": False}),
        ("Who never pay wey don pass due date", "list_debtors", {"overdue_only": True}),
        ("How much I sell this week?", "get_sales_summary", {"period": "this_week"}),
        ("How much I sell today", "get_sales_summary", {"period": "today"}),
        ("How much Mama Bisi owe?", "get_customer_balance", {"customer_name": "mama bisi"}),
        ("I spend 2k for transport", "record_expense", {"amount_naira": "2k"}),
        ("my name is Hosea", "save_profile", {"name": "Hosea"}),
    ],
)
async def test_fake_provider_rules(text, tool, expected):
    resp = await FakeProvider().chat([{"role": "system", "content": "KNOWN_CUSTOMERS: (none yet)"},
                                      {"role": "user", "content": text}])
    assert resp.tool_calls, f"no tool call for {text!r}"
    call_ = resp.tool_calls[0]
    assert call_.name == tool
    for key, value in expected.items():
        assert call_.arguments.get(key) == value, (key, call_.arguments)


# ---------- part-payments ------------------------------------------------------------------


@pytest.mark.parametrize("args, total, paid_now", [
    ({"amount_paid_naira": "60k", "balance_naira": "70k"}, 130_000, 60_000),   # paid + balance
    ({"amount_naira": "50k", "amount_paid_naira": "20k"}, 50_000, 20_000),    # total + paid
    ({"amount_naira": "50k", "balance_naira": "30k"}, 50_000, 20_000),        # total + balance
    ({"quantity": 3, "unit_price_naira": "10k", "amount_paid_naira": "10k"}, 30_000, 10_000),
])
def test_sale_amounts_computed_in_code(args, total, paid_now):
    from owobook.agent.tools import sale_amounts
    got = sale_amounts(args.get("amount_naira"), args.get("unit_price_naira"), args.get("quantity"),
                       args.get("amount_paid_naira"), args.get("balance_naira"))
    assert got == (total * 100, paid_now * 100)


def test_sale_amounts_that_dont_add_up_ask():
    from owobook.agent.tools import ToolInputError, sale_amounts
    with pytest.raises(ToolInputError):
        sale_amounts("100k", None, None, "60k", "70k")


async def test_part_paid_sale_gives_one_receipt(db):
    await onboard(db)
    provider = FakeProvider(script=[call(
        "record_sale", customer_name="Iro", item_description="rice", quantity=3, unit="bags",
        amount_paid_naira="60k", balance_naira="70k", paid=False, due_date="2026-10-30",
    )])
    replies = await say(db, "Iro buy 3 bags rice, e give me 60k, e go balance me 70k Friday", provider)
    [receipt] = replies
    assert "₦130,000 total" in receipt.text
    assert "₦60,000" in receipt.text and "₦70,000 — owing" in receipt.text
    assert len(receipt.buttons) == 3
    assert db.scalar(select(func.count()).select_from(Entry)) == 1


def test_null_strings_from_the_model_are_ignored(db):
    from owobook.agent.tools import AgentContext, execute_tool
    from owobook.dates import today_local
    trader, _ = ledger.get_or_create_trader(db, WA)
    ctx = AgentContext(db=db, trader=trader, lang="english", today=today_local())
    result = execute_tool(ctx, "record_sale", {
        "customer_name": "Chidi", "quantity": 2, "unit_price_naira": 6500, "amount_naira": "null",
        "amount_paid_naira": "null", "balance_naira": "None", "due_date": "null", "paid": False,
    })
    assert not result.is_error, result.data
    assert result.data["amount"] == "₦13,000" and result.data["due_date"] is None

    result = execute_tool(ctx, "record_sale", {"customer_name": "null", "amount_naira": "5k", "paid": False})
    assert result.is_error  # credit sale with no customer → ask who, never a customer called "Null"


async def test_save_customer_phone(db):
    await onboard(db)
    await say(db, "Tutu collect 3 charger 45k, he go pay Saturday")
    [reply] = await say(db, "Tutu number na 0803 123 4567")
    assert "+234 803 123 4567" in reply.text and "Send to Tutu" in reply.text
    trader, _ = ledger.get_or_create_trader(db, WA)
    tutu, _ = ledger.find_customer(db, trader, "Tutu")
    assert tutu.phone == "2348031234567"


async def test_bad_phone_number_asks_again(db):
    await onboard(db)
    replies = await say(db, "Tutu number na 0803 12", provider=FakeProvider(script=[
        LLMResponse(content=None, tool_calls=[ToolCall("c1", "save_customer_phone", {"customer_name": "Tutu", "phone": "0803 12"})]),
        LLMResponse(content="That number no complete. Abeg send am again?"),
    ]))
    assert "no complete" in replies[-1].text


# ---------- reply language --------------------------------------------------------------------


async def test_pidgin_message_gets_an_english_reply_by_default(db):
    await onboard(db)
    replies = await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday", msg_type="audio")
    assert replies[0].text.startswith("✅ Recorded")
    assert "Mama Bisi now owes you ₦45,000" in replies[0].text


@pytest.mark.parametrize("ask, lang, saved, recorded, title", [
    ("reply me in Yoruba", "yoruba", "Màá máa fèsì ní Yorùbá", "✅ Mo ti kọ ọ́ sílẹ̀", "📒 Àwọn tó jẹ ọ́ lówó (1)"),
    ("abeg speak Hausa", "hausa", "Zan riƙa ba ka amsa da Hausa", "✅ An rubuta", "📒 Waɗanda kake bi bashi (1)"),
    ("change language to Igbo", "igbo", "M ga na-aza gị n'Igbo", "✅ Edebanyela m ya", "📒 Ndị ji gị ụgwọ (1)"),
    ("reply me for pidgin", "pidgin", "I go dey reply you for Pidgin", "✅ I don write am", "📒 People wey dey owe you (1)"),
])
async def test_trader_can_switch_reply_language(db, ask, lang, saved, recorded, title):
    await onboard(db)
    replies = await say(db, ask)
    assert saved in replies[0].text
    assert ledger.get_or_create_trader(db, WA)[0].language_pref == lang

    receipt = (await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday"))[0].text
    assert receipt.startswith(recorded) and "₦45,000" in receipt and "Mama Bisi" in receipt
    assert (await say(db, "Who owe me money?"))[0].text.startswith(title)

    back = await say(db, "reply in English")
    assert "I'll reply in English" in back[0].text


async def test_chosen_language_reaches_the_model(db):
    await onboard(db)
    trader, _ = ledger.get_or_create_trader(db, WA)
    trader.language_pref = "igbo"
    provider = FakeProvider()
    await say(db, "Who owe me money?", provider=provider)
    context = provider.calls[0][1]["content"]
    assert "REPLY_LANGUAGE: igbo" in context
