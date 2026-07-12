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
import os
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
import slides as slides_mod
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

# ---------------------------------------------------------------- access gate
# On public deployments, set ACCESS_CODE so only people you share the code
# with can spend the server's API quota. Locally (no env var) it is off.
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")


@app.middleware("http")
async def access_gate(request, call_next):
    if ACCESS_CODE and request.url.path.startswith("/api/"):
        exempt = (request.url.path == "/api/access"
                  or request.url.path.endswith("/download"))
        if not exempt and request.headers.get("x-access-code", "") != ACCESS_CODE:
            from fastapi.responses import JSONResponse as _JR
            return _JR({"detail": "Access code required."}, status_code=401)
    return await call_next(request)


@app.get("/api/access")
def access_info():
    return {"required": bool(ACCESS_CODE)}


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
        job.setdefault("has_transcript", True)
        job.setdefault("has_slides", False)
        job.setdefault("slides_name", None)
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
    """Runs in a thread: generate a study guide for a finished job.

    Backend priority: Ollama if selected -> Anthropic (env key or Settings
    key) -> Groq Llama (cloud deployments with GROQ_API_KEY)."""
    plain_path = RESULTS / f"{jid}_plain.txt"
    try:
        cfg = _load_config()
        transcript = plain_path.read_text(encoding="utf-8") if plain_path.exists() else ""
        slides_path = RESULTS / f"{jid}_slides.txt"
        slides_text = slides_path.read_text(encoding="utf-8") if slides_path.exists() else ""
        backend = cfg.get("guide_backend", "anthropic")
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key", "")

        if backend == "ollama":
            md = guide_mod.generate_ollama(
                guide_mod.build_prompt(transcript, slides_text),
                cfg.get("ollama_model", guide_mod.DEFAULT_OLLAMA_MODEL), prebuilt=True)
        elif anthropic_key:
            md = guide_mod.generate(anthropic_key,
                                    guide_mod.build_prompt(transcript, slides_text),
                                    cfg.get("guide_model", guide_mod.DEFAULT_MODEL), prebuilt=True)
        elif os.environ.get("GROQ_API_KEY"):
            md = guide_mod.generate_groq(guide_mod.build_prompt(transcript, slides_text), prebuilt=True)
        else:
            raise RuntimeError(
                "No guide backend available. Add an API key in Settings, switch "
                "to Ollama (offline), or use Copy prompt.")
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
            "has_transcript": True,
            "has_slides": False,
            "slides_name": None,
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
    (RESULTS / f"{jid}_guide.docx").unlink(missing_ok=True)
    (RESULTS / f"{jid}_slides.txt").unlink(missing_ok=True)
    Path(job.get("upload_path", "/nonexistent")).unlink(missing_ok=True)
    _save_jobs()
    return {"deleted": jid}


@app.get("/api/settings")
def get_settings():
    cfg = _load_config()
    key = cfg.get("anthropic_api_key", "")
    return {
        "cloud_mode": transcriber.cloud_mode(),
        "has_api_key": bool(key),
        "key_hint": (key[:10] + "\u2026" + key[-4:]) if len(key) > 18 else ("set" if key else ""),
        "guide_model": cfg.get("guide_model", guide_mod.DEFAULT_MODEL),
        "guide_backend": cfg.get("guide_backend", "anthropic"),
        "ollama_model": cfg.get("ollama_model", guide_mod.DEFAULT_OLLAMA_MODEL),
    }


