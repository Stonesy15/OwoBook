"""Audio conversion. WhatsApp voice notes are OGG/Opus; Muse Voice Transcribe wants
mono 16-bit PCM WAV at 16 or 24 kHz. We convert with ffmpeg (system one, or the copy
bundled by the imageio-ffmpeg package)."""

import asyncio
import io
import shutil
import tempfile
import wave
from functools import lru_cache

TARGET_RATE = 24_000  # Muse Voice Transcribe's native rate


class AudioError(Exception):
    pass


@lru_cache
def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on the machine
        raise AudioError("ffmpeg not found. Install it, or `uv add imageio-ffmpeg`.") from exc


def is_target_wav(data: bytes) -> bool:
    """True if the bytes are already mono 16-bit WAV at 16/24 kHz (no conversion needed)."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() in (16_000, 24_000)
    except wave.Error:
        return False


def wav_duration_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / float(w.getframerate())


async def to_wav(data: bytes, timeout: float = 30.0) -> bytes:
    """Convert any audio (OGG/Opus, M4A, MP3, WAV...) to mono 16-bit 24 kHz WAV.

    The input goes through a temp file, not a pipe: M4A/MP4 files (e.g. iPhone Voice Memos)
    often keep their index at the end, which ffmpeg can't reach when reading from a pipe."""
    if is_target_wav(data):
        return data
    with tempfile.NamedTemporaryFile(suffix=".audio") as src:
        src.write(data)
        src.flush()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-i", src.name,
            "-ac", "1", "-ar", str(TARGET_RATE), "-c:a", "pcm_s16le", "-map_metadata", "-1",
            "-f", "wav", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise AudioError("ffmpeg timed out") from exc
    if proc.returncode != 0 or not out:
        raise AudioError(f"ffmpeg failed: {err.decode(errors='replace')[:300]}")
    wav = fix_wav_header(out)
    # ffmpeg can exit 0 on a broken file and write a header with no audio.
    if wav_duration_seconds(wav) < 0.1:
        raise AudioError(f"no audio decoded: {err.decode(errors='replace')[:300] or 'empty output'}")
    return wav


def fix_wav_header(data: bytes) -> bytes:
    """ffmpeg writing WAV to a pipe can't seek back to fill in sizes; rewrite a clean header."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        # Header sizes were left as 0xFFFFFFFF: take everything after the "data" chunk header.
        idx = data.find(b"data")
        if idx < 0:
            raise AudioError("ffmpeg produced no WAV data")
        frames = data[idx + 8:]
        params = None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(params.framerate if params else TARGET_RATE)
        w.writeframes(frames)
    return buf.getvalue()
