"""Receipt / debt-book photos → text, with Muse Spark vision.

The photo is read into plain lines a trader might say ("Mama Bisi took 3 bags of rice for 45000"),
which then go through the normal agent — so photos get the same tools, receipts and buttons as voice notes.

Docs checked 29 Oct 2026 (dev.meta.ai/docs/image-understanding): Chat Completions takes an
`image_url` content part, `{"url": "data:image/jpeg;base64,..."}`, in user messages only.
JPEG/PNG/GIF/WebP, ≤ 50 MB inline; a ~1280px image costs about 1,300–1,500 input tokens.
"""

import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from owobook.llm.base import LLMProvider, Usage

log = logging.getLogger(__name__)

PHOTO_PROMPT = (Path(__file__).resolve().parent / "prompts" / "photo.md").read_text(encoding="utf-8")
NOT_A_RECEIPT = "NOT_A_RECEIPT"
PHOTO_MARKER = "[photo]"  # first line of the agent message, so the model knows these lines came from a picture
MAX_LINES = 10
_BULLET = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")
SUPPORTED_MIME = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}


@dataclass
class PhotoReading:
    lines: list[str]  # empty = nothing to record in the photo
    usage: Usage
    provider: str
    model: str

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def build_photo_messages(image: bytes, mime_type: str, caption: str | None, trader_name: str | None,
                         shop_name: str | None) -> list[dict]:
    mime = (mime_type or "image/jpeg").split(";")[0].strip().lower()
    if mime not in SUPPORTED_MIME:
        mime = "image/jpeg"
    about = f"The trader is {trader_name or 'unknown'}, shop: {shop_name or 'unknown'}."
    note = f"The trader's caption: \"{caption.strip()}\"" if caption and caption.strip() else "No caption."
    data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    return [
        {"role": "system", "content": PHOTO_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": f"{about}\n{note}"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]


def parse_reading(content: str | None) -> list[str]:
    """Model output → clean entry lines (no bullets/blank lines); [] if it's not a receipt."""
    text = (content or "").strip()
    if not text or NOT_A_RECEIPT in text:
        return []
    lines = []
    for raw in text.splitlines():
        line = _BULLET.sub("", raw).strip().strip("`").strip()  # "- x", "• x", "1. x", "2) x"
        if line:
            lines.append(line)
    return lines[:MAX_LINES]


async def read_photo(provider: LLMProvider, image: bytes, mime_type: str, *, caption: str | None = None,
                     trader_name: str | None = None, shop_name: str | None = None) -> PhotoReading:
    """One Muse Spark vision call (no tools). Raises LLMError if the provider fails."""
    messages = build_photo_messages(image, mime_type, caption, trader_name, shop_name)
    resp = await provider.chat(messages)
    lines = parse_reading(resp.content)
    log.info("photo read into %d line(s)", len(lines))
    return PhotoReading(lines=lines, usage=resp.usage, provider=resp.provider or provider.name,
                        model=resp.model or provider.model)
