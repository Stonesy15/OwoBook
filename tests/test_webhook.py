import hashlib
import hmac
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from owobook import http as owo_http
from owobook.config import Settings
from owobook.db import Base, make_engine
from owobook.llm.fake import FakeProvider
from owobook.main import Services, create_app
from owobook.models import Entry, MessageLog
from owobook.transcribe.fake import FakeTranscriber
from owobook.whatsapp.client import WhatsAppClient
from owobook.whatsapp.formatting import Button, Reply
from owobook.whatsapp.webhook import parse_webhook, verify_signature

SECRET = "app-secret"
TRADER = "2348011112222"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(owo_http.asyncio, "sleep", instant)


def settings(**kw) -> Settings:
    base = dict(
        _env_file=None, wa_app_secret=SECRET, wa_verify_token="verify-me", wa_access_token="EAAG-test",
        wa_phone_number_id="555", wa_graph_version="v26.0", llm_provider="fake", transcriber="fake",
        enable_scheduler=False, task_token="task-secret",
    )
    return Settings(**{**base, **kw})


class FakeWhatsApp:
    """Records what would be sent instead of calling Meta."""

    def __init__(self, media: bytes = b"ogg-bytes"):
        self.sent: list[tuple[str, Reply]] = []
        self.read: list[str] = []
        self.media = media

    async def send_reply(self, to, reply):
        self.sent.append((to, reply))

    async def mark_read(self, message_id, typing=True):
        self.read.append(message_id)

    async def download_media(self, media_id):
        return self.media, "audio/ogg; codecs=opus"

    async def aclose(self):
        pass


@pytest.fixture
def app_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    wa = FakeWhatsApp()
    services = Services(
        settings=settings(),
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        provider=FakeProvider(),
        transcriber=FakeTranscriber("Mama Bisi collect 3 bags rice, she go pay 45k Friday"),
        whatsapp=wa,
    )
    with TestClient(create_app(services, run_migrations=False)) as client:
        yield client, wa, services
    engine.dispose()


def envelope(*messages, statuses=None) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550001111", "phone_number_id": "555"},
            "contacts": [{"profile": {"name": "Hosea"}, "wa_id": TRADER}],
            "messages": list(messages),
            **({"statuses": statuses} if statuses else {}),
        }}]}],
    }


def text_msg(body, mid="wamid.T1"):
    return {"from": TRADER, "id": mid, "timestamp": "1761650000", "type": "text", "text": {"body": body}}


def audio_msg(mid="wamid.A1"):
    return {"from": TRADER, "id": mid, "timestamp": "1761650000", "type": "audio",
            "audio": {"id": "MEDIA1", "mime_type": "audio/ogg; codecs=opus", "sha256": "x", "voice": True}}


def button_msg(button_id, mid="wamid.B1"):
    return {"from": TRADER, "id": mid, "timestamp": "1761650000", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "Correct ✓"}}}


def post(client, payload, secret=SECRET, signature=None):
    raw = json.dumps(payload).encode()
    sig = signature or "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


# ---------- signature & verification -------------------------------------------------------


def test_verify_signature():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, good, "s3cret")
    assert not verify_signature(body + b" ", good, "s3cret")
    assert not verify_signature(body, good, "other")
    assert not verify_signature(body, None, "s3cret")
    assert not verify_signature(body, good.removeprefix("sha256="), "s3cret")
    assert not verify_signature(body, good, "")


def test_get_webhook_verification(app_env):
    client, _, _ = app_env
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403


def test_bad_signature_is_rejected_and_nothing_processed(app_env):
    client, wa, _ = app_env
    resp = post(client, envelope(text_msg("Who owe me money?")), signature="sha256=deadbeef")
    assert resp.status_code == 401 and wa.sent == []


def test_health(app_env):
    assert app_env[0].get("/health").json() == {"ok": True}


# ---------- parsing ------------------------------------------------------------------------


