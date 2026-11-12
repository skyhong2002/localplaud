"""Offline fallback service worker: public script, static-only cache, no private pages."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_web_ui import _client

STATIC = Path(__file__).parents[1] / "src" / "localplaud" / "api" / "static"


def test_service_worker_is_served_at_root_scope_with_a_release_version(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["service-worker-allowed"] == "/"
    assert "__LP_VERSION__" not in response.text
    assert re.search(r"const VERSION = 'lp-[0-9a-f]{12}';", response.text)

    offline = client.get("/static/offline.html")
    assert offline.status_code == 200
    assert "無法連線到 localplaud" in offline.text
    assert 'lang="en"' in offline.text
    # Registration is limited to secure contexts and never asks to cache pages.
    app_js = (STATIC / "js" / "app.js").read_text()
    assert "navigator.serviceWorker.register('/sw.js', {scope: '/'})" in app_js
    assert "window.isSecureContext" in app_js


def test_service_worker_is_public_under_password_login(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.delenv("LOCALPLAUD_API__AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "correct horse")
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "a-long-random-session-secret")
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'sw.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    client = TestClient(app, base_url="https://testserver")
    # An expired session must not turn the background update check into a redirect.
    assert client.get("/sw.js", follow_redirects=False).status_code == 200
    assert client.get("/static/offline.html", follow_redirects=False).status_code == 200
    assert client.get("/api/files").status_code == 401


def test_service_worker_is_public_with_accounts(monkeypatch, tmp_path):
    from localplaud.api.accounts import PUBLIC

    assert "/sw.js" in PUBLIC


_HARNESS = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8').replaceAll('__LP_VERSION__', 'test');
const listeners = {}, stores = new Map(), puts = [];
let online = true;
class Resp { constructor(body, init = {}) { this.body = body; this.status = init.status ?? 200; this.ok = this.status < 400; this.type = 'basic'; this.headers = init.headers || {}; }
  clone() { return new Resp(this.body, {status: this.status, headers: this.headers}); } }
const caches = {
  async open(name) { if (!stores.has(name)) stores.set(name, new Map());
    const store = stores.get(name);
    return { async addAll(urls) { for (const u of urls) store.set(u, new Resp('core:' + u)); },
             async match(req) { return store.get(typeof req === 'string' ? req : new URL(req.url).pathname + new URL(req.url).search); },
             async put(req, res) { const key = new URL(req.url).pathname + new URL(req.url).search; puts.push(key); store.set(key, res); } }; },
  async keys() { return [...stores.keys()]; },
  async delete(name) { return stores.delete(name); },
  async match(url, opts) { return stores.get(opts.cacheName)?.get(url); },
};
const fetch = async req => { if (!online) throw new TypeError('offline'); return new Resp('net:' + (req.url || req)); };
const self = { location: { origin: 'https://lp.test' }, skipWaiting() {}, clients: { claim() {} },
  addEventListener(type, fn) { listeners[type] = fn; } };
vm.runInNewContext(source, { self, caches, fetch, Response: Resp, URL, console });
function request(path, { mode = 'cors', method = 'GET', headers = {} } = {}) {
  return { url: 'https://lp.test' + path, mode, method, headers: { has: k => k in headers, get: k => headers[k] } };
}
async function dispatch(req) {
  let responded = null; const waits = [];
  listeners.fetch({ request: req, respondWith(p) { responded = Promise.resolve(p); }, waitUntil(p) { waits.push(p); } });
  const result = responded ? await responded : null; await Promise.all(waits); return result;
}
(async () => {
  let waits = []; listeners.install({ waitUntil(p) { waits.push(p); } }); await Promise.all(waits);
  stores.set('lp-old', new Map()); waits = [];
  listeners.activate({ waitUntil(p) { waits.push(p); } }); await Promise.all(waits);
  assert.deepEqual([...stores.keys()], ['lp-test']);

  // Pages, fragments, API and audio are never written to the cache.
  for (const [path, opts] of [['/file/r1', {mode: 'navigate'}], ['/file/r1?workspace=true', {headers: {'hx-request': 'true'}}],
                              ['/api/files', {}], ['/audio/r1', {}], ['/share/abc', {mode: 'navigate'}]]) await dispatch(request(path, opts));
  assert.deepEqual(puts, []);
  // Range requests and writes are left entirely to the browser.
  assert.equal(await dispatch(request('/audio/r1', {headers: {range: 'bytes=0-'}})), null);
  assert.equal(await dispatch(request('/api/files', {method: 'POST'})), null);

  // Static assets are cached and served when the network is gone.
  const css = await dispatch(request('/static/css/app.css?v=1'));
  assert.equal(css.body, 'net:https://lp.test/static/css/app.css?v=1');
  assert.deepEqual(puts, ['/static/css/app.css?v=1']);
  online = false;
  assert.equal((await dispatch(request('/static/css/app.css?v=1'))).body, 'net:https://lp.test/static/css/app.css?v=1');

  // An unreachable server shows the offline page for app navigations only.
  assert.equal((await dispatch(request('/home', {mode: 'navigate'}))).body, 'core:/static/offline.html');
  assert.equal(await dispatch(request('/login', {mode: 'navigate'})), null);
  assert.equal(await dispatch(request('/file/r1?workspace=true', {headers: {'hx-request': 'true'}})), null);
  console.log(JSON.stringify({ok: true}));
})().catch(error => { console.error(error); process.exit(1); });
"""


def test_service_worker_caches_only_static_assets_and_falls_back_offline():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the service worker")
    result = subprocess.run(
        [node, "-e", _HARNESS, str(STATIC / "sw.js")], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True}
