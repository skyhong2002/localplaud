"""Shell asset loading: cached translation catalog, deferred scripts, focus and type floors."""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_web_ui import _client, _seed

STATIC = Path(__file__).parents[1] / "src" / "localplaud" / "api" / "static"


def _set_locale(client, locale: str) -> None:
    defaults = client.get("/api/preferences/workspace").json()
    assert client.put("/api/preferences/workspace", json=defaults | {"locale": locale}).status_code == 200


def test_catalog_is_a_cached_script_instead_of_inline_page_data(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        _set_locale(client, "zh-Hant-TW")
        page = client.get("/home")
        assert page.status_code == 200
        match = re.search(r'<script src="(/i18n/zh-Hant-TW\.js\?v=([0-9a-f]{12}))"></script>', page.text)
        assert match, "the shell must reference the versioned catalog script"
        # The catalog is no longer serialized into every HTML response.
        assert '"Settings sections":"設定區段"' not in page.text
        assert "Object.freeze" not in page.text

        versioned = client.get(match.group(1))
        assert versioned.status_code == 200
        assert versioned.headers["content-type"].startswith("text/javascript")
        assert versioned.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert versioned.headers["etag"] == f'"{match.group(2)}"'
        assert versioned.text.startswith("window.localplaudCatalog=Object.freeze({")
        assert '"Settings sections":"設定區段"' in versioned.text

        # A stale or missing version revalidates instead of pinning old copy for a year.
        stale = client.get("/i18n/zh-Hant-TW.js?v=000000000000")
        assert stale.status_code == 200
        assert stale.headers["cache-control"] == "private, no-cache"
        assert client.get("/i18n/en.js").status_code == 404
        assert client.get("/i18n/xx.js").status_code == 404


def test_english_shell_loads_no_catalog(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        page = client.get("/home")
        assert page.status_code == 200
        assert "/i18n/" not in page.text
        assert "window.localplaudT = message => (window.localplaudCatalog || {})[message] || message;" in page.text


def test_shell_scripts_are_deferred_in_dependency_order(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        page = client.get("/home").text
        htmx = page.index('<script src="/static/htmx-1.9.12.min.js?v=')
        app_js = page.index('<script src="/static/js/app.js?v=')
        assert htmx < app_js
        assert re.search(r'<script src="/static/js/app\.js\?v=[0-9a-f]+" defer></script>', page)
        # Inline helpers that body scripts read synchronously stay before the deferred code.
        assert page.index("window.localplaudModal") < app_js


def test_recording_scripts_defer_after_the_shared_runtime(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        _seed()
        page = client.get("/file/r1").text
        order = [
            page.index('/static/js/app.js?v='),
            page.index('/static/recording-share.js?v='),
            page.index('/static/js/workspace.js?v='),
            page.index('/static/js/surfaces.js?v='),
        ]
        assert order == sorted(order)
        for name in ("workspace", "note-editor", "speaker-suggestions"):
            assert re.search(rf'<script src="/static/js/{name}\.js\?v=[0-9a-f]+" defer></script>', page)


def test_template_card_focus_ring_has_a_fallback_without_has_selector():
    css = (STATIC / "css" / "surfaces.css").read_text()
    fallback = css.index(".sf-tcard-open:focus-visible { outline:2px solid")
    supports = css.index("@supports selector(:has(*))")
    assert fallback < supports
    # Removing the title's own ring is only allowed inside the :has() guard.
    unguarded = css[:supports]
    assert ".sf-tcard-open:focus-visible { outline:none; }" not in unguarded


def test_status_pills_meet_the_12px_type_floor():
    app_css = (STATIC / "css" / "app.css").read_text()
    recording_css = (STATIC / "css" / "recording.css").read_text()
    assert re.search(r"\n\.st \{[^}]*font-size: 12px;", app_css)
    assert ".file-row .row-sub .state { font-size: 12px; }" in app_css
    assert re.search(r"\.ws-head \.recording-meta \.st \{[^}]*font-size: 12px;", recording_css)