def test_parse_text_audio_button_and_unsupported():
    sticker = {"from": TRADER, "id": "wamid.S1", "timestamp": "1", "type": "sticker", "sticker": {"id": "9"}}
    template_button = {"from": TRADER, "id": "wamid.TB", "timestamp": "1", "type": "button",
                       "button": {"payload": "confirm:7", "text": "Correct"}}
    msgs = parse_webhook(envelope(text_msg("hi"), audio_msg(), button_msg("undo:5"), sticker, template_button))
    assert [m.type for m in msgs] == ["text", "audio", "button", "unsupported", "button"]
    assert msgs[0].text == "hi" and msgs[0].profile_name == "Hosea" and msgs[0].wa_id == TRADER
    assert msgs[1].media_id == "MEDIA1"
    assert msgs[2].button_id == "undo:5" and msgs[4].button_id == "confirm:7"
    assert msgs[3].original_type == "sticker"


def test_statuses_only_and_junk_payloads_give_no_messages():
    assert parse_webhook(envelope(statuses=[{"id": "wamid.X", "status": "delivered"}])) == []
    assert parse_webhook({"object": "page"}) == []
    assert parse_webhook([]) == []


# ---------- end to end ---------------------------------------------------------------------


def test_text_message_gets_onboarding_and_receipt(app_env):
    client, wa, services = app_env
    resp = post(client, envelope(text_msg("Mama Bisi collect 3 bags rice, she go pay 45k Friday")))
    assert resp.status_code == 200
    assert wa.read == ["wamid.T1"]
    texts = [r.text for _, r in wa.sent]
    assert "Welcome" in texts[0] and "₦45,000" in texts[1]
    assert [b.title for b in wa.sent[1][1].buttons] == ["Correct ✓", "Fix it ✏️", "Undo ↩️"]
    with services.session_factory() as db:  # committed, not just flushed
        assert db.scalar(select(func.count()).select_from(Entry)) == 1


def test_meta_retry_is_deduplicated(app_env):
    client, wa, services = app_env
    payload = envelope(text_msg("Mama Bisi collect 3 bags rice, she go pay 45k Friday"))
    post(client, payload)
    sent_before = len(wa.sent)
    post(client, payload)  # Meta delivers the same message again
    assert len(wa.sent) == sent_before
    with services.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Entry)) == 1
        assert db.scalar(select(func.count()).select_from(MessageLog).where(MessageLog.direction == "in")) == 1


def test_voice_note_is_downloaded_transcribed_and_recorded(app_env):
    client, wa, services = app_env
    post(client, envelope(audio_msg()))
    assert services.transcriber.calls, "transcriber was not called"
    first = wa.sent[0][1].text
    assert first.startswith('🎤 "Mama Bisi collect')
    assert "₦45,000" in wa.sent[-1][1].text

    post(client, envelope(audio_msg()))  # retry: no second transcription (costs money)
    assert len(services.transcriber.calls) == 1


def test_button_tap_confirms_entry(app_env):
    client, wa, services = app_env
    post(client, envelope(text_msg("Mama Bisi collect 3 bags rice, she go pay 45k Friday")))
    confirm_id = wa.sent[-1][1].buttons[0].id
    post(client, envelope(button_msg(confirm_id)))
    assert "Saved" in wa.sent[-1][1].text or "set" in wa.sent[-1][1].text
    with services.session_factory() as db:
        assert db.scalar(select(Entry.status)) == "confirmed"


def test_unsupported_type_gets_friendly_reply(app_env):
    client, wa, _ = app_env
    post(client, envelope(text_msg("hello")))
    sticker = {"from": TRADER, "id": "wamid.S1", "timestamp": "1", "type": "sticker", "sticker": {"id": "9"}}
    post(client, envelope(sticker))
    assert "voice note" in wa.sent[-1][1].text


def test_processing_error_sends_friendly_message(app_env):
    client, wa, services = app_env

    async def boom(media_id):
        raise RuntimeError("media server down")

    wa.download_media = boom
    resp = post(client, envelope(audio_msg()))
    assert resp.status_code == 200
    assert "try again" in wa.sent[-1][1].text.lower()


