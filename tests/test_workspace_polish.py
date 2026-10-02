"""Static regressions for recording-workspace QA polish (W4, W6, W7, W9)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "localplaud" / "api"
DETAIL = (ROOT / "templates" / "detail.html").read_text()
RECORDING_CSS = (ROOT / "static" / "css" / "recording.css").read_text()


def test_keyboard_help_button_uses_sprite_icon_and_tooltip():
    start = DETAIL.index('id="player-shortcuts"')
    button = DETAIL[start : DETAIL.index("</button>", start)]
    assert "icon('keyboard')" in button
    assert "data-tooltip=" in button
    assert ">?<" not in button


def test_toast_clears_docked_player_on_desktop_and_phone():
    assert (
        "body:has(#persistent-player.ws-player) .toast-region { bottom: calc(var(--ws-player-h)"
        in RECORDING_CSS
    )
    assert "body.has-tabbar:has(#persistent-player.ws-player) .toast-region" in RECORDING_CSS


def test_mind_map_toolbar_labels_do_not_wrap_inside_buttons():
    assert ".mindmap-zoom .icon-action { white-space: nowrap; }" in RECORDING_CSS


def test_phone_hides_purposeless_file_list_toggle():
    phone = RECORDING_CSS[RECORDING_CSS.index(".recording-shell .detail-body > .filelist { display: none; }") :]
    assert phone.index('.ws-topbar [data-collapse-toggle="list"] { display: none; }') < 300


def test_workspace_segment_actions_use_sprite_not_mask_icons():
    assert "nav-icon" not in DETAIL


def test_notes_have_one_tab_row_without_a_visible_generate_disclosure(monkeypatch, tmp_path):
    import re

    from tests.test_web_ui import _client, _seed

    client = _client(monkeypatch, tmp_path)
    _seed()
    html = client.get("/file/r1?tab=notes").text
    disclosure = re.search(r'<details[^>]+id="note-generation-settings"[^>]*>', html)
    assert disclosure and "hidden" in disclosure.group() and "open" not in disclosure.group()
    assert html.count('class="ws-tabrow"') == 1
    # Generation stays reachable from the tab row's + menu.
    tabrow = html.split('class="ws-tabrow"', 1)[1].split('id="recording-panel-notes"', 1)[0]
    assert "data-open-note-generation" in tabrow
    assert "data-open-note-dialog" in html
