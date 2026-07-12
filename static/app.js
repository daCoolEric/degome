/* Lecture Transcriber — frontend logic */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

const dropzone = $("#dropzone");
const fileInput = $("#fileInput");
const pickBtn = $("#pickBtn");
const recBtn = $("#recBtn");
const recStatus = $("#recStatus");
const recTimer = $("#recTimer");
const modelSel = $("#modelSel");
const jobsEl = $("#jobs");
const jobsLabel = $("#jobsLabel");
const serverNote = $("#serverNote");
const jobTpl = $("#jobTpl");

let accessCode = localStorage.getItem("degome_access") || "";

async function ensureAccess() {
  try {
    const info = await (await fetch("/api/access")).json();
    if (!info.required) return;
    while (true) {
      const probe = await fetch("/api/jobs", { headers: apiHeaders() });
      if (probe.status !== 401) return;
      const code = prompt("Enter the Degome access code:");
      if (code == null) return;
      accessCode = code.trim();
      localStorage.setItem("degome_access", accessCode);
    }
  } catch {}
}

function apiHeaders(extra) {
  const h = extra || {};
  if (accessCode) h["x-access-code"] = accessCode;
  return h;
}

async function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = apiHeaders(opts.headers);
  const res = await fetch(url, opts);
  if (res.status === 401) {
    const code = prompt("Access code required:");
    if (code != null) {
      accessCode = code.trim();
      localStorage.setItem("degome_access", accessCode);
      opts.headers = apiHeaders({});
      return fetch(url, opts);
    }
  }
  return res;
}

const cards = new Map(); // job id -> card element
let pollTimer = null;
let hasApiKey = false;

/* ---------------- settings ---------------- */

const settingsDlg = $("#settingsDlg");
const apiKeyInput = $("#apiKeyInput");
const keyHint = $("#keyHint");
const backendSel = $("#backendSel");
const ollamaModelInput = $("#ollamaModelInput");

function syncBackendFields() {
  const ollama = backendSel.value === "ollama";
  $("#anthropicFields").hidden = ollama;
  $("#ollamaFields").hidden = !ollama;
}
backendSel.addEventListener("change", syncBackendFields);

async function loadSettings() {
  try {
    const s = await (await apiFetch("/api/settings")).json();
    hasApiKey = s.has_api_key;
    keyHint.textContent = s.has_api_key ? "Current key: " + s.key_hint : "No key saved \u2014 Ollama and Copy-prompt paths still work.";
    backendSel.value = s.guide_backend || "anthropic";
    ollamaModelInput.value = s.ollama_model || "llama3.1:8b";
    syncBackendFields();
  } catch {}
}
$("#settingsBtn").addEventListener("click", () => { loadSettings(); settingsDlg.showModal(); });
$("#closeSettings").addEventListener("click", () => settingsDlg.close());
$("#saveSettings").addEventListener("click", async () => {
  const payload = {
    guide_backend: backendSel.value,
    ollama_model: ollamaModelInput.value.trim(),
  };
  const key = apiKeyInput.value.trim();
  if (key) payload.anthropic_api_key = key;
  await apiFetch("/api/settings", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  apiKeyInput.value = "";
  await loadSettings();
  settingsDlg.close();
});
$("#clearKey").addEventListener("click", async () => {
  await apiFetch("/api/settings", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ anthropic_api_key: "" }),
  });
  await loadSettings();
});

/* ---------------- tiny markdown renderer (offline, no CDN) ---------------- */

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, (_, c) => "<code>" + c + "</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}
function renderMarkdown(md) {
  const lines = escapeHtml(md).split(/\r?\n/);
  const out = [];
  let inList = null, inCode = false;
  const closeList = () => { if (inList) { out.push("</" + inList + ">"); inList = null; } };
  for (const raw of lines) {
    if (raw.trim().startsWith("```")) {
      if (inCode) { out.push("</code></pre>"); inCode = false; }
      else { closeList(); out.push("<pre><code>"); inCode = true; }
      continue;
    }
    if (inCode) { out.push(raw); continue; }
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
      closeList();
      const lvl = Math.min(m[1].length + 1, 4); // shift down: # -> h2
      out.push("<h" + lvl + ">" + inlineMd(m[2]) + "</h" + lvl + ">");
    } else if ((m = line.match(/^\s*[-*]\s+(.*)/))) {
      if (inList !== "ul") { closeList(); out.push("<ul>"); inList = "ul"; }
      out.push("<li>" + inlineMd(m[1]) + "</li>");
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (inList !== "ol") { closeList(); out.push("<ol>"); inList = "ol"; }
      out.push("<li>" + inlineMd(m[1]) + "</li>");
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      out.push("<p>" + inlineMd(line) + "</p>");
    }
  }
  closeList();
  if (inCode) out.push("</code></pre>");
  return out.join("\n");
}

