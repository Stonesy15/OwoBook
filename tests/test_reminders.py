import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from owobook import ledger, reminders
from owobook.agent.handler import handle_message
from owobook.config import Settings
from owobook.db import Base, make_engine
from owobook.llm.fake import FakeProvider
from owobook.main import Services, create_app, start_scheduler
from owobook.models import MessageLog
from owobook.transcribe.fake import FakeTranscriber

WED = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 7, 0, tzinfo=timezone.utc)  # 08:00 in Lagos
WA = "2348011112222"
K = 100_000  # ₦1,000 in kobo


def settings(**kw) -> Settings:
    base = dict(_env_file=None, llm_provider="fake", transcriber="fake", enable_scheduler=False,
                wa_reminder_template="", task_token="task-secret", wa_app_secret="s")
    return Settings(**{**base, **kw})


class FakeWhatsApp:
    def __init__(self):
        self.sent, self.templates = [], []

    async def send_reply(self, to, reply):
        self.sent.append((to, reply))

    async def send_template(self, to, name, language="en", params=None):
        self.templates.append((to, name, language, params))

    async def mark_read(self, *a, **k):
        pass

    async def aclose(self):
        pass


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def make_trader(factory, *, last_inbound=NOW - timedelta(hours=2), shop="Hosea Provisions"):
    """Mama Bisi due today (part paid), Alhaji Musa overdue, Chidi due later, Blessing cleared."""
    with factory() as db:
        trader, _ = ledger.get_or_create_trader(db, WA)
        trader.onboarded, trader.name, trader.shop_name, trader.last_inbound_at = True, "Hosea", shop, last_inbound
        created = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
        ledger.record_sale(db, trader, customer_name="Mama Bisi", item_description="rice", quantity=3, unit="bags",
                           amount_kobo=45 * K, paid=False, due_date=WED, now=created)
        ledger.record_payment(db, trader, customer_name="Mama Bisi", amount_kobo=10 * K, now=created)
        ledger.record_sale(db, trader, customer_name="Alhaji Musa", item_description="cement", quantity=10, unit="bags",
                           amount_kobo=250 * K, paid=False, due_date=date(2026, 10, 25), now=created)
        ledger.record_sale(db, trader, customer_name="Chidi", amount_kobo=13 * K, paid=False,
                           due_date=date(2026, 10, 31), now=created)
        ledger.record_sale(db, trader, customer_name="Blessing", amount_kobo=15 * K, paid=False,
                           due_date=date(2026, 10, 24), now=created)
        ledger.record_payment(db, trader, customer_name="Blessing", amount_kobo=15 * K, now=created)
        db.commit()


async def run(factory, wa, **kw):
    return await reminders.run_reminders(factory, wa, kw.pop("settings", settings()), today=WED, now=NOW, **kw)


# ---------- who gets reminded --------------------------------------------------------------


def test_debtors_due_only_today_and_overdue(factory):
    make_trader(factory)
    with factory() as db:
        trader, _ = ledger.get_or_create_trader(db, WA)
        due = reminders.debtors_due(db, trader, WED)
    assert [d.customer.display_name for d in due] == ["Mama Bisi", "Alhaji Musa"]  # today first, no Chidi/Blessing
    assert due[0].balance_kobo == 35 * K


def test_customer_draft_text(factory):
    make_trader(factory)
    with factory() as db:
        trader, _ = ledger.get_or_create_trader(db, WA)
        today_d, overdue_d = reminders.debtors_due(db, trader, WED)
        en = reminders.customer_draft(db, trader, today_d, "english", WED)
        pg = reminders.customer_draft(db, trader, overdue_d, "pidgin", WED)
    assert "Good morning Mama Bisi" in en and "Hosea Provisions" in en
    assert "₦35,000 for 3 bags rice is due today" in en
    assert "Alhaji Musa" in pg and "₦250,000 for 10 bags cement" in pg and "don pass since Sun, 25 Oct" in pg


# ---------- sending & the 24-hour window ---------------------------------------------------


async def test_inside_window_sends_heads_up_and_drafts_once(factory):
    make_trader(factory)
    wa = FakeWhatsApp()
    [result] = await run(factory, wa)
    assert result.status == "sent" and result.debtors == 2
    texts = [r.text for _, r in wa.sent]
    assert texts[0].startswith("⏰ 2 customers") and "₦285,000" in texts[0]
    assert len(texts) == 3 and "Mama Bisi" in texts[1] and "Alhaji Musa" in texts[2]

    [again] = await run(factory, wa)  # the job runs twice (restart, manual trigger)
    assert again.status == "already_sent" and len(wa.sent) == 3

    [forced] = await run(factory, wa, force=True)
    assert forced.status == "sent" and len(wa.sent) == 6


async def test_outside_window_uses_template(factory):
    make_trader(factory, last_inbound=NOW - timedelta(hours=30))
    wa = FakeWhatsApp()
    [result] = await run(factory, wa, settings=settings(wa_reminder_template="debt_reminder"))
    assert result.status == "template" and wa.sent == []
    assert wa.templates == [(WA, "debt_reminder", "en", ["Hosea", "2", "₦285,000"])]


async def test_outside_window_without_template_skips(factory):
    make_trader(factory, last_inbound=None)
    wa = FakeWhatsApp()
    [result] = await run(factory, wa)
    assert result.status == "no_template" and wa.sent == [] and wa.templates == []
    with factory() as db:  # not logged, so it would go out once a template is configured
        assert db.scalar(select(func.count()).select_from(MessageLog).where(MessageLog.type == "reminder")) == 0


