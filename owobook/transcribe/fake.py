"""FakeTranscriber for tests and offline use: returns a fixed transcript."""

from owobook.transcribe import Transcript


class FakeTranscriber:
    name = "fake"
    model = "fake-asr-1"

    def __init__(self, text: str = "Mama Bisi collect 3 bags rice, she go pay 45k Friday", seconds: float = 4.0):
        self.text = text
        self.seconds = seconds
        self.calls: list[list[str]] = []

    async def transcribe(self, audio: bytes, *, keywords: list[str] | None = None) -> Transcript:
        self.calls.append(keywords or [])
        return Transcript(text=self.text, audio_seconds=self.seconds, provider=self.name, model=self.model)
