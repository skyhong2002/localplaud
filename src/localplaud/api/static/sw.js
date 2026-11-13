/* localplaud service worker: shared static assets and an offline fallback page.
 *
 * Served from /sw.js (scope "/") with __LP_VERSION__ replaced by the shell
 * asset version, so every upgrade installs a fresh cache and drops the old one.
 *
 * Privacy rule: only same-origin /static/* GET responses are ever stored.
 * Pages, HTMX fragments, API responses, audio, exports and share links are
 * never cached: they can contain private transcripts, and several accounts
 * may share one browser. A navigation that cannot reach the server shows the
 * static offline page instead of the browser's connection error.
 */
const VERSION = 'lp-__LP_VERSION__';
const OFFLINE_URL = '/static/offline.html';
const CORE = [
  OFFLINE_URL,
  '/static/logo.svg',
  '/static/favicon.svg',
  '/static/htmx-1.9.12.min.js',
];
const NO_FALLBACK = /^\/(?:api|audio|share|login|logout|oauth|auth|register|healthz|i18n)(?:\/|$)/;

self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(cache => cache.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== VERSION) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET' || request.headers.has('range')) return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {
    // Stale-while-revalidate. Asset URLs carry ?v=<content hash>, so a new
    // release requests new keys rather than reusing an outdated file.
    event.respondWith((async () => {
      const cache = await caches.open(VERSION);
      const cached = await cache.match(request);
      const network = fetch(request).then(response => {
        if (response.ok && response.type === 'basic') cache.put(request, response.clone());
        return response;
      });
      if (cached) {
        event.waitUntil(network.catch(() => undefined));
        return cached;
      }
      return network;
    })());
    return;
  }

  if (request.mode === 'navigate' && !NO_FALLBACK.test(url.pathname)) {
    // Network only; the response is never stored. Server errors pass through.
    event.respondWith(fetch(request).catch(async () => (
      (await caches.match(OFFLINE_URL, {cacheName: VERSION}))
      || new Response('localplaud is offline.', {status: 503, headers: {'content-type': 'text/plain; charset=utf-8'}})
    )));
  }
});
