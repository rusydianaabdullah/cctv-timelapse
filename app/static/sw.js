// Network-first: CCTV data must always be live; cache only lets the app shell open while offline.
const CACHE = 'cctv-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/') || u.pathname.startsWith('/timelapse/file/')) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && e.request.mode === 'navigate') { const c = r.clone(); caches.open(CACHE).then(k => k.put(e.request, c)) }
    return r;
  }).catch(() => caches.match(e.request)));
});
