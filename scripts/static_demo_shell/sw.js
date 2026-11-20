// localplaud read-only demo: answers app requests from a recorded snapshot.
const BASE = "__BASE__";
const SHELL = new Set(["/sw.js", "/manifest.json", "/boot.html"]);
const IGNORED = new Set(["return_to"]); // keep in sync with build_static_demo.py
let manifestPromise = null;

function manifest() {
  if (!manifestPromise) {
    manifestPromise = fetch(BASE + "/manifest.json", { cache: "no-cache" }).then((r) => r.json());
  }
  return manifestPromise;
}

self.addEventListener("install", (event) => {
  event.waitUntil(manifest().then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

function keyFor(path, search, hx) {
  const params = [...new URLSearchParams(search)].filter(([k]) => !IGNORED.has(k)).sort((a, b) =>
    a[0] === b[0] ? (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) : a[0] < b[0] ? -1 : 1,
  );
  const q = new URLSearchParams(params).toString();
  return path + (q ? "?" + q : "") + (hx ? " #hx" : "");
}

function lookup(m, path, search, hx) {
  const candidates = [
    keyFor(path, search, hx),
    keyFor(path, search, !hx),
    keyFor(path, "", hx),
    keyFor(path, "", !hx),
  ];
  for (const k of candidates) if (m[k]) return m[k];
  return null;
}

async function respondFrom(entry, request) {
  const res = await fetch(BASE + "/_r/" + entry.f);
  const blob = entry.f.endsWith(".gz")
    ? await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).blob()
    : await res.blob();
  const headers = { "Content-Type": entry.t, "Accept-Ranges": "bytes" };
  if (entry.d) headers["Content-Disposition"] = entry.d;
  const range = request.headers.get("range");
  if (range) {
    const m = /bytes=(\d*)-(\d*)/.exec(range) || [];
    const size = blob.size;
    let start, end;
    if (m[1]) { start = parseInt(m[1], 10); end = m[2] ? Math.min(parseInt(m[2], 10), size - 1) : size - 1; }
    else if (m[2]) { start = Math.max(0, size - parseInt(m[2], 10)); end = size - 1; }
    else { start = 0; end = size - 1; }
    return new Response(blob.slice(start, end + 1, entry.t), {
      status: 206,
      headers: { ...headers, "Content-Range": `bytes ${start}-${end}/${size}`, "Content-Length": String(end - start + 1) },
    });
  }
  return new Response(blob, { status: entry.s || 200, headers });
}

async function notifyReadOnly(clientId) {
  const client = clientId && (await self.clients.get(clientId));
  if (client) client.postMessage("lp-readonly");
}

function missingPage() {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not in the demo · localplaud</title>
<body style="font:16px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#F7F8FA;color:#101828">
<div style="max-width:420px;text-align:center;padding:24px"><h1 style="font-size:22px">This page isn't part of the demo</h1>
<p style="color:#475467">The static demo includes the main workspace views. Install localplaud to explore everything.</p>
<p><a href="${BASE}/" style="color:#007AFF">Back to the demo</a> · <a href="https://github.com/skyhong2002/localplaud#quickstart" style="color:#007AFF">Get localplaud</a></p></div>`,
    { status: 404, headers: { "Content-Type": "text/html; charset=utf-8" } },
  );
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  let path = url.pathname;
  if (path.startsWith(BASE)) path = path.slice(BASE.length) || "/";
  else if (event.request.mode === "navigate") return; // outside the demo: let the network handle it
  if (SHELL.has(path) || path.startsWith("/_r/")) return;

  event.respondWith(
    (async () => {
      const req = event.request;
      if (req.method !== "GET" && req.method !== "HEAD") {
        event.waitUntil(notifyReadOnly(event.clientId || event.resultingClientId));
        return new Response("This is a read-only demo — changes are not saved.", {
          status: 403,
          headers: { "Content-Type": "text/plain; charset=utf-8", "X-Localplaud-Demo": "read-only" },
        });
      }
      const m = await manifest();
      const hx = req.headers.get("hx-request") === "true";
      const entry = lookup(m, path, url.search, hx);
      if (entry) return respondFrom(entry, req);
      if (req.mode === "navigate") return missingPage();
      return new Response("", { status: 404 });
    })(),
  );
});
