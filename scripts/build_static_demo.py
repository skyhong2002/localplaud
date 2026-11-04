#!/usr/bin/env python3
"""Snapshot the demo Web App into a read-only static site for GitHub Pages.

The Web App is server-rendered (FastAPI + HTMX), so a static host cannot run it.
This script crawls a running demo server (see ``scripts/serve_demo.sh``) with a
real browser, records every GET response the UI makes (pages, HTMX fragments,
JSON, audio, static assets), rewrites absolute URLs under ``--base``, and writes:

* ``<out>/demo/_r/*``          recorded response bodies
* ``<out>/demo/manifest.json``  request key -> recorded response
* ``<out>/demo/sw.js``          service worker that answers requests from the
                                  manifest and rejects writes as read-only
* ``<out>/demo/index.html``     bootstrap that installs the service worker

Only synthetic demo data should ever be crawled. Run with Playwright, e.g.::

    uv run --no-project --with playwright python scripts/build_static_demo.py \
        --origin http://127.0.0.1:8792 --out site --base /localplaud/demo
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sys
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from playwright.sync_api import sync_playwright

ROUTES = (
    "about account admin api apply-library ask audio auth connections destinations "
    "discover emails explore exports external-rules favicon.ico file files folders "
    "hardware-recommendations healthz home login logout models note-templates notes "
    "notifications plaud profiles recordings register resolution-preview resolve "
    "retention robots.txt rules run runs search settings share starting-profiles "
    "static status sync-deliveries templates ui webhooks workers"
).split()

# Never crawl these: they mutate state, leak operator details, or are unbounded.
DENY = re.compile(
    r"^/(logout|login|register|auth/|admin|diagnostics\.json|api/system|api/storage|"
    r"api/backups|api/providers|api/integrations|exports/|share/|api/ask/streams|"
    r"file/[^/]+/export)"
)
TEXT_TYPES = ("text/", "application/json", "application/javascript", "image/svg")
LEAK_PATTERNS = [re.compile(p) for p in (r"/Users/\w+", r"/home/\w+", r"/private/tmp", r"/tmp/lp-")]


# Query parameters that never change the response (kept in sync with sw.js).
IGNORED_PARAMS = {"return_to"}
# Links that only re-sort or re-layout a list are not crawled; the service worker
# falls back to the default view for them.
SKIP_PARAMS = {"sort", "dir", "view"}


def key_for(path: str, query: str, hx: bool) -> str:
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in IGNORED_PARAMS]
    q = urlencode(sorted(pairs))
    return f"{path}{'?' + q if q else ''}{' #hx' if hx else ''}"


def rewrite(text: str, base: str, origin: str = "") -> str:
    if origin:
        text = text.replace(origin + "/", base + "/").replace(origin, base)
    alt = "|".join(re.escape(r) for r in ROUTES)
    # A leading "/" + known route, preceded by a quote, backtick, "(", "=" or space.
    pattern = re.compile(rf"(?<=[\"'`(=\s,])/(?=(?:{alt})(?:[/?#\"'`)\s$,]|$))")
    text = pattern.sub(base + "/", text)
    # Root links: href="/" and similar.
    text = re.sub(
        r"""((?:href|action|hx-get|hx-post)=["'])/(?=["'])""",
        lambda m: m.group(1) + base + "/",
        text,
    )
    return text


BANNER = """
<style>.sf-ask-banner{display:none!important}#lp-demo-pill.mini .full{display:none!important}#lp-demo-pill:not(.mini) .mini-label{display:none!important}</style>
<div id="lp-demo-pill" style="position:fixed;z-index:2147483646;top:10px;left:50%;transform:translateX(-50%);
background:#101828;color:#fff;font:600 12px/1 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;
padding:8px 12px;border-radius:999px;box-shadow:0 6px 20px rgba(0,0,0,.25);display:flex;gap:10px;align-items:center;white-space:nowrap">
<button class="mini-label" onclick="this.parentNode.classList.remove('mini')" style="all:unset;cursor:pointer">Demo</button>
<span class="full">Read-only demo · synthetic data</span>
<a class="full" href="https://github.com/skyhong2002/localplaud#quickstart" style="color:#8EC5FF;text-decoration:none">Get localplaud ↗</a>
<button class="full" aria-label="Minimize demo notice" onclick="this.parentNode.classList.add('mini')" style="all:unset;cursor:pointer;opacity:.7">✕</button></div>
<div id="lp-demo-toast" role="status" aria-live="polite" style="position:fixed;z-index:2147483647;bottom:24px;left:50%;
transform:translateX(-50%);background:#101828;color:#fff;font:500 14px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;
padding:10px 16px;border-radius:10px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw;text-align:center"></div>
<script>(function(){var t;setTimeout(function(){var p=document.getElementById('lp-demo-pill');if(p)p.classList.add('mini')},7000);function toast(){var e=document.getElementById('lp-demo-toast');if(!e)return;
e.textContent='This is a read-only demo — changes are not saved. Install localplaud to edit your own recordings.';
e.style.opacity=1;clearTimeout(t);t=setTimeout(function(){e.style.opacity=0},3200)}
document.addEventListener('htmx:responseError',function(ev){if(ev.detail&&ev.detail.xhr&&ev.detail.xhr.status===403)toast()});
navigator.serviceWorker&&navigator.serviceWorker.addEventListener('message',function(ev){if(ev.data==='lp-readonly')toast()});})();</script>
"""


def inject_banner(html: str) -> str:
    m = re.search(r"<body[^>]*>", html, re.I)
    return html[: m.end()] + BANNER + html[m.end() :] if m else html


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default="http://127.0.0.1:8792")
    ap.add_argument("--out", default="site")
    ap.add_argument("--base", default="/localplaud/demo", help="URL path the demo is served under")
    ap.add_argument("--pages-root", default="/localplaud", help="URL path that maps to --out")
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument(
        "--featured", default="904813b8118c1515cb314263", help="file id whose exports are captured"
    )
    args = ap.parse_args()

    origin, base = args.origin.rstrip("/"), args.base.rstrip("/")
    root = Path(args.out) / base[len(args.pages_root.rstrip("/")) :].strip("/")
    if root.exists():
        shutil.rmtree(root)
    (root / "_r").mkdir(parents=True)

    store: dict[str, dict] = {}
    aliases: dict[str, str] = {}

    def record(url: str, status: int, headers: dict, body: bytes, hx: bool) -> None:
        parts = urlsplit(url)
        if not url.startswith(origin):
            return
        key = key_for(parts.path, parts.query, hx)
        ctype = headers.get("content-type", "application/octet-stream")
        if any(ctype.startswith(t) for t in TEXT_TYPES):
            text = body.decode("utf-8", "replace")
            if ctype.startswith("text/html") and not hx and "<body" in text.lower():
                text = inject_banner(text)
            text = re.sub(r"/(?:private/)?tmp/lp-[\w.-]+/(?:state|demo)/", "data/", text)
            body = rewrite(text, base, origin).encode()
        text_like = any(ctype.startswith(t) for t in TEXT_TYPES)
        name = hashlib.sha1(body).hexdigest()[:20] + (".gz" if text_like else "")
        target = root / "_r" / name
        if not target.exists():  # identical bodies are stored once
            target.write_bytes(gzip.compress(body, 9, mtime=0) if text_like else body)
        store[key] = {"f": name, "t": ctype, "s": status}
        if disposition := headers.get("content-disposition"):
            store[key]["d"] = disposition

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        # Prepare the throwaway demo copy: a model-free outline for the featured recording.
        ctx.request.post(
            origin + f"/api/files/{args.featured}/outline/regenerate",
            data={"method": "time_slices"},
        )
        ctx.request.put(
            origin + "/api/preferences/workspace",
            data={
                "workspace_name": "Demo workspace",
                "timezone": "Asia/Taipei",
                "ask_enabled": True,
            },
        )

        pending: list = []

        def on_response(resp):
            # Body reads are not allowed inside sync event handlers; drain later.
            if resp.request.method == "GET" and resp.url.startswith(origin):
                pending.append(resp)

        def drain() -> None:
            while pending:
                resp = pending.pop(0)
                req = resp.request
                hx = req.headers.get("hx-request") == "true"
                if 300 <= resp.status < 400:
                    loc = resp.headers.get("location", "")
                    if loc:
                        src = urlsplit(resp.url)
                        dst = urlsplit(loc if loc.startswith("http") else origin + loc)
                        aliases[key_for(src.path, src.query, hx)] = key_for(dst.path, dst.query, hx)
                    continue
                if resp.status >= 400:
                    continue
                try:
                    record(resp.url, resp.status, resp.headers, resp.body(), hx)
                except Exception as exc:
                    print("  body failed", resp.url, exc, file=sys.stderr)

        ctx.on("response", on_response)
        page = ctx.new_page()
        seen: set[str] = set()
        fetched_hx: set[str] = set()
        queue = deque(
            [
                "/",
                "/home",
                "/search",
                "/search?q=onboarding",
                "/templates",
                "/templates?tab=my",
                "/discover",
                "/explore",
                "/ask",
                "/notes",
                "/notifications",
                "/status",
                "/settings",
            ]
        )
        while queue and len(seen) < args.max_pages:
            path = queue.popleft()
            if path in seen:
                continue
            seen.add(path)
            if "/export" in path:
                try:
                    r = ctx.request.get(origin + path)
                    if r.ok:
                        record(origin + path, r.status, r.headers, r.body(), False)
                except Exception as exc:
                    print("skip", path, exc, file=sys.stderr)
                continue
            try:
                page.goto(origin + path, wait_until="networkidle", timeout=30000)
            except Exception as exc:  # keep crawling
                print("skip", path, exc, file=sys.stderr)
                continue
            if path.startswith("/file/") and path.count("/") == 2:
                for tab in page.locator("[role=tab][data-panel]").all():
                    try:
                        tab.click(timeout=2000)
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
            drain()
            # Pre-fetch every hx-get target so lazy panels work offline.
            for hx in page.eval_on_selector_all(
                "[hx-get]", "els=>els.map(e=>e.getAttribute('hx-get'))"
            ):
                if (
                    hx
                    and hx.startswith("/")
                    and hx not in fetched_hx
                    and not DENY.match(urlsplit(hx).path)
                ):
                    fetched_hx.add(hx)
                    try:
                        r = ctx.request.get(origin + hx, headers={"HX-Request": "true"})
                        if r.ok:
                            record(origin + hx, r.status, r.headers, r.body(), True)
                    except Exception:
                        pass
            hrefs = page.eval_on_selector_all("a[href]", "els=>els.map(e=>e.getAttribute('href'))")
            for href in hrefs:
                if not href or not href.startswith("/") or href.startswith("//"):
                    continue
                u = urlsplit(href)
                if DENY.match(u.path) and not (u.path.startswith(f"/file/{args.featured}/export")):
                    continue
                params = {k for k, _ in parse_qsl(u.query)}
                if params & SKIP_PARAMS:
                    continue
                query = urlencode(
                    [(k, v) for k, v in parse_qsl(u.query) if k not in IGNORED_PARAMS]
                )
                target = u.path + ("?" + query if query else "")
                if target not in seen:
                    queue.append(target)
            drain()
            print(f"{len(seen):4d} {path}  ({len(store)} responses)", flush=True)
        browser.close()

    leaks = []
    for key, entry in store.items():
        if entry["t"].startswith(TEXT_TYPES) and not key.startswith("/static/"):
            body = gzip.decompress((root / "_r" / entry["f"]).read_bytes()).decode(
                "utf-8", "replace"
            )
            for pat in LEAK_PATTERNS:
                if m := pat.search(body):
                    leaks.append((key, m.group(0)))
    for key, dst in aliases.items():
        if key not in store and dst in store:
            store[key] = store[dst]
    (root / "manifest.json").write_text(json.dumps(store, separators=(",", ":")))
    shell = Path(__file__).with_name("static_demo_shell")
    for f in shell.iterdir():
        (root / f.name).write_text(f.read_text().replace("__BASE__", base))
    print(f"captured {len(store)} responses from {len(seen)} pages -> {root}")
    if leaks:
        print("POSSIBLE LEAKS:", *sorted(set(leaks))[:40], sep="\n  ")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
