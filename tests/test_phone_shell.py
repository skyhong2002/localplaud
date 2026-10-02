"""Phone shell: relative list dates, Plaud-app rows, Explore hub, add sheet, player hooks."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.assets import with_assets


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'phone.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def test_relative_datetime_matches_plaud_app_wording_in_both_locales():
    from localplaud.api.ui import dur_short, relative_datetime

    now = datetime(2026, 10, 2, 11, 30, tzinfo=UTC)
    taipei = {"timezone": "Asia/Taipei", "now": now}
    assert relative_datetime(_ms("2026-10-02T09:05:00+08:00"), **taipei) == "Today at 09:05"
    assert relative_datetime(_ms("2026-10-01T22:27:00+08:00"), **taipei) == "Yesterday at 22:27"
    assert relative_datetime(_ms("2026-09-30T21:38:00+08:00"), **taipei) == "30 Sep at 21:38"
    assert relative_datetime(_ms("2025-09-30T21:38:00+08:00"), **taipei) == "30 Sep 2025"
    zh = taipei | {"locale": "zh-Hant-TW"}
    assert relative_datetime(_ms("2026-10-01T22:27:00+08:00"), **zh) == "昨天 22:27"
    assert relative_datetime(_ms("2026-09-30T21:38:00+08:00"), **zh) == "9月30日 21:38"
    assert relative_datetime(_ms("2025-09-30T21:38:00+08:00"), **zh) == "2025年9月30日"
    # The calendar day is the workspace's, not UTC's: 00:30 Taipei is still "today".
    assert relative_datetime(_ms("2026-10-02T00:30:00+08:00"), **taipei) == "Today at 00:30"
    assert relative_datetime(None) == ""
    assert (dur_short(4_928_000), dur_short(480_000), dur_short(45_000)) == ("1h 22m", "8m", "45s")


def test_phone_rows_explore_and_add_sheet_render(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="p1", filename="Weekly Sync", status=FileStatus.done,
                              duration_ms=4_928_000, start_time_ms=_ms("2026-09-30T21:38:00+08:00")))
        session.add(PlaudFile(id="p2", filename="Broken", status=FileStatus.error,
                              duration_ms=60_000, start_time_ms=_ms("2026-09-29T08:00:00+08:00")))

    files = c.get("/?workspace=true")
    row = files.text.split('data-file-id="p1"', 1)[1].split("</li>", 1)[0]
    assert '<span class="row-meta-mobile">' in row and "1h 22m" in row
    assert 'class="row-sep" aria-hidden="true"' in row
    # Phone top bar: processing status (opens the drawer), AI search, avatar -> Settings.
    assert 'class="m-topbar m-root-topbar"' in files.text
    assert 'aria-label="Search and Ask"' in files.text and '<use href="#i-search-ai">' in files.text
    assert 'class="m-status-level m-status-attention"' in files.text
    # Unlabelled 3-slot tab bar: Files, "+" bottom sheet, Explore.
    assert 'class="m-plus" type="button" data-dialog-open="add-sheet"' in files.text
    assert 'href="/explore"' in files.text and 'id="add-sheet"' in files.text
    assert "data-add-file" in files.text and "data-add-sync" in files.text

    explore = c.get("/explore")
    assert explore.status_code == 200
    assert 'class="explore-hero"' in explore.text and 'href="/status"' in explore.text
    assert 'class="m-tab" href="/explore" aria-current="page"' in explore.text
    for target in ("/templates", "/discover", "/settings#plaud-account", "/settings#private-backup"):
        assert f'href="{target}"' in explore.text
    # Explore is a hub, not a plan/upsell surface.
    assert "Upgrade" not in explore.text and "Unlock" not in explore.text

    shell = with_assets(c, files)
    assert "lp.player = (() => {" in shell and "lp.rowAction = (button, action) =>" in shell
    assert "window.addEventListener('popstate', park);" in shell
    assert "overscroll-behavior-y: contain" in shell
    assert 'id="lp-mini-player"' in files.text and 'id="lp-audio-host" hidden' in files.text


def test_workspace_timestamp_handles_hour_boundaries():
    from localplaud.api.app import _mmss

    for seconds, expected in (
        (None, ""), (-1, "0:00"), (0, "0:00"), (59, "0:59"),
        (3599, "59:59"), (3600, "1:00:00"), (3789, "1:03:09"),
        (7203.9, "2:00:03"),
    ):
        assert _mmss(seconds) == expected


def test_parked_audio_preserves_playback_without_duplicate_workspace_id():
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the player lifecycle")
    script = Path(__file__).parents[1] / "src/localplaud/api/static/js/app.js"
    harness = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('  lp.player = (() => {');
const end = source.indexOf('  /* ---------------------------------------------------------- mini player */', start);
const host = {append(el) {el.inView = false;}};
const ctx = {lp: {}, URL, WeakSet, Set, navigator: {}, location: {href:'http://localhost/'},
  window: {addEventListener(){}}, document: {title:'Demo',addEventListener(){},getElementById(){return host;}}};
vm.runInNewContext(source.slice(start, end),ctx);
function element(id) {return {id:'player',className:'',inView:true,paused:false,currentTime:60,
  _src:'/audio/'+id, handlers:{}, addEventListener(t,f){this.handlers[t]=f;},
  getAttribute(k){return k==='src'?this._src:null;},removeAttribute(k){if(k==='id')this.id='';},
  closest(){return this.inView?{}:null;},cloneNode(){return element(id);},replaceWith(x){this.inView=false;x.inView=true;},
  pause(){this.paused=true;},remove(){this.inView=false;}};}
const a=element('a');ctx.lp.player.attach(a,{fileId:'a'});ctx.lp.player.park();
assert.equal(a.id,'');assert.equal(a.paused,false);assert.equal(a.currentTime,60);
const b=element('b');b.paused=true;b.currentTime=0;ctx.lp.player.attach(b,{fileId:'b'});
assert.equal(ctx.lp.player.info.fileId,'a');assert.equal(b.id,'player');
const returned=element('a');assert.equal(ctx.lp.player.attach(returned,{fileId:'a'}),a);
assert.equal(a.id,'player');assert.equal(a.paused,false);assert.equal(a.currentTime,60);
const previousTitle=ctx.lp.player.info.title;
ctx.lp.player.updateTitle('b','Other recording');
assert.equal(ctx.lp.player.info.title,previousTitle);
ctx.lp.player.updateTitle('a','Sky meeting');
assert.equal(ctx.lp.player.info.title,'Sky meeting');
assert.equal(a._lpInfo.title,'Sky meeting');
assert.equal(a.paused,false);assert.equal(a.currentTime,60);
"""
    subprocess.run([node, "-e", harness, str(script)], check=True, capture_output=True, text=True)