/* ---------------- upload ---------------- */

function uploadFile(file) {
  const form = new FormData();
  form.append("file", file, file.name || "recording.webm");
  form.append("model", modelSel.value);

  const xhr = new XMLHttpRequest();
  const card = makeUploadCard(file.name || "recording.webm");

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      setCardProgress(card, e.loaded / e.total, "uploading \u2014 " + Math.round((e.loaded / e.total) * 100) + "%");
    }
  };
  xhr.onload = () => {
    card.remove();
    if (xhr.status >= 200 && xhr.status < 300) {
      refreshJobs();
      startPolling();
    } else {
      let msg = "Upload failed (" + xhr.status + ").";
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      alert(msg);
    }
  };
  xhr.onerror = () => {
    card.remove();
    serverNote.hidden = false;
  };
  xhr.open("POST", "/api/transcribe");
  if (accessCode) xhr.setRequestHeader("x-access-code", accessCode);
  xhr.send(form);
}

function makeUploadCard(name) {
  const card = jobTpl.content.firstElementChild.cloneNode(true);
  $(".job-name", card).textContent = name;
  $(".job-badge", card).textContent = "uploading";
  $(".job-badge", card).classList.add("st-transcribing");
  $(".job-progress", card).hidden = false;
  $(".ticker-text", card).textContent = "sending file to this machine\u2019s server\u2026";
  jobsLabel.hidden = false;
  jobsEl.prepend(card);
  return card;
}

function setCardProgress(card, fraction, tickerText) {
  $(".chalkline-fill", card).style.width = (fraction * 100).toFixed(1) + "%";
  if (tickerText) $(".ticker-text", card).textContent = tickerText;
}

/* drag & drop + picker */
["dragenter", "dragover"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); })
);
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); })
);
dropzone.addEventListener("drop", (e) => {
  for (const f of e.dataTransfer.files) uploadFile(f);
});
pickBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  for (const f of fileInput.files) uploadFile(f);
  fileInput.value = "";
});

/* ---------------- mic recording ---------------- */

let recorder = null;
let recChunks = [];
let recStart = 0;
let recTick = null;

async function startRecording() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    alert("Microphone access was blocked. Allow it in your browser settings to record.");
    return;
  }
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus" : "";
  recorder = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : undefined);
  recChunks = [];
  recorder.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
  recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
    const stamp = new Date().toISOString().slice(0, 16).replace("T", "_").replace(":", "-");
    uploadFile(new File([blob], "mic-recording_" + stamp + ".webm", { type: blob.type }));
  };
  recorder.start(1000);
  recStart = Date.now();
  recStatus.hidden = false;
  recBtn.textContent = "Stop & transcribe";
  recTick = setInterval(() => {
    const s = Math.floor((Date.now() - recStart) / 1000);
    recTimer.textContent =
      String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }, 500);

  // keep the screen awake during long recordings, where supported
  try { await navigator.wakeLock?.request("screen"); } catch {}
}

function stopRecording() {
  clearInterval(recTick);
  recStatus.hidden = true;
  recBtn.innerHTML = '<span class="rec-dot" aria-hidden="true"></span>Record with mic';
  if (recorder && recorder.state !== "inactive") recorder.stop();
  recorder = null;
}

recBtn.addEventListener("click", () => {
  if (recorder) stopRecording();
  else startRecording();
});


async function uploadSlides(files, jobId) {
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  const url = jobId ? "/api/jobs/" + jobId + "/slides" : "/api/slides";
  const res = await apiFetch(url, { method: "POST", body: form });
  if (!res.ok) {
    let msg = "Upload failed (" + res.status + ").";
    try {
      const d = (await res.json()).detail;
      if (typeof d === "string") msg = d;
      else if (Array.isArray(d)) msg = "The app shell is outdated \u2014 hard-refresh the page (Ctrl+Shift+R) and try again.";
    } catch {}
    alert(msg);
    return false;
  }
  await refreshJobs();
  return true;
}

const slidesOnlyBtn = $("#slidesOnlyBtn");
const slidesOnlyInput = $("#slidesOnlyInput");
slidesOnlyBtn.addEventListener("click", () => slidesOnlyInput.click());
slidesOnlyInput.addEventListener("change", async () => {
  if (slidesOnlyInput.files.length) {
    jobsLabel.hidden = false;
    await uploadSlides([...slidesOnlyInput.files], null);
  }
  slidesOnlyInput.value = "";
});

