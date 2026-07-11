"""
Transcription engine: ffmpeg audio normalization + faster-whisper.

Accepts ANY audio or video file ffmpeg understands (mp4, mkv, mov, webm,
avi, mp3, m4a, wav, aac, ogg, opus, flac ...) and produces plain +
timestamped transcripts. Reports progress through a callback so the web
UI can show a live timecode ticker.
"""

import shutil
import subprocess
from pathlib import Path

_MODEL_CACHE = {}


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


def transcribe(audio_path: Path, model_size: str, duration: float, on_progress):
    """
    Run faster-whisper over the audio file, in bounded-memory chunks.

    on_progress(fraction, latest_line) is called after every segment.
    Returns (plain_text, timestamped_text, language, language_probability).
    """
    model = _get_model(model_size)
    chunks = _split_audio(audio_path)

    plain_parts, ts_parts = [], []
    language, lang_prob = None, 0.0
    offset = 0.0

    try:
        for chunk in chunks:
            chunk_dur = media_duration(chunk)
            segments, info = model.transcribe(str(chunk), beam_size=5, vad_filter=True)
            if language is None:
                language, lang_prob = info.language, float(info.language_probability)

            for seg in segments:
                text = seg.text.strip()
                if not text:
                    continue
                start = offset + seg.start
                line = f"[{fmt_time(start)}] {text}"
                ts_parts.append(line)
                plain_parts.append(text)
                fraction = min((offset + seg.end) / duration, 0.999) if duration > 0 else 0.5
                on_progress(fraction, line)

            offset += chunk_dur
    finally:
        for chunk in chunks:
            if chunk != audio_path:
                chunk.unlink(missing_ok=True)

    return (
        " ".join(plain_parts),
        "\n".join(ts_parts),
        language or "unknown",
        lang_prob,
    )
