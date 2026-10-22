/* localplaud service worker: app-shell caching and an offline fallback.
 *
 * Served from /sw.js (scope "/"). Rules:
 * - Only same-origin GET requests are ever cached.
 * - Never cached: /api/*, /audio/*, exports/downloads, /share/*, auth routes,
 *   range requests, and HTMX fragment requests (always live).
 * - /static/* assets: cache-first, refreshed in the background.
 * - Page navigations: network-first; a successful page is kept so it can be
 *   shown offline (read-only), otherwise the offline page is returned.
 * Logging out clears every cache (see app.js).
 */
const VERSION = 'lp-v1';
const SHELL = `${VERSION}-shell`;
const PAGES = `${VERSION}-pages`;
const MAX_PAGES = 30;
const CORE = [
  '/static/offline.html',
  '/static/htmx-1.9.12.min.js',
  '/static/logo.svg',
  '/static/favicon.svg',
  '/static/icons/icon-192.png',
];
const NEVER = /^\/(?:api|audio|share|login|logout|oauth|healthz|sw\.js)(?:\/|$)|\/(?:export|download)(?:\/|\.|$)/;

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL).then(cache => cache.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (!key.startsWith(VERSION)) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener('message', event => {
  if (event.data === 'lp:clear-caches') {
    event.waitUntil(caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key)))));
  }
});

async function trimPages() {
  const cache = await caches.open(PAGES);
  const keys = await cache.keys();
  for (const request of keys.slice(0, Math.max(0, keys.length - MAX_PAGES))) await cache.delete(request);
}

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET' || request.headers.has('range')) return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || NEVER.test(url.pathname)) return;
  const htmx = request.headers.get('hx-request') === 'true';
  // Progressive page shells load their content with ?workspace=true; keep those
  // (network-first) so a previously opened page still reads offline. Every other
  // HTMX fragment is live-only.
  if (htmx && url.searchParams.get('workspace') !== 'true') return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL);
      const cached = await cache.match(request);
      const network = fetch(request).then(response => {
        if (response.ok && response.type === 'basic') cache.put(request, response.clone());
        return response;
      }).catch(() => cached);
      return cached || network;
    })());
    return;
  }

  if (request.mode === 'navigate' || htmx) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok && response.type === 'basic' && !response.redirected) {
          const cache = await caches.open(PAGES);
          await cache.put(request, response.clone());
          trimPages();
        }
        return response;
      } catch (_error) {
        return (await caches.match(request, {cacheName: PAGES}))
          || (htmx ? undefined : await caches.match('/static/offline.html'))
          || new Response('Offline', {status: 503, headers: {'content-type': 'text/plain'}});
      }
    })());
  }
});