/* ---------------- jobs ---------------- */

async function refreshJobs() {
  let jobs;
  try {
    const res = await apiFetch("/api/jobs");
    jobs = await res.json();
    serverNote.hidden = true;
    const db = document.querySelector("#deviceBoard");
    if (db) db.hidden = true;
  } catch {
    serverNote.hidden = false;
    const db = document.querySelector("#deviceBoard");
    if (db) db.hidden = false;
    return [];
  }
  jobsLabel.hidden = jobs.length === 0;

  for (const job of jobs) {
    let card = cards.get(job.id);
    if (!card) {
      card = jobTpl.content.firstElementChild.cloneNode(true);
      card.dataset.id = job.id;
      wireCard(card, job.id);
      cards.set(job.id, card);
      jobsEl.append(card);
    }
    renderCard(card, job);
  }
  // drop cards for jobs deleted elsewhere
  for (const [id, card] of cards) {
    if (!jobs.some((j) => j.id === id)) { card.remove(); cards.delete(id); }
  }
  return jobs;
}

function renderCard(card, job) {
  $(".job-name", card).textContent = job.filename;

  const bits = [];
  if (job.model && job.model !== "-") bits.push(job.model);
  if (job.duration) bits.push(fmtDur(job.duration) + " of audio");
  if (job.has_slides) bits.push("slides: " + (job.slides_name || "attached"));
  if (job.language) bits.push("lang: " + job.language);
  if (job.status === "done" && job.seconds_taken != null) bits.push("done in " + fmtDur(job.seconds_taken));
  $(".job-meta", card).textContent = bits.join(" \u00b7 ");

  const badge = $(".job-badge", card);
  badge.textContent = job.status;
  badge.className = "job-badge st-" + job.status;

  const active = job.status === "queued" || job.status === "extracting" || job.status === "transcribing";
  $(".job-progress", card).hidden = !active;
  if (active) {
    setCardProgress(card, job.progress || 0);
    $(".ticker-text", card).textContent =
      job.status === "transcribing" && job.latest
        ? job.latest
        : job.status === "extracting"
        ? "extracting audio with ffmpeg\u2026"
        : "waiting in queue\u2026";
  }

  $(".job-error", card).hidden = job.status !== "error";
  if (job.status === "error") $(".job-error", card).textContent = job.error || "Something went wrong.";

  const done = job.status === "done";
  $(".job-actions", card).hidden = !done;
  if (done) {
    $(".act-dl-ts", card).href = "/api/jobs/" + job.id + "/download?kind=timestamped";
    $(".act-dl-plain", card).href = "/api/jobs/" + job.id + "/download?kind=plain";
  }
  const hasT = job.has_transcript !== false;
  for (const sel of [".act-view", ".act-copy", ".act-dl-ts", ".act-dl-plain"]) {
    $(sel, card).style.display = hasT ? "" : "none";
  }
  $(".act-slides", card).textContent = job.has_slides ? "Add more materials" : "Add materials";

  // study guide state
  const gStatus = $(".guide-status", card);
  const gText = $(".guide-status-text", card);
  const gBtn = $(".act-guide", card);
  const gs = job.guide_status || "none";
  gBtn.textContent = gs === "done" ? "View study guide" : "Study guide";
  gBtn.disabled = gs === "generating";
  if (gs === "generating") {
    gStatus.hidden = false;
    gStatus.classList.remove("err");
    gText.textContent = "generating study guide\u2026 (30\u201390 s)";
  } else if (gs === "error") {
    gStatus.hidden = false;
    gStatus.classList.add("err");
    gText.textContent = job.guide_error || "Guide generation failed.";
  } else {
    gStatus.hidden = true;
  }
  if (gs === "done" && card.dataset.awaitGuide === "1") {
    card.dataset.awaitGuide = "";
    showGuide(card, job.id);
  }
}

async function showGuide(card, id) {
  const panel = $(".guide-panel", card);
  try {
    const res = await apiFetch("/api/jobs/" + id + "/guide");
    if (!res.ok) throw new Error();
    const { markdown } = await res.json();
    $(".guide-body", card).innerHTML = renderMarkdown(markdown);
    $(".act-dl-guide", card).href = "/api/jobs/" + id + "/guide/download?fmt=docx";
      $(".act-dl-guide-md", card).href = "/api/jobs/" + id + "/guide/download?fmt=md";
    panel.hidden = false;
  } catch {
    /* no guide yet */
  }
}

