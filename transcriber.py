"""
Transcription engine: ffmpeg audio normalization + two backends.

  LOCAL (default): faster-whisper on this machine — free, private, offline.
  CLOUD (automatic when GROQ_API_KEY is set): Groq's hosted Whisper —
    fast (~seconds per chunk), free-tier friendly, for server deployments
    where local inference is impractical.

Both paths accept ANY audio or video file ffmpeg understands and report
progress through a callback.
"""

import os
import shutil
import subprocess
from pathlib import Path

_MODEL_CACHE = {}

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")


def cloud_mode() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def media_duration(path: Path) -> float:
    """Duration in seconds via ffprobe (0.0 if unknown)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def extract_audio(src: Path, dst: Path) -> None:
    """Normalize any audio/video input to 16 kHz mono WAV."""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-loglevel", "error", str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dst.exists():
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(
            "ffmpeg could not read this file. "
            + (detail[-1] if detail else "Is it a valid audio/video file?")
        )


def _get_model(model_size: str):
    from faster_whisper import WhisperModel
    if model_size not in _MODEL_CACHE:
        _MODEL_CACHE[model_size] = WhisperModel(model_size, compute_type="int8")
    return _MODEL_CACHE[model_size]


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _split_audio(wav: Path, chunk_seconds: int = 600) -> list[Path]:
    """Split a WAV into ~10-minute chunks so memory stays bounded on long
    recordings (whole-file VAD on an 80-minute lecture needs >1 GB RAM)."""
    pattern = wav.with_name(wav.stem + ".chunk%03d.wav")
    cmd = [
        "ffmpeg", "-y", "-i", str(wav),
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-c", "copy", "-loglevel", "error", str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    chunks = sorted(wav.parent.glob(wav.stem + ".chunk*.wav"))
    if result.returncode != 0 or not chunks:
        # fall back to processing the whole file in one go
        return [wav]
    return chunks


def _to_flac(wav_chunk: Path) -> Path:
    """Compress a WAV chunk to FLAC (lossless, ~5x smaller) to fit
    Groq's 25 MB per-request limit comfortably."""
    flac = wav_chunk.with_suffix(".flac")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_chunk), "-c:a", "flac",
         "-loglevel", "error", str(flac)],
        check=True, capture_output=True,
    )
    return flac


def _transcribe_chunk_groq(chunk: Path):
    """Send one audio chunk to Groq's Whisper API. Returns a list of
    (start, end, text) segments relative to the chunk."""
    import requests

    flac = _to_flac(chunk)
    try:
        with flac.open("rb") as fh:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                files={"file": (flac.name, fh, "audio/flac")},
                data={
                    "model": GROQ_WHISPER_MODEL,
                    "response_format": "verbose_json",
                    "language": "en",
                    "temperature": "0",
                },
                timeout=600,
            )
    finally:
        flac.unlink(missing_ok=True)

    if resp.status_code == 401:
        raise RuntimeError("Groq rejected the API key (check GROQ_API_KEY on the server).")
    if resp.status_code == 429:
        raise RuntimeError("Groq rate limit reached — wait a few minutes and try again.")
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"Groq API error {resp.status_code}: {detail or resp.text[:200]}")

    data = resp.json()
    segs = data.get("segments") or []
    if segs:
        return [(s.get("start", 0.0), s.get("end", 0.0), (s.get("text") or "").strip())
                for s in segs]
    text = (data.get("text") or "").strip()
    return [(0.0, 0.0, text)] if text else []


def transcribe(audio_path: Path, model_size: str, duration: float, on_progress):
    """
    Transcribe in bounded-memory chunks, locally or via Groq.

    on_progress(fraction, latest_line) is called after every segment.
    Returns (plain_text, timestamped_text, language, language_probability).
    """
    use_cloud = cloud_mode()
    model = None if use_cloud else _get_model(model_size)
    chunks = _split_audio(audio_path)

    plain_parts, ts_parts = [], []
    language, lang_prob = ("en", 1.0) if use_cloud else (None, 0.0)
    offset = 0.0

    try:
        for chunk in chunks:
            chunk_dur = media_duration(chunk)

            if use_cloud:
                seg_iter = _transcribe_chunk_groq(chunk)
            else:
                segments, info = model.transcribe(
                    str(chunk), beam_size=5, vad_filter=True, language="en")
                if language is None:
                    language, lang_prob = info.language, float(info.language_probability)
                seg_iter = ((s.start, s.end, s.text.strip()) for s in segments)

            for seg_start, seg_end, text in seg_iter:
                if not text:
                    continue
                start = offset + seg_start
                line = f"[{fmt_time(start)}] {text}"
                ts_parts.append(line)
                plain_parts.append(text)
                fraction = min((offset + seg_end) / duration, 0.999) if duration > 0 else 0.5
                on_progress(fraction, line)

            offset += chunk_dur
    finally:
        for chunk in chunks:
            if chunk != audio_path:
                chunk.unlink(missing_ok=True)

    return (
        " ".join(plain_parts),
        "\n".join(ts_parts),
        language or "en",
        lang_prob,
    )