@app.post("/api/settings")
async def set_settings(payload: dict):
    cfg = _load_config()
    if "anthropic_api_key" in payload:
        cfg["anthropic_api_key"] = (payload["anthropic_api_key"] or "").strip()
    if payload.get("guide_model"):
        cfg["guide_model"] = payload["guide_model"].strip()
    if payload.get("guide_backend") in ("anthropic", "ollama"):
        cfg["guide_backend"] = payload["guide_backend"]
    if payload.get("ollama_model"):
        cfg["ollama_model"] = payload["ollama_model"].strip()
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
    if not (RESULTS / f"{jid}_plain.txt").exists() and not (RESULTS / f"{jid}_slides.txt").exists():
        raise HTTPException(409, "No transcript or slides to build a guide from.")
    if job.get("guide_status") == "generating":
        raise HTTPException(409, "A guide is already being generated.")
    cfg = _load_config()
    has_backend = (
        cfg.get("guide_backend") == "ollama"
        or os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key")
        or os.environ.get("GROQ_API_KEY")
    )
    if not has_backend:
        raise HTTPException(
            400,
            "No guide backend available. Add an API key in Settings, switch to "
            "Ollama (offline), or use 'Copy prompt' with Claude.ai.",
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
def download_guide(jid: str, fmt: str = "docx"):
    path = RESULTS / f"{jid}_guide.md"
    if not path.exists():
        raise HTTPException(404, "No study guide yet.")
    with _jobs_lock:
        job = _jobs.get(jid, {})
    stem = Path(job.get("filename", jid)).stem

    if fmt == "md":
        return FileResponse(path, media_type="text/markdown",
                            filename=f"{stem}_study_guide.md")
    if fmt != "docx":
        raise HTTPException(400, "fmt must be 'docx' or 'md'.")

    docx_path = RESULTS / f"{jid}_guide.docx"
    md_text = path.read_text(encoding="utf-8")
    # regenerate if missing or stale relative to the markdown
    if not docx_path.exists() or docx_path.stat().st_mtime < path.stat().st_mtime:
        import docgen
        docx_path.write_bytes(docgen.guide_to_docx(md_text, job.get("filename", "Lecture")))
    return FileResponse(
        docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{stem}_study_guide.docx")


@app.get("/api/jobs/{jid}/guide_prompt")
def guide_prompt(jid: str):
    """Full prompt + transcript, for pasting into Claude.ai (free path)."""
    plain = RESULTS / f"{jid}_plain.txt"
    sl = RESULTS / f"{jid}_slides.txt"
    transcript = plain.read_text(encoding="utf-8") if plain.exists() else ""
    slides_text = sl.read_text(encoding="utf-8") if sl.exists() else ""
    if not transcript and not slides_text:
        raise HTTPException(404, "Nothing ready yet.")
    return JSONResponse({"prompt": guide_mod.build_prompt(transcript, slides_text)})




MAX_SLIDES_BYTES = 50 * 1024 * 1024  # 50 MB


async def _save_slides_upload(files: list[UploadFile], jid: str) -> str:
    """Extract text from one or more material files; total 50 MB cap."""
    sections, names, total = [], [], 0
    for file in files:
        suffix = Path(file.filename or "slides").suffix.lower() or ".pdf"
        if suffix not in slides_mod.SUPPORTED:
            raise HTTPException(
                422, f"'{file.filename}': unsupported type. Use PDF, PPTX/PPT, "
                     "DOCX/DOC, XLSX/XLS, or JPG/PNG.")
        tmp = UPLOADS / f"{jid}_slides_{len(names)}{suffix}"
        size = 0
        with tmp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                total += len(chunk)
                if total > MAX_SLIDES_BYTES:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, "Materials exceed the 50 MB total limit.")
                out.write(chunk)
        if size == 0:
            tmp.unlink(missing_ok=True)
            continue
        try:
            text = slides_mod.extract_text(tmp)
        except RuntimeError as exc:
            raise HTTPException(422, f"'{file.filename}': {exc}")
        finally:
            tmp.unlink(missing_ok=True)
        name = file.filename or f"slides{suffix}"
        names.append(name)
        sections.append(f"=== {name} ===\n{text}")
    if not sections:
        raise HTTPException(400, "No readable files were uploaded.")
    dest = RESULTS / f"{jid}_slides.txt"
    combined = "\n\n".join(sections)
    if dest.exists():  # accumulate across multiple uploads to the same job
        combined = dest.read_text(encoding="utf-8") + "\n\n" + combined
    dest.write_text(combined[:slides_mod.MAX_CHARS], encoding="utf-8")
    return ", ".join(names) if len(names) <= 2 else f"{names[0]} +{len(names)-1} more"


@app.post("/api/jobs/{jid}/slides")
async def attach_slides(jid: str, files: list[UploadFile] = File(...)):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "No such job.")
    name = await _save_slides_upload(files, jid)
    # slides change the guide inputs -> invalidate any existing guide
    (RESULTS / f"{jid}_guide.md").unlink(missing_ok=True)
    (RESULTS / f"{jid}_guide.docx").unlink(missing_ok=True)
    prev = job.get("slides_name")
    name = f"{prev}, {name}" if prev else name
    _update(jid, has_slides=True, slides_name=name,
            guide_status="none", guide_error=None)
    _save_jobs()
    return {"attached": jid, "slides_name": name}


@app.post("/api/slides")
async def slides_only_job(files: list[UploadFile] = File(...)):
    """Create a job from materials alone — no recording needed."""
    jid = uuid.uuid4().hex[:12]
    name = await _save_slides_upload(files, jid)
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid, "filename": name, "model": "-",
            "status": "done", "progress": 1.0, "latest": "", "error": None,
            "language": None, "duration": 0.0, "seconds_taken": None,
            "chars": None, "guide_status": "none", "guide_error": None,
            "has_transcript": False, "has_slides": True, "slides_name": name,
            "created": time.time(), "upload_path": "",
        }
    _save_jobs()
    return {"job_id": jid}


# static PWA — mounted last so /api keeps priority
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")


if __name__ == "__main__":
    print("Degome running -> http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