function wireCard(card, id) {
  $(".act-guide", card).addEventListener("click", async () => {
    const panel = $(".guide-panel", card);
    // already generated -> toggle the panel
    const probe = await apiFetch("/api/jobs/" + id + "/guide");
    if (probe.ok) {
      if (!panel.hidden) { panel.hidden = true; return; }
      const { markdown } = await probe.json();
      $(".guide-body", card).innerHTML = renderMarkdown(markdown);
      $(".act-dl-guide", card).href = "/api/jobs/" + id + "/guide/download?fmt=docx";
      $(".act-dl-guide-md", card).href = "/api/jobs/" + id + "/guide/download?fmt=md";
      panel.hidden = false;
      return;
    }
    // not generated yet -> start it (or steer to the free path)
    const res = await apiFetch("/api/jobs/" + id + "/guide", { method: "POST" });
    if (res.ok) {
      card.dataset.awaitGuide = "1";
      startPolling();
      refreshJobs();
    } else {
      let msg = "Could not start guide generation.";
      try { msg = (await res.json()).detail || msg; } catch {}
      if (msg.startsWith("No API key")) {
        if (confirm(msg + "\n\nOpen Settings now?")) { loadSettings(); settingsDlg.showModal(); }
      } else {
        alert(msg);
      }
    }
  });

  $(".act-slides", card).addEventListener("click", () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".pdf,.pptx,.ppt,.docx,.doc,.xlsx,.xls,.png,.jpg,.jpeg";
    input.multiple = true;
    input.onchange = async () => {
      if (input.files[0]) {
        const btn = $(".act-slides", card);
        btn.textContent = "Extracting…";
        const ok = await uploadSlides(input.files, id);
        btn.textContent = "Add materials";
        if (ok) $(".guide-panel", card).hidden = true; // guide was invalidated
      }
    };
    input.click();
  });

  $(".act-prompt", card).addEventListener("click", async (e) => {
    const res = await apiFetch("/api/jobs/" + id + "/guide_prompt");
    if (!res.ok) { alert("Transcript isn't ready yet."); return; }
    const { prompt } = await res.json();
    await navigator.clipboard.writeText(prompt);
    const btn = e.currentTarget;
    const old = btn.textContent;
    btn.textContent = "Copied \u2014 paste into Claude.ai";
    setTimeout(() => (btn.textContent = old), 2200);
  });

  $(".act-view", card).addEventListener("click", async () => {
    const pre = $(".job-text", card);
    if (!pre.hidden) { pre.hidden = true; $(".act-view", card).textContent = "View transcript"; return; }
    if (!pre.textContent) {
      const res = await apiFetch("/api/jobs/" + id + "/text?kind=timestamped");
      pre.textContent = (await res.json()).text;
    }
    pre.hidden = false;
    $(".act-view", card).textContent = "Hide transcript";
  });

  $(".act-copy", card).addEventListener("click", async (e) => {
    const res = await apiFetch("/api/jobs/" + id + "/text?kind=plain");
    await navigator.clipboard.writeText((await res.json()).text);
    const btn = e.currentTarget;
    const old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = old), 1400);
  });

  $(".act-del", card).addEventListener("click", async () => {
    if (!confirm("Delete this transcription?")) return;
    await apiFetch("/api/jobs/" + id, { method: "DELETE" });
    card.remove();
    cards.delete(id);
    if (!cards.size) jobsLabel.hidden = true;
  });
}

function fmtDur(sec) {
  sec = Math.round(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? m + "m " + String(s).padStart(2, "0") + "s" : s + "s";
}

/* poll while anything is active */
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    const jobs = await refreshJobs();
    const anyActive = jobs.some((j) =>
      j.status === "queued" || j.status === "extracting" || j.status === "transcribing" ||
      j.guide_status === "generating");
    if (!anyActive) { clearInterval(pollTimer); pollTimer = null; }
  }, 1500);
}

/* ---------------- boot ---------------- */
async function bootRefresh(attempt = 0) {
  await refreshJobs();
  // server not answering yet (cold start on free hosting) -> retry ~48s
  if (!serverNote.hidden && attempt < 12) {
    setTimeout(() => bootRefresh(attempt + 1), 4000);
  }
}
ensureAccess().then(() => { loadSettings(); bootRefresh(); });
refreshJobs().then((jobs) => {
  if (jobs.some((j) =>
    (j.status !== "done" && j.status !== "error") || j.guide_status === "generating")) startPolling();
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
