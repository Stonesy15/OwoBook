"""The private, read-only ledger page: signed links, the WhatsApp tool, and the page itself."""

from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from owobook import ledger, ledger_page
from owobook.dates import utcnow

from tests.test_agent import WA, onboard, say
from tests.test_webhook import app_env, settings  # noqa: F401 (app_env is a fixture)

BASE = "https://owobook.example"


@pytest.fixture(autouse=True)
def link_settings(monkeypatch):
    s = settings(public_base_url=BASE)
    monkeypatch.setattr(ledger_page, "get_settings", lambda: s)
    return s


def test_token_round_trip_and_expiry():
    now = utcnow()
    token = ledger_page.make_token(42, now)
    assert ledger_page.read_token(token, now) == 42
    assert ledger_page.read_token(token, now + timedelta(days=ledger_page.LINK_DAYS + 1)) is None


@pytest.mark.parametrize("bad", ["", "abc", "1.2", "1.2.3.4", "zz.zz.zz"])
def test_garbage_tokens_are_rejected(bad):
    assert ledger_page.read_token(bad) is None


def test_tampered_token_is_rejected():
    tid, exp, sig = ledger_page.make_token(1).split(".")
    assert ledger_page.read_token(f"2.{exp}.{sig}") is None  # someone else's id with my signature


def test_token_signed_with_another_secret_is_rejected(monkeypatch, link_settings):
    token = ledger_page.make_token(1)
    other = settings(public_base_url=BASE, wa_app_secret="different")
    monkeypatch.setattr(ledger_page, "get_settings", lambda: other)
    assert ledger_page.read_token(token) is None


def test_link_secret_takes_priority(monkeypatch):
    a = settings(link_secret="one")
    monkeypatch.setattr(ledger_page, "get_settings", lambda: a)
    token = ledger_page.make_token(1)
    monkeypatch.setattr(ledger_page, "get_settings", lambda: settings(link_secret="two"))
    assert ledger_page.read_token(token) is None


async def test_show_my_ledger_sends_a_private_link(db):
    await onboard(db)
    replies = await say(db, "show my ledger")
    assert f"{BASE}/l/" in replies[0].text and "7 days" in replies[0].text
    token = replies[0].text.split("/l/")[1].split()[0]
    assert ledger_page.read_token(token) == ledger.get_or_create_trader(db, WA)[0].id


async def test_no_link_when_the_server_has_no_public_address(db, monkeypatch):
    monkeypatch.setattr(ledger_page, "get_settings", lambda: settings(public_base_url=""))
    await onboard(db)
    replies = await say(db, "show my ledger")
    assert "isn't available" in replies[0].text and "/l/" not in replies[0].text


async def test_page_shows_what_the_database_says(db):
    await onboard(db)
    await say(db, "Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    await say(db, "Sold 5 crates of eggs 12k cash")
    await say(db, "Mama Bisi don pay 20k")
    trader = ledger.get_or_create_trader(db, WA)[0]
    html = ledger_page.render_page(db, trader)
    assert "Mama Bisi" in html and "₦25,000" in html  # owes 45k − 20k
    assert "₦57,000" in html  # sales this week: 45k credit + 12k cash
    assert "Sold 5 crates eggs (cash)" in html and "Mama Bisi paid" in html
    assert 'name="robots" content="noindex' in html and "http-equiv=\"refresh\"" in html


async def test_page_escapes_names(db):
    await onboard(db)
    trader = ledger.get_or_create_trader(db, WA)[0]
    ledger.record_sale(db, trader, customer_name="<script>x</script>", amount_kobo=1_000_00, paid=False,
                       item_description="rice")
    html = ledger_page.render_page(db, trader)
    assert "<script>x" not in html and "&lt;script&gt;" in html


def test_page_route(app_env):  # noqa: F811
    client, _, services = app_env
    with services.session_factory() as db:
        trader = ledger.get_or_create_trader(db, "2348011112222")[0]
        trader.shop_name = "Hosea Provisions"
        ledger.record_sale(db, trader, customer_name="Tunde", amount_kobo=30_000_00, paid=False,
                           item_description="beans")
        db.commit()
        token = ledger_page.make_token(trader.id)

    res = client.get(f"/l/{token}")
    assert res.status_code == 200 and "Hosea Provisions" in res.text and "₦30,000" in res.text
    assert res.headers["cache-control"] == "no-store" and res.headers["x-robots-tag"] == "noindex"

    bad = client.get("/l/1.zzzz.notasignature")
    assert bad.status_code == 404 and "expired" in bad.text
    unknown = client.get(f"/l/{ledger_page.make_token(999)}")
    assert unknown.status_code == 404
