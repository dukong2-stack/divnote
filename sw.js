// 화면은 캐시로 빠르게, 데이터는 항상 최신을 먼저 시도
const SHELL = "divnote-v13";
const FILES = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok && !url.pathname.includes("/data/")) caches.open(SHELL).then(c => c.put(e.request, r.clone()));
      return r;
    }).catch(() => caches.match(e.request, {ignoreSearch: true}))
  );
});
