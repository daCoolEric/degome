/* Degome — on-device transcription (browser mode).
   Decodes any audio/video file with the Web Audio API, resamples to
   16 kHz mono, and hands it to the Whisper worker. Nothing is uploaded. */
"use strict";

(function () {
  const $ = (sel) => document.querySelector(sel);

  const pickBtn = $("#devicePickBtn");
  const fileInput = $("#deviceFileInput");
  const jobBox = $("#deviceJob");
  const fill = $("#deviceFill");
  const ticker = $("#deviceTicker");
  const actions = $("#deviceActions");
  const textPre = $("#deviceText");
  const modelSel = $("#deviceModelSel");

  let worker = null;
  let busy = false;
  let lastPlain = "";
  let lastTimestamped = "";
  let lastFilename = "recording";

  const GUIDE_PROMPT_HEAD = `You are preparing exam-focused study notes for a university lecture.

From the lecture transcript below, produce a well-structured markdown study guide with these sections:

## Session summary
Five bullets capturing what this session covered.

## Key concepts
Each concept the lecturer taught, with a 2-4 sentence plain-language explanation.

## Formulas, definitions & worked examples
Every formula, definition, or worked example - reproduced fully and verified. Show complete solutions step by step.

## Likely exam material
Questions asked in class, points repeated or emphasized.

## Announcements & action items
Assignments, deadlines, readings mentioned.

Rules: the transcript is auto-generated - infer garbled technical terms from context and note corrections in brackets. Ignore small talk. Write "None mentioned." for empty sections.

TRANSCRIPT:
`;

  function getWorker() {
    if (!worker) {
      worker = new Worker("browser-asr.js", { type: "module" });
      worker.onmessage = onWorkerMessage;
      worker.onerror = (e) => fail("Worker failed to start: " + (e.message || "module workers may be unsupported in this browser."));
    }
    return worker;
  }

  function setProgress(fraction, text) {
    if (fraction != null) fill.style.width = (fraction * 100).toFixed(1) + "%";
    if (text != null) ticker.textContent = text;
  }

  function fail(message) {
    busy = false;
    setProgress(0, "");
    ticker.textContent = "\u26a0 " + message;
  }

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec));
    const m = Math.floor(sec / 60), s = sec % 60;
    const h = Math.floor(m / 60);
    return `${String(h).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  async function decodeTo16kMono(file) {
    const bytes = await file.arrayBuffer();
    const probe = new (window.AudioContext || window.webkitAudioContext)();
    let decoded;
    try {
      decoded = await probe.decodeAudioData(bytes);
    } finally {
      probe.close();
    }
    const frames = Math.ceil(decoded.duration * 16000);
    const offline = new OfflineAudioContext(1, frames, 16000);
    const src = offline.createBufferSource();
    src.buffer = decoded;
    src.connect(offline.destination);
    src.start();
    const rendered = await offline.startRendering();
    return { audio: rendered.getChannelData(0), duration: decoded.duration };
  }

  let transcribeStart = 0;
  let elapsedTimer = null;

  function onWorkerMessage(e) {
    const msg = e.data;
    if (msg.type === "download") {
      const name = (msg.file || "model").split("/").pop();
      setProgress(msg.progress * 0.5, `downloading model (once): ${name} \u2014 ${Math.round(msg.progress * 100)}%`);
    } else if (msg.type === "status" && msg.status === "transcribing") {
      transcribeStart = Date.now();
      setProgress(0.5, "transcribing on this device\u2026");
      elapsedTimer = setInterval(() => {
        const s = Math.round((Date.now() - transcribeStart) / 1000);
        setProgress(null, `transcribing on this device\u2026 ${fmt(s)} elapsed`);
      }, 1000);
    } else if (msg.type === "result") {
      clearInterval(elapsedTimer);
      busy = false;
      lastPlain = (msg.text || "").trim();
      lastTimestamped = msg.chunks
        .map((c) => (c.start != null ? `[${fmt(c.start)}] ` : "") + c.text.trim())
        .join("\n");
      setProgress(1, "done \u2014 transcribed entirely on this device");
      textPre.textContent = lastTimestamped || lastPlain;
      textPre.hidden = false;
      actions.hidden = false;
    } else if (msg.type === "error") {
      clearInterval(elapsedTimer);
      fail(msg.message);
    }
  }

  async function transcribeFile(file) {
    if (busy) return;
    busy = true;
    lastFilename = file.name || "recording";
    jobBox.hidden = false;
    actions.hidden = true;
    textPre.hidden = true;
    setProgress(0.02, "decoding audio in this browser\u2026");

    let decoded;
    try {
      decoded = await decodeTo16kMono(file);
    } catch {
      return fail("This browser couldn't decode that file. Try MP3, M4A, WAV, or MP4.");
    }

    if (decoded.duration > 45 * 60) {
      return fail("On-device mode is limited to ~45 minutes (phone memory). Use the laptop server for full lectures.");
    }
    if (decoded.duration > 30 * 60) {
      setProgress(0.04, "long recording \u2014 this may take a while on a phone\u2026");
    }

    const w = getWorker();
    // Transfer the buffer to avoid copying tens of MB
    w.postMessage(
      { type: "transcribe", audio: decoded.audio, model: modelSel.value, language: "english" },
      [decoded.audio.buffer]
    );
    setProgress(0.05, "starting Whisper in this browser\u2026");
  }

  pickBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) transcribeFile(fileInput.files[0]);
    fileInput.value = "";
  });

  $("#deviceCopy").addEventListener("click", async (e) => {
    await navigator.clipboard.writeText(lastPlain);
    flash(e.currentTarget, "Copied");
  });
  $("#devicePrompt").addEventListener("click", async (e) => {
    await navigator.clipboard.writeText(GUIDE_PROMPT_HEAD + lastPlain);
    flash(e.currentTarget, "Copied \u2014 paste into Claude.ai");
  });
  $("#deviceDownload").addEventListener("click", () => {
    const blob = new Blob([lastTimestamped || lastPlain], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = lastFilename.replace(/\.[^.]+$/, "") + "_transcript.txt";
    a.click();
    URL.revokeObjectURL(a.href);
  });

  function flash(btn, msg) {
    const old = btn.textContent;
    btn.textContent = msg;
    setTimeout(() => (btn.textContent = old), 2000);
  }
})();
