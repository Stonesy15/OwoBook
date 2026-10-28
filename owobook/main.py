"""OwoBook web server: the WhatsApp webhook.

    uv run uvicorn owobook.main:app --reload --port 8000

GET  /webhook  — Meta's one-time verification (hub.challenge)
POST /webhook  — inbound messages. We check the signature, answer 200 at once, and process
                 each message in the background (Meta retries slow or failed webhooks).
GET  /health   — for uptime checks and the deploy platform
"""

import asyncio
import json
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from owobook.agent.handler import already_seen, handle_message, handle_unsupported, handle_voice_note
from owobook.config import Settings, get_settings, mask_secret
from owobook.llm import get_provider
from owobook.llm.base import LLMProvider
from owobook.transcribe import Transcriber, get_transcriber
from owobook.whatsapp import formatting as fmt
from owobook.whatsapp.client import WhatsAppClient
from owobook.whatsapp.formatting import Reply
from owobook.whatsapp.webhook import InboundMessage, parse_webhook, verify_signature

log = logging.getLogger("owobook")


@dataclass
class Services:
    """Everything the webhook needs. Tests swap in fakes."""

    settings: Settings
    session_factory: Callable[[], Session]
    provider: LLMProvider
    transcriber: Transcriber
    whatsapp: WhatsAppClient
    # One lock per trader: their messages are handled in order, never two at once.
    locks: dict[str, asyncio.Lock] = field(default_factory=lambda: defaultdict(asyncio.Lock))


def default_services(settings: Settings) -> Services:
    from owobook.db import SessionLocal

    return Services(
        settings=settings,
        session_factory=SessionLocal,
        provider=get_provider(settings.llm_provider, settings),
        transcriber=get_transcriber(settings.transcriber),
        whatsapp=WhatsAppClient(settings),
    )


def create_app(services: Services | None = None, run_migrations: bool = True) -> FastAPI:
    settings = services.settings if services else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_migrations:
            from owobook.db import migrate

            migrate()  # first: Alembic's logging config would otherwise override ours
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
        for noisy in ("alembic", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        app.state.services = services or default_services(settings)
        s = app.state.services.settings
        log.info(
            "OwoBook up: env=%s llm=%s (%s) voice=%s wa_phone=%s token=%s graph=%s",
            s.app_env, app.state.services.provider.name, app.state.services.provider.model, s.transcriber,
            s.wa_phone_number_id or "(missing)", mask_secret(s.wa_access_token), s.wa_graph_version,
        )
        if not s.wa_app_secret:
            log.warning("WA_APP_SECRET is not set: webhook signatures are NOT checked (development only)")
        yield
        await app.state.services.whatsapp.aclose()

    app = FastAPI(title="OwoBook", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/webhook")
    async def verify(request: Request) -> Response:
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and settings.wa_verify_token and q.get("hub.verify_token") == settings.wa_verify_token:
            log.info("webhook verified by Meta")
            return PlainTextResponse(q.get("hub.challenge", ""))
        log.warning("webhook verification failed (wrong WA_VERIFY_TOKEN?)")
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks) -> Response:
        services: Services = request.app.state.services
        raw = await request.body()
        secret = services.settings.wa_app_secret
        if secret or services.settings.app_env == "production":
            if not verify_signature(raw, request.headers.get("x-hub-signature-256"), secret):
                log.warning("rejected webhook with bad signature")
                return PlainTextResponse("bad signature", status_code=401)
        try:
            messages = parse_webhook(json.loads(raw))
        except (ValueError, AttributeError):
            log.warning("ignored a webhook body that is not valid JSON")
            return PlainTextResponse("ok")
        for msg in messages:
            background.add_task(process_inbound, services, msg)
        return PlainTextResponse("ok")  # always 200 fast, so Meta doesn't retry

    return app


async def process_inbound(services: Services, msg: InboundMessage) -> None:
    """Handle one inbound WhatsApp message end to end. Never raises."""
    allowed = services.settings.allowed_wa_id_set
    if allowed and msg.wa_id not in allowed:
        log.info("ignored message from %s (not in ALLOWED_WA_IDS)", msg.wa_id)
        return
    wa = services.whatsapp
    async with services.locks[msg.wa_id]:
        try:
            with services.session_factory() as db:
                if already_seen(db, msg.message_id):  # Meta retry: don't pay to transcribe twice
                    log.info("duplicate %s ignored", msg.message_id)
                    return
            await wa.mark_read(msg.message_id)
            audio = None
            if msg.type == "audio" and msg.media_id:
                audio, _ = await wa.download_media(msg.media_id)

            with services.session_factory() as db:
                if msg.type == "text":
                    replies = await handle_message(db, services.provider, msg.wa_id, text=msg.text,
                                                   msg_type="text", wa_message_id=msg.message_id)
                elif msg.type == "button":
                    replies = await handle_message(db, services.provider, msg.wa_id, button_id=msg.button_id,
                                                   msg_type="button", wa_message_id=msg.message_id)
                elif audio is not None:
                    replies = await handle_voice_note(db, services.provider, services.transcriber, msg.wa_id,
                                                      audio, wa_message_id=msg.message_id)
                else:  # image (Phase 6), sticker, location, document...
                    replies = await handle_unsupported(db, msg.wa_id, msg_type=msg.original_type or msg.type,
                                                       wa_message_id=msg.message_id)
                db.commit()  # save the ledger before replying, so a failed send never loses a record
        except Exception:
            log.exception("failed to process %s from %s", msg.message_id, msg.wa_id)
            replies = [Reply(fmt.t("pidgin" if msg.text and fmt.detect_language(msg.text) == "pidgin" else "english",
                                   "error"))]

        for reply in replies:
            try:
                await wa.send_reply(msg.wa_id, reply)
            except Exception:
                log.exception("failed to send a reply to %s", msg.wa_id)
                break


app = create_app()
