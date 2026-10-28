"""WhatsApp Cloud API client: send text / reply buttons / templates, mark read, download media.

Docs (checked 28 Oct 2026, Graph API v26.0):
- POST /{version}/{phone_number_id}/messages  — text (body ≤ 4096), interactive buttons
  (≤ 3 buttons, title ≤ 20 chars, id ≤ 256, body ≤ 1024), template.
- GET /{version}/{media_id} → {"url", "mime_type", "file_size", ...}; GET url with the same token.
  The URL expires after 5 minutes. Audio is capped at 16 MB.
"""

import logging
from typing import Any

import httpx

from owobook.config import Settings, get_settings
from owobook.http import HTTPError, request_with_retries
from owobook.whatsapp.formatting import Reply

log = logging.getLogger(__name__)

MAX_TEXT = 4096
MAX_BUTTON_BODY = 1024
MAX_BUTTON_TITLE = 20
MAX_MEDIA_BYTES = 16 * 1024 * 1024


class WhatsAppError(Exception):
    pass


class WhatsAppClient:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
        s = self.settings
        self.base = f"https://graph.facebook.com/{s.wa_graph_version}"
        self.messages_url = f"{self.base}/{s.wa_phone_number_id}/messages"

    @property
    def _auth(self) -> dict[str, str]:
        if not self.settings.wa_access_token:
            raise WhatsAppError("WA_ACCESS_TOKEN is missing in .env")
        return {"Authorization": f"Bearer {self.settings.wa_access_token}"}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- sending ----------------------------------------------------------------------

    async def _post_message(self, payload: dict[str, Any]) -> str | None:
        """POST to /messages; returns the new message's wamid."""
        body = {"messaging_product": "whatsapp", **payload}
        try:
            resp = await request_with_retries(
                self._http, "POST", self.messages_url, json=body, headers=self._auth, label="whatsapp send"
            )
        except HTTPError as exc:
            raise WhatsAppError(str(exc)) from exc
        messages = resp.json().get("messages") or [{}]
        return messages[0].get("id")

    async def send_text(self, to: str, text: str) -> str | None:
        return await self._post_message({
            "recipient_type": "individual", "to": to, "type": "text",
            "text": {"preview_url": False, "body": text[:MAX_TEXT]},
        })

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str | None:
        """Interactive reply buttons: [(id, title), ...], at most 3."""
        return await self._post_message({
            "recipient_type": "individual", "to": to, "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:MAX_BUTTON_BODY]},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": bid[:256], "title": title[:MAX_BUTTON_TITLE]}}
                    for bid, title in buttons[:3]
                ]},
            },
        })

    async def send_reply(self, to: str, reply: Reply) -> None:
        """Send one OwoBook reply: plain text, or text with reply buttons."""
        if not reply.buttons:
            await self.send_text(to, reply.text)
            return
        buttons = [(b.id, b.title) for b in reply.buttons]
        if len(reply.text) <= MAX_BUTTON_BODY:
            await self.send_buttons(to, reply.text, buttons)
        else:  # too long for a button message: text first, then the buttons under it
            await self.send_text(to, reply.text)
            await self.send_buttons(to, "👆", buttons)

    async def send_template(self, to: str, name: str, language: str = "en", params: list[str] | None = None) -> str | None:
        """Pre-approved template (needed outside the 24-hour customer service window)."""
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if params:
            template["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
            ]
        return await self._post_message({"recipient_type": "individual", "to": to, "type": "template",
                                         "template": template})

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        """Blue ticks + "typing…" while we work (voice notes take a few seconds). Best effort."""
        payload: dict[str, Any] = {"status": "read", "message_id": message_id}
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        try:
            await request_with_retries(
                self._http, "POST", self.messages_url, retries=0, label="whatsapp mark read",
                json={"messaging_product": "whatsapp", **payload}, headers=self._auth,
            )
        except (HTTPError, WhatsAppError) as exc:
            log.info("mark_read failed (ignored): %s", exc)

    # ---------- media ------------------------------------------------------------------------

    async def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        """Return (bytes, mime_type) of an inbound voice note / image."""
        try:
            meta = await request_with_retries(
                self._http, "GET", f"{self.base}/{media_id}", headers=self._auth, label="whatsapp media info"
            )
            info = meta.json()
            if int(info.get("file_size") or 0) > MAX_MEDIA_BYTES:
                raise WhatsAppError(f"media {media_id} too large: {info.get('file_size')} bytes")
            data = await request_with_retries(
                self._http, "GET", info["url"], headers=self._auth, label="whatsapp media download"
            )
        except (HTTPError, KeyError, ValueError) as exc:
            raise WhatsAppError(f"could not download media {media_id}: {exc}") from exc
        return data.content, info.get("mime_type")
