/* Degome service worker — caches the app shell and, at runtime, the
   transformers.js library so on-device transcription works offline
   after first use. Model files are cached by transformers.js itself. */
const CACHE = "degome-shell-v13";
const CDN_CACHE = "degome-cdn-v1";
const SHELL = ["/", "/index.html", "/style.css", "/app.js", "/device.js",
  "/browser-asr.js", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE && k !== CDN_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // network only

  // runtime cache-first for the transformers.js CDN
  if (url.hostname === "cdn.jsdelivr.net") {
    e.respondWith(
      caches.open(CDN_CACHE).then(async (cache) => {
        const hit = await cache.match(e.request);
        if (hit) return hit;
        const resp = await fetch(e.request);
        if (resp.ok) cache.put(e.request, resp.clone());
        return resp;
      })
    );
    return;
  }

  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
