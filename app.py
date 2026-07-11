#!/usr/bin/env python3
"""
Lecture Transcriber — local PWA server
======================================
A progressive web app that turns any audio or video recording into a
transcript, using ffmpeg + faster-whisper running on YOUR machine
(free, private, offline once models are downloaded).

Run:
    pip install fastapi uvicorn python-multipart faster-whisper
    python app.py
    -> open http://localhost:8000  (installable as a PWA)

Jobs are processed one at a time in a background worker thread. Finished
transcripts are kept in ./data so they survive a server restart.
"""

import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import guide as guide_mod
import transcriber

BASE = Path(__file__).parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
RESULTS = DATA / "results"
JOBS_FILE = DATA / "jobs.json"
CONFIG_FILE = DATA / "config.json"
for d in (DATA, UPLOADS, RESULTS):
    d.mkdir(parents=True, exist_ok=True)


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=1), encoding="utf-8")

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large-v3"}

app = FastAPI(title="Degome")

# ---------------------------------------------------------------- job store
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_work_q: "queue.Queue[str]" = queue.Queue()


def _save_jobs() -> None:
    with _jobs_lock:
        snapshot = {k: {x: y for x, y in v.items()} for k, v in _jobs.items()}
    JOBS_FILE.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")


def _load_jobs() -> None:
    if not JOBS_FILE.exists():
        return
    try:
        stored = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for jid, job in stored.items():
        # anything that was mid-flight when the server stopped is now failed
        if job.get("status") in ("queued", "extracting", "transcribing"):
            job["status"] = "error"
            job["error"] = "Server was stopped while this job was running. Upload it again."
        if job.get("guide_status") == "generating":
            job["guide_status"] = "error"
            job["guide_error"] = "Server was stopped while generating. Try again."
        job.setdefault("guide_status", "none")
        job.setdefault("guide_error", None)
        _jobs[jid] = job


def _update(jid: str, **fields) -> None:
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(fields)


# ---------------------------------------------------------------- worker
def _process(jid: str) -> None:
    with _jobs_lock:
        job = dict(_jobs[jid])
    src = Path(job["upload_path"])
    wav = UPLOADS / f"{jid}.norm.wav"  # distinct name: the upload itself may be a .wav
    try:
        _update(jid, status="extracting", progress=0.0)
        transcriber.extract_audio(src, wav)
        duration = transcriber.media_duration(wav)
        _update(jid, duration=duration)

        _update(jid, status="transcribing")
        started = time.time()

        def on_progress(fraction: float, latest: str) -> None:
            _update(jid, progress=round(fraction, 4), latest=latest)

        plain, timestamped, lang, lang_prob = transcriber.transcribe(
            wav, job["model"], duration, on_progress
        )

        (RESULTS / f"{jid}_plain.txt").write_text(plain, encoding="utf-8")
        header = (
            f"Transcript of: {job['filename']}\n"
            f"Model: {job['model']}   Detected language: {lang} "
            f"({lang_prob:.0%} confidence)\n" + "=" * 60 + "\n\n"
        )
        (RESULTS / f"{jid}_timestamped.txt").write_text(header + timestamped, encoding="utf-8")

        _update(
            jid, status="done", progress=1.0,
            language=lang, seconds_taken=round(time.time() - started),
            chars=len(plain), latest="",
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        _update(jid, status="error", error=str(exc))
    finally:
        for leftover in (src, wav):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        _save_jobs()


def _worker() -> None:
    while True:
        jid = _work_q.get()
        try:
            _process(jid)
        finally:
            _work_q.task_done()


threading.Thread(target=_worker, daemon=True).start()
_load_jobs()


def _generate_guide(jid: str) -> None:
    """Runs in a thread: generate a study guide for a finished job."""
    plain_path = RESULTS / f"{jid}_plain.txt"
    try:
        cfg = _load_config()
        key = cfg.get("anthropic_api_key", "")
        if not key:
            raise RuntimeError("No API key configured. Add one in Settings, or use Copy prompt.")
        transcript = plain_path.read_text(encoding="utf-8")
        md = guide_mod.generate(key, transcript, cfg.get("guide_model", guide_mod.DEFAULT_MODEL))
        (RESULTS / f"{jid}_guide.md").write_text(md, encoding="utf-8")
        _update(jid, guide_status="done", guide_error=None)
    except Exception as exc:  # noqa: BLE001
        _update(jid, guide_status="error", guide_error=str(exc))
    finally:
        _save_jobs()


# ---------------------------------------------------------------- API
@app.post("/api/transcribe")
async def create_job(file: UploadFile = File(...), model: str = Form("small")):
    if model not in ALLOWED_MODELS:
        raise HTTPException(400, f"Unknown model '{model}'.")
    if not transcriber.check_ffmpeg():
        raise HTTPException(500, "ffmpeg is not installed on the server machine.")

    jid = uuid.uuid4().hex[:12]
    suffix = Path(file.filename or "recording").suffix or ".bin"
    dest = UPLOADS / f"{jid}{suffix}"

    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "File is larger than the 2 GB limit.")
            out.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file is empty.")

    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "filename": file.filename or f"recording{suffix}",
            "model": model,
            "status": "queued",
            "progress": 0.0,
            "latest": "",
            "error": None,
            "language": None,
            "duration": 0.0,
            "seconds_taken": None,
            "chars": None,
            "guide_status": "none",
            "guide_error": None,
            "created": time.time(),
            "upload_path": str(dest),
        }
    _save_jobs()
    _work_q.put(jid)
    return {"job_id": jid}


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "upload_path"}


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        return [_public(j) for j in jobs]


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "No such job.")
    return _public(job)


