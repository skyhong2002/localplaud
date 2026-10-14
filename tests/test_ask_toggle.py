"""Ask is hidden until the workspace turns it on in Settings."""

from __future__ import annotations

from tests.test_web_ui import _client, _seed
from tests.workspace import set_ask_enabled

ASK_ENTRY_POINTS = (
    'id="open-ask-dock"',
    'id="recording-tab-ask"',
    'id="recording-panel-ask"',
    'class="ws-ask-pin"',
)


def test_ask_is_off_by_default_and_hidden_everywhere(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()

    assert client.get("/api/preferences/workspace").json()["ask_enabled"] is False

    recording = client.get("/file/r1?workspace=true").text
    for marker in ASK_ENTRY_POINTS:
        assert marker not in recording
    assert "data-ask-enabled" not in recording
    sidebar = recording.split('aria-label="Personal"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/ask"' not in sidebar

    home = client.get("/home").text
    assert 'data-home-card="ask"' not in home
    assert 'class="home-cards home-cards-2"' in home

    search = client.get("/search?q=team").text
    assert 'class="sf-search-ask"' not in search
    assert "Ask the library" not in search

    # An old ?tab=ask link lands on notes instead of an empty workspace.
    old_link = client.get("/file/r1?workspace=true&tab=ask").text
    assert (
        'id="recording-tab-notes" role="tab" aria-controls="recording-panel-notes" aria-selected="true"'
        in old_link
    )

    # The library Ask page stays reachable by URL; only the entry points hide.
    assert client.get("/ask").status_code == 200


def test_turning_ask_on_restores_every_entry_point(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    set_ask_enabled(client)

    recording = client.get("/file/r1?workspace=true").text
    for marker in ASK_ENTRY_POINTS:
        assert marker in recording
    assert "data-ask-enabled" in recording
    sidebar = recording.split('aria-label="Personal"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/ask"' in sidebar

    home = client.get("/home").text
    assert 'data-home-card="ask"' in home
    assert "home-cards-2" not in home

    assert 'class="sf-search-ask"' in client.get("/search?q=team").text

    set_ask_enabled(client, False)
    assert 'id="open-ask-dock"' not in client.get("/file/r1?workspace=true").text


def test_settings_offers_the_ask_toggle_on_desktop_and_phone(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    page = client.get("/settings").text
    assert 'name="ask_enabled" >' in page
    assert "ask_enabled:data.get('ask_enabled')==='on'" in page
    assert 'data-sf-pref-toggle="ask_enabled" data-sf-pref-reload >' in page

    set_ask_enabled(client)
    page = client.get("/settings").text
    assert 'name="ask_enabled" checked' in page
    assert 'data-sf-pref-toggle="ask_enabled" data-sf-pref-reload checked' in page
