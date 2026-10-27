"""Muse Voice Transcribe (Meta Model API) for recorded voice notes.

Docs checked 27 Oct 2026 (dev.meta.ai/docs/speech-to-text):
POST https://api.meta.ai/v1/asr/transcribe, Bearer auth, multipart/form-data with
  - `request`: JSON settings {"model", "mode", "audioEncoding": "WAV", "languageBias", "keywords"}
  - `audio`:   mono 16-bit PCM WAV, 16 or 24 kHz, max 10 minutes / 32 MB
Response JSON: {"sessionId", "transcript", "audioDurationMs", "turns": [...]}
"""

import json
import logging

import httpx

from owobook.config import Settings
from owobook.http import HTTPError, request_with_retries
from owobook.transcribe import BASE_KEYWORDS, Transcript, TranscriptionError
from owobook.transcribe.audio import AudioError, to_wav, wav_duration_seconds

log = logging.getLogger(__name__)
MAX_KEYWORDS = 100


class MetaTranscriber:
    name = "meta"

    def __init__(self, settings: Settings):
        if not settings.model_api_key:
            raise TranscriptionError("MODEL_API_KEY is missing — set it in .env (or use TRANSCRIBER=fake)")
        self.model = settings.meta_transcribe_model
        self.url = settings.meta_api_base.rstrip("/") + "/asr/transcribe"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {settings.model_api_key}", "Accept": "application/json"},
        )

    async def transcribe(self, audio: bytes, *, keywords: list[str] | None = None) -> Transcript:
        try:
            wav = await to_wav(audio)
        except AudioError as exc:
            raise TranscriptionError(str(exc)) from exc

        words = list(dict.fromkeys([*(keywords or []), *BASE_KEYWORDS]))[:MAX_KEYWORDS]
        settings = {
            "model": self.model,
            "mode": "PUSH_TO_TALK",  # one voice note = one turn
            "audioEncoding": "WAV",
            "languageBias": ["English"],  # Nigerian Pidgin isn't a listed language; it's English-based
            "keywords": words,
        }
        files = {
            "request": (None, json.dumps(settings), "application/json"),
            "audio": ("voice.wav", wav, "audio/wav"),
        }
        try:
            resp = await request_with_retries(self._client, "POST", self.url, files=files, label="meta transcribe")
        except HTTPError as exc:
            raise TranscriptionError(str(exc)) from exc

        data = resp.json()
        text = (data.get("transcript") or "").strip()
        if not text and data.get("turns"):
            text = " ".join(t.get("transcript", "") for t in data["turns"]).strip()
        seconds = (data.get("audioDurationMs") or 0) / 1000 or wav_duration_seconds(wav)
        log.info("transcribed %.1fs of audio: %r", seconds, text)
        return Transcript(text=text, audio_seconds=seconds, provider=self.name, model=self.model)