@app.get("/api/jobs/{jid}/text")
def job_text(jid: str, kind: str = "timestamped"):
    if kind not in ("plain", "timestamped"):
        raise HTTPException(400, "kind must be 'plain' or 'timestamped'.")
    path = RESULTS / f"{jid}_{kind}.txt"
    if not path.exists():
        raise HTTPException(404, "Transcript not ready.")
    return JSONResponse({"text": path.read_text(encoding="utf-8")})


@app.get("/api/jobs/{jid}/download")
def job_download(jid: str, kind: str = "timestamped"):
    if kind not in ("plain", "timestamped"):
        raise HTTPException(400, "kind must be 'plain' or 'timestamped'.")
    path = RESULTS / f"{jid}_{kind}.txt"
    if not path.exists():
        raise HTTPException(404, "Transcript not ready.")
    with _jobs_lock:
        job = _jobs.get(jid, {})
    stem = Path(job.get("filename", jid)).stem
    return FileResponse(path, media_type="text/plain",
                        filename=f"{stem}_transcript_{kind}.txt")


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    with _jobs_lock:
        job = _jobs.pop(jid, None)
    if not job:
        raise HTTPException(404, "No such job.")
    for kind in ("plain", "timestamped"):
        (RESULTS / f"{jid}_{kind}.txt").unlink(missing_ok=True)
    (RESULTS / f"{jid}_guide.md").unlink(missing_ok=True)
    Path(job.get("upload_path", "/nonexistent")).unlink(missing_ok=True)
    _save_jobs()
    return {"deleted": jid}


@app.get("/api/settings")
def get_settings():
    cfg = _load_config()
    key = cfg.get("anthropic_api_key", "")
    return {
        "has_api_key": bool(key),
        "key_hint": (key[:10] + "\u2026" + key[-4:]) if len(key) > 18 else ("set" if key else ""),
        "guide_model": cfg.get("guide_model", guide_mod.DEFAULT_MODEL),
    }


@app.post("/api/settings")
async def set_settings(payload: dict):
    cfg = _load_config()
    if "anthropic_api_key" in payload:
        cfg["anthropic_api_key"] = (payload["anthropic_api_key"] or "").strip()
    if payload.get("guide_model"):
        cfg["guide_model"] = payload["guide_model"].strip()
    _save_config(cfg)
    return {"ok": True, "has_api_key": bool(cfg.get("anthropic_api_key"))}


@app.post("/api/jobs/{jid}/guide")
def start_guide(jid: str):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "No such job.")
    if job["status"] != "done":
        raise HTTPException(409, "Transcription isn't finished yet.")
    if job.get("guide_status") == "generating":
        raise HTTPException(409, "A guide is already being generated.")
    if not _load_config().get("anthropic_api_key"):
        raise HTTPException(
            400,
            "No API key configured. Add one in Settings — or use 'Copy prompt' "
            "and paste it into Claude.ai for a free guide.",
        )
    _update(jid, guide_status="generating", guide_error=None)
    threading.Thread(target=_generate_guide, args=(jid,), daemon=True).start()
    return {"started": jid}


@app.get("/api/jobs/{jid}/guide")
def get_guide(jid: str):
    path = RESULTS / f"{jid}_guide.md"
    if not path.exists():
        raise HTTPException(404, "No study guide yet.")
    return JSONResponse({"markdown": path.read_text(encoding="utf-8")})


@app.get("/api/jobs/{jid}/guide/download")
def download_guide(jid: str):
    path = RESULTS / f"{jid}_guide.md"
    if not path.exists():
        raise HTTPException(404, "No study guide yet.")
    with _jobs_lock:
        job = _jobs.get(jid, {})
    stem = Path(job.get("filename", jid)).stem
    return FileResponse(path, media_type="text/markdown",
                        filename=f"{stem}_study_guide.md")


@app.get("/api/jobs/{jid}/guide_prompt")
def guide_prompt(jid: str):
    """Full prompt + transcript, for pasting into Claude.ai (free path)."""
    plain = RESULTS / f"{jid}_plain.txt"
    if not plain.exists():
        raise HTTPException(404, "Transcript not ready.")
    return JSONResponse({"prompt": guide_mod.build_prompt(plain.read_text(encoding="utf-8"))})


# static PWA — mounted last so /api keeps priority
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")


if __name__ == "__main__":
    print("Degome running -> http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