def test_allow_list_blocks_other_numbers(app_env):
    client, wa, services = app_env
    services.settings = settings(allowed_wa_ids="2348000000000")
    post(client, envelope(text_msg("hello")))
    assert wa.sent == []


def test_allow_list_accepts_plus_and_spaces(app_env):
    """WhatsApp's wa_id has no '+', but people paste numbers the way they write them."""
    client, wa, services = app_env
    services.settings = settings(allowed_wa_ids="+234 801-111-2222 , +2348000000000")
    assert services.settings.allowed_wa_id_set == {TRADER, "2348000000000"}
    post(client, envelope(text_msg("hello")))
    assert wa.sent and wa.sent[0][0] == TRADER


def test_invalid_json_still_gets_200(app_env):
    client, _, _ = app_env
    raw = b"not json"
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sig}).status_code == 200


# ---------- the real client's HTTP calls (mocked) ------------------------------------------

GRAPH = "https://graph.facebook.com/v26.0"


@respx.mock
async def test_client_sends_buttons_and_text():
    route = respx.post(f"{GRAPH}/555/messages").mock(return_value=httpx.Response(
        200, json={"messaging_product": "whatsapp", "contacts": [], "messages": [{"id": "wamid.OUT"}]}
    ))
    wa = WhatsAppClient(settings())
    reply = Reply("✅ Recorded", [Button("confirm:1", "Correct ✓"), Button("fix:1", "Fix it ✏️"), Button("undo:1", "Undo ↩️")])
    await wa.send_reply(TRADER, reply)
    body = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["authorization"] == "Bearer EAAG-test"
    assert body["type"] == "interactive" and body["interactive"]["type"] == "button"
    assert body["interactive"]["body"]["text"] == "✅ Recorded"
    assert [b["reply"]["id"] for b in body["interactive"]["action"]["buttons"]] == ["confirm:1", "fix:1", "undo:1"]

    await wa.send_reply(TRADER, Reply("hello"))
    body = json.loads(route.calls.last.request.content)
    assert body == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": TRADER,
                    "type": "text", "text": {"preview_url": False, "body": "hello"}}

    # long receipt with buttons: text first, then a short button message
    await wa.send_reply(TRADER, Reply("x" * 1500, [Button("confirm:1", "Correct ✓")]))
    last_two = [json.loads(c.request.content)["type"] for c in route.calls[-2:]]
    assert last_two == ["text", "interactive"]
    await wa.aclose()


@respx.mock
async def test_client_downloads_media_in_two_steps():
    respx.get(f"{GRAPH}/MEDIA1").mock(return_value=httpx.Response(200, json={
        "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1", "mime_type": "audio/ogg",
        "file_size": 2048, "id": "MEDIA1", "messaging_product": "whatsapp",
    }))
    download = respx.get("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1").mock(
        return_value=httpx.Response(200, content=b"OggS-audio")
    )
    wa = WhatsAppClient(settings())
    data, mime = await wa.download_media("MEDIA1")
    assert data == b"OggS-audio" and mime == "audio/ogg"
    assert download.calls.last.request.headers["authorization"] == "Bearer EAAG-test"
    await wa.aclose()


@respx.mock
async def test_client_retries_5xx_and_reports_4xx():
    from owobook.whatsapp.client import WhatsAppError

    route = respx.post(f"{GRAPH}/555/messages").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]}),
    ])
    wa = WhatsAppClient(settings())
    assert await wa.send_text(TRADER, "hi") == "wamid.OK" and route.call_count == 2

    respx.post(f"{GRAPH}/555/messages").mock(return_value=httpx.Response(
        400, json={"error": {"message": "Recipient phone number not in allowed list", "code": 131030}}
    ))
    with pytest.raises(WhatsAppError, match="131030"):
        await wa.send_text(TRADER, "hi")
    await wa.aclose()


def test_access_log_hides_verify_token():
    import logging

    from owobook.main import _HideQueryString

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:0", "GET", "/webhook?hub.verify_token=secret&hub.challenge=1", "1.1", 200), None)
    _HideQueryString().filter(record)
    assert "secret" not in record.getMessage() and "/webhook?…" in record.getMessage()
