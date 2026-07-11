/* Degome — in-browser transcription worker (module worker).
   Runs Whisper via transformers.js entirely on the device: WebGPU when
   available, WebAssembly otherwise. The model downloads once from the
   Hugging Face CDN and is cached by the browser for offline reuse. */

import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.3.1";

env.allowLocalModels = false; // always fetch/cache from the hub

const MODELS = {
  tiny: "onnx-community/whisper-tiny",
  base: "onnx-community/whisper-base",
};

let transcriber = null;
let loadedModel = null;

async function loadModel(size) {
  if (transcriber && loadedModel === size) return;
  transcriber = null;
  loadedModel = null;

  const opts = {
    dtype: "q8",
    progress_callback: (p) => {
      if (p.status === "progress" && p.total) {
        postMessage({
          type: "download",
          file: p.file,
          progress: p.loaded / p.total,
        });
      }
    },
  };

  // Prefer WebGPU; fall back to WASM if unsupported or it fails to init.
  try {
    if (self.navigator && "gpu" in self.navigator) {
      transcriber = await pipeline("automatic-speech-recognition", MODELS[size], {
        ...opts, device: "webgpu",
      });
    }
  } catch {
    transcriber = null;
  }
  if (!transcriber) {
    transcriber = await pipeline("automatic-speech-recognition", MODELS[size], opts);
  }
  loadedModel = size;
}

self.onmessage = async (e) => {
  const msg = e.data;
  try {
    if (msg.type === "transcribe") {
      postMessage({ type: "status", status: "loading-model" });
      await loadModel(msg.model in MODELS ? msg.model : "base");

      postMessage({ type: "status", status: "transcribing" });
      const output = await transcriber(msg.audio, {
        chunk_length_s: 30,
        stride_length_s: 5,
        return_timestamps: true,
        language: msg.language || "english",
        task: "transcribe",
      });

      postMessage({
        type: "result",
        text: output.text || "",
        chunks: (output.chunks || []).map((c) => ({
          start: c.timestamp ? c.timestamp[0] : null,
          text: c.text,
        })),
      });
    }
  } catch (err) {
    postMessage({ type: "error", message: String(err && err.message || err) });
  }
};

postMessage({ type: "ready" });
