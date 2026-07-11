# Degome — lecture transcriber & study guide PWA

Degome (Ewe: *ɖe gɔme* — to bring out the meaning) is a progressive web app that turns **any audio or video recording** into a
transcript. Everything runs on your own machine with ffmpeg + faster-whisper:
free, private, and offline once the models are downloaded.

## What it handles
- **Video:** mp4, mkv, mov, webm, avi ... (audio is extracted automatically)
- **Audio:** mp3, m4a, wav, aac, ogg, opus, flac ...
- **Live mic recording** straight from the browser (32 kbps Opus — about
  14 MB per hour)
- Files up to 2 GB; jobs queue and process one at a time
- Live progress with the latest transcribed line ticking by
- Two outputs per job: **timestamped** and **plain** text (view, copy, download)
- **Study guides:** one click turns a finished transcript into an exam-focused
  markdown study guide (summary, key concepts, formulas & worked examples,
  likely exam material, announcements)
- Finished transcripts survive a server restart (stored in `./data`)

## Study guides — two ways

**Free path (no key needed):** every finished transcript has a
**Copy prompt** button. It copies a carefully engineered prompt *plus the
full transcript* to your clipboard — paste it into Claude.ai and you get the
study guide there. Zero cost.

**One-click path:** open **Settings** (top right), paste an Anthropic API key
(get one at console.anthropic.com), and the **Study guide** button generates
the guide in ~30–90 s using Claude Haiku — a few cents per lecture. The
guide renders in the app and downloads as markdown. The key is stored only
in `data/config.json` on your machine.

## Setup (once)

You already have ffmpeg and faster-whisper from the CLI script. The only new
dependencies are the web server ones:

```
pip install fastapi uvicorn python-multipart
```

(Fresh machine? Also: `pip install faster-whisper` and install ffmpeg —
`winget install ffmpeg` on Windows.)

## Run

```
python app.py
```

Open **http://localhost:8000** — Chrome/Edge will offer to install it as an
app (menu → "Install Lecture Transcriber").

## Using it from your phone

The server listens on your whole network, so on the same Wi-Fi you can open
`http://<your-laptop-ip>:8000` from a phone and upload recordings to be
processed by the laptop. Find the IP with `ipconfig` (Windows) — e.g.
`http://192.168.1.34:8000`.

> Note: browsers only allow **mic recording** on `localhost` or HTTPS, so the
> "Record with mic" button won't work from a phone over plain LAN http —
> file uploads work fine. Record with the phone's voice recorder app and
> upload the file instead.

## Model guide

| Model | Speed on CPU | When to use |
|---|---|---|
| tiny / base | very fast | quick rough drafts |
| **small** (default) | ~15–30 min per lecture-hour | good everyday balance |
| medium | slower | technical terms (crypto, medicine, law) |
| large-v3 | slowest | maximum accuracy when it really matters |

Each model downloads once on first use, then works offline.

## Project layout

```
app.py            FastAPI server: upload API, job queue, static hosting
transcriber.py    ffmpeg normalization + faster-whisper engine
static/           the PWA (HTML, CSS, JS, manifest, service worker, icons)
data/             uploads (temporary) and finished transcripts
```

## Swapping to a cloud transcription API later

All transcription happens in `transcriber.transcribe()`. To move to a hosted
Whisper API (e.g. for a deployed product), replace that one function with an
API call — the server, queue, and UI don't change.
