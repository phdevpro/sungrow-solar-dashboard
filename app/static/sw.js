// Minimal service worker: makes the app installable and caches the static
// shell (icons, manifest). Data requests always go to the network.
const CACHE = "solar-shell-v1";
const SHELL = ["/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
  }
  // Everything else (page, API): straight to the network.
});
