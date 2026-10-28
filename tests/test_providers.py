import io
import json
import math
import subprocess
import struct
import wave

import httpx
import pytest
import respx
from sqlalchemy import select

from owobook import http as owo_http
from owobook.agent.handler import handle_voice_note
from owobook.config import Settings
from owobook.llm import FakeProvider, get_provider
from owobook.llm.base import LLMError, LLMResponse, ToolCall
from owobook.llm.openai_compat import FallbackProvider, OpenAICompatProvider
from owobook.models import UsageLog
from owobook.transcribe import TranscriptionError
from owobook.transcribe.audio import AudioError, ffmpeg_path, is_target_wav, to_wav, wav_duration_seconds
from owobook.transcribe.fake import FakeTranscriber
from owobook.transcribe.meta import MetaTranscriber
from owobook.usage import estimate_cost

META_URL = "https://api.meta.ai/v1/chat/completions"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(owo_http.asyncio, "sleep", instant)


def settings(**kw) -> Settings:
    base = dict(model_api_key="LLM|test-key", baseten_api_key="", baseten_model="", _env_file=None)
    return Settings(**{**base, **kw})


def completion(message: dict, usage=(100, 20)) -> dict:
    return {
        "id": "x", "object": "chat.completion", "model": "muse-spark-1.3",
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }


def tool_message(name: str, args) -> dict:
    return {"content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)},
    }]}


@respx.mock
async def test_meta_provider_parses_tool_calls_and_usage():
    route = respx.post(META_URL).mock(return_value=httpx.Response(
        200, json=completion(tool_message("record_payment", {"customer_name": "Mama Bisi", "amount_naira": 20000}))
    ))
    provider = get_provider("meta", settings())
    resp = await provider.chat([{"role": "user", "content": "Mama Bisi don pay 20k"}], tools=[{"type": "function"}])

    assert resp.tool_calls[0].name == "record_payment"
    assert resp.tool_calls[0].arguments == {"customer_name": "Mama Bisi", "amount_naira": 20000}
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (100, 20)
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer LLM|test-key"
    body = json.loads(request.content)
    assert body["model"] == "muse-spark-1.3"
    assert body["tool_choice"] == "auto" and body["reasoning_effort"] == "low"


@respx.mock
async def test_retries_429_then_succeeds():
    route = respx.post(META_URL).mock(side_effect=[
        httpx.Response(429, json={"error": "slow down"}),
        httpx.Response(503),
        httpx.Response(200, json=completion({"content": "hello"})),
    ])
    resp = await get_provider("meta", settings()).chat([{"role": "user", "content": "hi"}])
    assert resp.content == "hello" and route.call_count == 3


@respx.mock
async def test_400_is_not_retried():
    route = respx.post(META_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(LLMError, match="HTTP 400"):
        await get_provider("meta", settings()).chat([{"role": "user", "content": "hi"}])
    assert route.call_count == 1


@respx.mock
async def test_invalid_tool_json_becomes_empty_args():
    respx.post(META_URL).mock(return_value=httpx.Response(200, json=completion(tool_message("list_debtors", "{oops"))))
    resp = await get_provider("meta", settings()).chat([{"role": "user", "content": "hi"}])
    assert resp.tool_calls[0].arguments == {}


def test_missing_key_gives_clear_error():
    with pytest.raises(LLMError, match="API key is missing"):
        get_provider("meta", settings(model_api_key=""))


def test_meta_with_baseten_configured_gets_fallback():
    provider = get_provider("meta", settings(baseten_api_key="bt", baseten_model="some/model"))
    assert isinstance(provider, FallbackProvider)
    assert isinstance(get_provider("baseten", settings(baseten_api_key="bt", baseten_model="m")), OpenAICompatProvider)


async def test_fallback_uses_backup_when_primary_fails():
    class Broken:
        name, model = "meta", "muse-spark-1.3"

        async def chat(self, *a, **k):
            raise LLMError("account restricted")

    backup = FakeProvider(script=[LLMResponse(content="from backup")])
    resp = await FallbackProvider(Broken(), backup).chat([{"role": "user", "content": "hi"}])
    assert resp.content == "from backup"


def make_wav(rate=44_100, channels=2, seconds=0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) * channels
            for i in range(int(rate * seconds))
        )
        w.writeframes(frames)
    return buf.getvalue()


async def test_audio_is_converted_to_mono_24k_wav():
    original = make_wav()
    assert not is_target_wav(original)
    converted = await to_wav(original)
    assert is_target_wav(converted)
    assert wav_duration_seconds(converted) == pytest.approx(0.5, abs=0.05)


