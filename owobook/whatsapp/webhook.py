"""WhatsApp Cloud API webhooks: signature check and turning Meta's payload into simple messages.

Payload shape (Meta docs, Graph API v26.0):
    {"object": "whatsapp_business_account",
     "entry": [{"changes": [{"field": "messages", "value": {
         "contacts": [{"profile": {"name": "..."}, "wa_id": "234..."}],
         "messages": [{"from": "234...", "id": "wamid...", "timestamp": "...", "type": "text",
                       "text": {"body": "..."}}],
         "statuses": [...]}}]}]}
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Types we act on. Everything else gets the friendly "voice note or text for now" reply.
SUPPORTED_TYPES = {"text", "audio", "image", "button"}


@dataclass
class InboundMessage:
    wa_id: str  # the trader's WhatsApp number
    message_id: str  # wamid..., used for dedupe
    type: str  # text | audio | image | button | unsupported
    text: str | None = None  # text body, image caption
    media_id: str | None = None  # audio / image
    mime_type: str | None = None
    button_id: str | None = None  # our reply-button id, e.g. "confirm:12"
    profile_name: str | None = None
    original_type: str | None = None  # Meta's type, for logs ("sticker", "location", ...)


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Check X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(raw body, app secret)."""
    if not header or not header.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _parse_one(msg: dict[str, Any], names: dict[str, str]) -> InboundMessage | None:
    wa_id, message_id, mtype = msg.get("from"), msg.get("id"), msg.get("type")
    if not wa_id or not message_id:
        return None
    base = dict(wa_id=wa_id, message_id=message_id, profile_name=names.get(wa_id), original_type=mtype)

    if mtype == "text":
        return InboundMessage(type="text", text=(msg.get("text") or {}).get("body", ""), **base)
    if mtype == "audio":
        audio = msg.get("audio") or {}
        return InboundMessage(type="audio", media_id=audio.get("id"), mime_type=audio.get("mime_type"), **base)
    if mtype == "image":
        image = msg.get("image") or {}
        return InboundMessage(
            type="image", media_id=image.get("id"), mime_type=image.get("mime_type"), text=image.get("caption"), **base
        )
    if mtype == "interactive":
        interactive = msg.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        if reply and reply.get("id"):
            return InboundMessage(type="button", button_id=reply["id"], text=reply.get("title"), **base)
    if mtype == "button":  # quick-reply button on a template message
        button = msg.get("button") or {}
        if button.get("payload"):
            return InboundMessage(type="button", button_id=button["payload"], text=button.get("text"), **base)
    return InboundMessage(type="unsupported", **base)


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    """All inbound messages in a webhook payload. Delivery statuses are ignored (logged if failed)."""
    out: list[InboundMessage] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return out
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                parsed = _parse_one(msg, names)
                if parsed:
                    out.append(parsed)
            for status in value.get("statuses") or []:
                if status.get("status") == "failed":
                    log.warning("WhatsApp could not deliver %s: %s", status.get("id"), status.get("errors"))
    return out