async def test_dry_run_sends_and_logs_nothing(factory):
    make_trader(factory)
    wa = FakeWhatsApp()
    [result] = await run(factory, wa, dry_run=True)
    assert result.status == "dry_run" and len(result.replies) == 3 and wa.sent == []
    [real] = await run(factory, wa)
    assert real.status == "sent"


async def test_one_heads_up_for_a_single_debtor(factory):
    make_trader(factory)
    wa = FakeWhatsApp()
    [result] = await run(factory, wa, dry_run=True)
    with factory() as db:
        trader, _ = ledger.get_or_create_trader(db, WA)
        ledger.record_payment(db, trader, customer_name="Alhaji Musa", amount_kobo=250 * K)
        db.commit()
    [result] = await run(factory, wa, dry_run=True)
    assert result.replies[0].text == "⏰ Mama Bisi's ₦35,000 is due today. Here's a reminder you can forward 👇"


async def test_many_debtors_are_capped(factory):
    with factory() as db:
        trader, _ = ledger.get_or_create_trader(db, WA)
        trader.onboarded, trader.last_inbound_at = True, NOW
        for i in range(8):
            ledger.record_sale(db, trader, customer_name=f"Customer {chr(65 + i)}", amount_kobo=(i + 1) * K,
                               paid=False, due_date=WED, new_customer=True)
        db.commit()
    [result] = await run(factory, FakeWhatsApp(), dry_run=True)
    assert len(result.replies) == 1 + reminders.MAX_DRAFTS + 1
    assert "3 more" in result.replies[-1].text


async def test_no_debts_due_means_no_message(factory):
    with factory() as db:
        trader, _ = ledger.get_or_create_trader(db, WA)
        trader.onboarded = True
        ledger.record_sale(db, trader, customer_name="Chidi", amount_kobo=K, paid=False, due_date=date(2026, 11, 5))
        db.commit()
    wa = FakeWhatsApp()
    assert await run(factory, wa) == [] and wa.sent == []


async def test_template_quick_reply_shows_the_drafts(factory):
    make_trader(factory)
    with factory() as db:
        replies = await handle_message(db, FakeProvider(), WA, button_id="Show reminders", msg_type="button")
    assert any("Mama Bisi" in r.text for r in replies)


# ---------- triggers -----------------------------------------------------------------------


def test_task_endpoint_needs_token_and_runs_job(factory):
    make_trader(factory)
    wa = FakeWhatsApp()
    services = Services(settings=settings(), session_factory=factory, provider=FakeProvider(),
                        transcriber=FakeTranscriber(), whatsapp=wa)
    with TestClient(create_app(services, run_migrations=False)) as client:
        assert client.post("/tasks/reminders").status_code == 403
        assert client.post("/tasks/reminders", headers={"X-Task-Token": "wrong"}).status_code == 403
        assert client.post("/tasks/reminders", headers={"X-Task-Token": "task-secret"}).status_code == 202
    # the real job uses today's date; whatever it found, it must not have crashed or sent to strangers
    assert all(to == WA for to, _ in wa.sent)


async def test_scheduler_runs_daily_at_8_lagos(factory):
    services = Services(settings=settings(), session_factory=factory, provider=FakeProvider(),
                        transcriber=FakeTranscriber(), whatsapp=FakeWhatsApp())
    scheduler = start_scheduler(services)
    try:
        nxt = scheduler.get_job("daily_reminders").next_run_time
        assert (nxt.hour, nxt.minute) == (8, 0) and str(nxt.tzinfo) == "Africa/Lagos"
    finally:
        scheduler.shutdown(wait=False)


# ---------- demo seed ----------------------------------------------------------------------


def _load_seed_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
    spec = importlib.util.spec_from_file_location("seed_demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_demo_gives_due_overdue_later_and_cleared(db):
    seed_demo = _load_seed_module()
    trader, _ = ledger.get_or_create_trader(db, WA)
    seed_demo.seed(db, trader, WED)
    debtors = {d.customer.display_name: d for d in ledger.list_debtors(db, trader, today=WED)}
    assert debtors["Mama Bisi"].due_today and debtors["Mama Bisi"].balance_kobo == 35 * K
    assert debtors["Alhaji Musa"].overdue and debtors["Iya Tola"].overdue
    assert not debtors["Chidi"].overdue and debtors["Emeka"].balance_kobo == 40 * K  # part-paid 20k of 60k
    assert "Blessing" not in debtors
    assert ledger.sales_summary(db, trader, "today", WED).cash_sales_kobo == 39 * K


async def test_overlapping_triggers_run_reminders_once(factory, monkeypatch):
    """Scheduler and external cron both firing at 08:00 must not send twice."""
    import asyncio

    from owobook import main
    from owobook.main import reminders_job

    async def on_wednesday(*a, **kw):
        return await reminders.run_reminders(*a, today=WED, now=NOW, **kw)

    monkeypatch.setattr(main, "run_reminders", on_wednesday)
    make_trader(factory)
    wa = FakeWhatsApp()
    services = Services(settings=settings(), session_factory=factory, provider=FakeProvider(),
                        transcriber=FakeTranscriber(), whatsapp=wa)
    await asyncio.gather(reminders_job(services), reminders_job(services))
    assert len(wa.sent) == 3  # one heads-up + two drafts, not six