async def test_m4a_with_index_at_end_is_converted(tmp_path):
    # iPhone Voice Memos write .m4a with the "moov" index at the END, which ffmpeg can't read from a pipe.
    (tmp_path / "in.wav").write_bytes(make_wav(seconds=1.0))
    subprocess.run([ffmpeg_path(), "-loglevel", "error", "-i", str(tmp_path / "in.wav"), "-c:a", "aac",
                    str(tmp_path / "memo.m4a")], check=True)
    data = (tmp_path / "memo.m4a").read_bytes()
    assert data.find(b"moov") > data.find(b"mdat")  # index really is at the end
    assert wav_duration_seconds(await to_wav(data)) == pytest.approx(1.0, abs=0.1)


async def test_undecodable_audio_raises():
    with pytest.raises(AudioError):
        await to_wav(b"not audio at all" * 100)


async def test_target_wav_is_passed_through():
    wav = make_wav(rate=16_000, channels=1)
    assert await to_wav(wav) is wav


@respx.mock
async def test_meta_transcriber_sends_multipart_request():
    route = respx.post("https://api.meta.ai/v1/asr/transcribe").mock(return_value=httpx.Response(
        200, json={"sessionId": "s", "transcript": "Mama Bisi don pay 20k", "audioDurationMs": 2500, "turns": []}
    ))
    transcript = await MetaTranscriber(settings()).transcribe(make_wav(), keywords=["Mama Bisi"])
    assert transcript.text == "Mama Bisi don pay 20k" and transcript.audio_seconds == 2.5

    body = route.calls.last.request.content
    assert b'name="request"' in body and b'name="audio"' in body and b"RIFF" in body
    request_json = json.loads(body.split(b'name="request"')[1].split(b"\r\n\r\n", 1)[1].split(b"\r\n--")[0])
    assert request_json["model"] == "muse-voice-transcribe-1.0"
    assert request_json["audioEncoding"] == "WAV" and request_json["mode"] == "PUSH_TO_TALK"
    assert request_json["keywords"][0] == "Mama Bisi" and "naira" in request_json["keywords"]


@respx.mock
async def test_meta_transcriber_error():
    respx.post("https://api.meta.ai/v1/asr/transcribe").mock(return_value=httpx.Response(400, text="bad wav"))
    with pytest.raises(TranscriptionError):
        await MetaTranscriber(settings()).transcribe(make_wav())


async def test_voice_note_end_to_end_with_fakes(db):
    provider, transcriber = FakeProvider(), FakeTranscriber("Mama Bisi collect 3 bags rice, she go pay 45k Friday")
    replies = await handle_voice_note(db, provider, transcriber, "234801", b"fake-audio")
    # first contact: onboarding (with the transcript shown) + the receipt
    assert replies[0].text.startswith('🎤 "Mama Bisi collect')
    assert "₦45,000" in replies[-1].text
    rows = db.scalars(select(UsageLog)).all()
    assert any(r.provider == "fake" and r.audio_seconds == 4.0 for r in rows)

    # the trader's customer names are used to bias the next transcription
    await handle_voice_note(db, provider, transcriber, "234801", b"fake-audio")
    assert "Mama Bisi" in transcriber.calls[-1]


async def test_first_voice_note_with_spelled_out_amount_is_processed(db):
    transcriber = FakeTranscriber("Mama Bisi collect three bags rice, she go pay forty five thousand naira Friday")
    replies = await handle_voice_note(db, FakeProvider(script=[
        LLMResponse(content=None, tool_calls=[ToolCall("c1", "record_sale", {
            "customer_name": "Mama Bisi", "item_description": "rice", "amount_naira": "forty five thousand naira",
            "paid": False, "due_date": "Friday"})])
    ]), transcriber, "234802", b"a")
    assert "Welcome" in replies[0].text and "₦45,000" in replies[-1].text


async def test_voice_note_dedupe(db):
    transcriber = FakeTranscriber()
    await handle_voice_note(db, FakeProvider(), transcriber, "234801", b"a", wa_message_id="wamid.v1")
    assert await handle_voice_note(db, FakeProvider(), transcriber, "234801", b"a", wa_message_id="wamid.v1") == []
    assert len(transcriber.calls) == 1  # no paying twice for the same voice note


async def test_empty_transcript_asks_to_resend(db):
    replies = await handle_voice_note(db, FakeProvider(), FakeTranscriber(text=""), "234801", b"a")
    assert "again" in replies[0].text


def test_cost_estimates():
    # 2,000 input + 150 output tokens on Muse Spark 1.3
    assert estimate_cost("muse-spark-1.3", 2000, 150) == pytest.approx(2000 / 1e6 * 1.25 + 150 / 1e6 * 4.25)
    # 10.9 seconds of audio is billed as 10 seconds at $0.18/hour
    assert estimate_cost("muse-voice-transcribe-1.0", audio_seconds=10.9) == pytest.approx(10 * 0.18 / 3600)
