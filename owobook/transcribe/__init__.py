"""Speech-to-text. Pick with TRANSCRIBER=meta|fake."""

from dataclasses import dataclass
from typing import Protocol

from owobook.config import Settings, get_settings


@dataclass
class Transcript:
    text: str
    audio_seconds: float
    provider: str
    model: str


class Transcriber(Protocol):
    name: str
    model: str

    async def transcribe(self, audio: bytes, *, keywords: list[str] | None = None,
                         languages: list[str] | None = None) -> Transcript: ...


class TranscriptionError(Exception):
    pass


# Words to bias recognition toward (Pidgin + money talk), for Nigerian traders. Customer names are added per trader.
BASE_KEYWORDS = [
    "naira", "kobo", "k", "thousand", "collect", "carry", "don pay", "go pay", "dey owe", "balance",
    "next tomorrow", "abeg", "wetin", "oga", "Mama", "Iya", "Baba", "Alhaji", "Madam",
    "bag", "bags", "crate", "crates", "carton", "cartons", "paint", "mudu", "derica", "tuber", "tubers",
    "rice", "beans", "garri", "yam", "eggs", "indomie", "palm oil", "groundnut oil", "semovita", "pepper",
]


def get_transcriber(name: str | None = None, settings: Settings | None = None) -> Transcriber:
    settings = settings or get_settings()
    name = name or settings.transcriber
    if name == "fake":
        from owobook.transcribe.fake import FakeTranscriber

        return FakeTranscriber()
    if name == "meta":
        from owobook.transcribe.meta import MetaTranscriber

        return MetaTranscriber(settings)
    raise ValueError(f"Unknown transcriber {name!r}")
