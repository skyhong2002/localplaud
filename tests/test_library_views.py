"""Tests for library sorting, filtering, and trash/uncategorized views."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, urlsplit


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'lib.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed():
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    # Mixed names, durations, scenes, statuses, and one trashed row.
    rows = [
        # id, filename, status, duration_ms, start_time_ms, scene, is_trash
        ("a", "Alpha meeting", FileStatus.done, 300000, 1000, 1, False),
        ("b", "Bravo call", FileStatus.error, 900000, 2000, 1, False),
        ("c", "Charlie note", FileStatus.partial, 120000, 3000, 2, False),
        ("d", "Delta memo", FileStatus.processing, 600000, 4000, 2, False),
        ("t", "Trashed thing", FileStatus.done, 60000, 5000, 1, True),
    ]
    with session_scope() as s:
        for fid, name, st, dur, start, scene, trash in rows:
            s.add(
                PlaudFile(
                    id=fid,
                    filename=name,
                    status=st,
                    duration_ms=dur,
                    start_time_ms=start,
                    scene=scene,
                    is_trash=trash,
                )
            )


def _ids(client, query=""):
    return [f["id"] for f in client.get(f"/api/files{query}").json()["files"]]


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).astimezone(UTC).timestamp() * 1000)


def _set_timezone(client, timezone: str) -> None:
    preferences = client.get("/api/preferences/workspace").json()
    response = client.put("/api/preferences/workspace", json=preferences | {"timezone": timezone})
    assert response.status_code == 200


def test_default_excludes_trash_and_sorts_recent_desc(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    ids = _ids(c)
    assert "t" not in ids  # trash excluded by default
    # newest start_time_ms first
    assert ids == ["d", "c", "b", "a"]


def test_sort_name_asc_and_desc(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert _ids(c, "?sort=name&dir=asc") == ["a", "b", "c", "d"]
    assert _ids(c, "?sort=name&dir=desc") == ["d", "c", "b", "a"]


def test_sort_duration(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    # durations: c=120k, a=300k, d=600k, b=900k
    assert _ids(c, "?sort=duration&dir=asc") == ["c", "a", "d", "b"]
    assert _ids(c, "?sort=duration&dir=desc") == ["b", "d", "a", "c"]


def test_state_filter(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert _ids(c, "?state=error") == ["b"]
    assert _ids(c, "?state=done") == ["a"]  # trashed 't' also done but excluded


def test_scene_filter(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert set(_ids(c, "?scene=2")) == {"c", "d"}
    assert set(_ids(c, "?scene=1")) == {"a", "b"}  # 't' scene 1 but trashed


def test_trash_view_inclusion(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert _ids(c, "?view=trash") == ["t"]


def test_query_combines_with_filter(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    # q matches Bravo (error) and would also match nothing else in scene 2
    assert _ids(c, "?q=bravo&state=error") == ["b"]
    assert _ids(c, "?q=note&scene=2") == ["c"]


def test_invalid_params_fall_back(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    # bogus sort/dir/state/scene/view must not 500 and behave like defaults
    r = c.get("/api/files?sort=bogus&dir=sideways&state=nope&scene=abc&view=weird")
    assert r.status_code == 200
    assert [f["id"] for f in r.json()["files"]] == ["d", "c", "b", "a"]


def test_date_filter_uses_workspace_timezone_and_inclusive_days(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    _set_timezone(c, "Asia/Taipei")
    rows = {
        "before": "2026-07-15T15:59:59+00:00",
        "lower": "2026-07-15T16:00:00+00:00",
        "upper": "2026-07-16T15:59:59.999+00:00",
        "after": "2026-07-16T16:00:00+00:00",
    }
    with session_scope() as session:
        session.add_all(
            PlaudFile(
                id=file_id,
                filename=file_id,
                status=FileStatus.done,
                start_time_ms=_ms(timestamp),
                duration_ms=60_000,
            )
            for file_id, timestamp in rows.items()
        )

    assert _ids(c, "?date_from=2026-07-16&date_to=2026-07-16") == [
        "upper",
        "lower",
    ]


def test_date_filter_respects_dst_local_day_boundary(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    _set_timezone(c, "America/New_York")
    rows = {
        "before": "2026-03-08T04:59:59+00:00",
        "lower": "2026-03-08T05:00:00+00:00",
        "upper": "2026-03-09T03:59:59.999+00:00",
        "after": "2026-03-09T04:00:00+00:00",
    }
    with session_scope() as session:
        session.add_all(
            PlaudFile(
                id=file_id,
                filename=file_id,
                status=FileStatus.done,
                start_time_ms=_ms(timestamp),
                duration_ms=60_000,
            )
            for file_id, timestamp in rows.items()
        )

    assert _ids(c, "?date_from=2026-03-08&date_to=2026-03-08") == [
        "upper",
        "lower",
    ]


def test_duration_filter_is_inclusive_and_excludes_unknown_duration(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="zero",
                    filename="Zero",
                    status=FileStatus.done,
                    start_time_ms=6_000,
                    duration_ms=0,
                ),
                PlaudFile(
                    id="decimal",
                    filename="Decimal",
                    status=FileStatus.done,
                    start_time_ms=7_000,
                    duration_ms=90_000,
                ),
                PlaudFile(
                    id="unknown",
                    filename="Unknown",
                    status=FileStatus.done,
                    start_time_ms=8_000,
                    duration_ms=None,
                ),
            ]
        )

    assert _ids(c, "?min_duration_minutes=2&max_duration_minutes=10") == ["d", "c", "a"]
    assert _ids(c, "?min_duration_minutes=1.5&max_duration_minutes=1.5") == ["decimal"]
    assert _ids(c, "?max_duration_minutes=0") == ["zero"]


def test_invalid_ranges_are_safe_and_visible(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()

    assert _ids(c, "?date_from=bad&min_duration_minutes=nan") == ["d", "c", "b", "a"]
    assert _ids(c, "?min_duration_minutes=-1&max_duration_minutes=inf") == [
        "d",
        "c",
        "b",
        "a",
    ]
    assert _ids(c, "?date_from=2026-07-17&date_to=2026-07-16") == []
    assert _ids(c, "?min_duration_minutes=10&max_duration_minutes=2") == []

    date_page = c.get("/?date_from=2026-07-17&date_to=2026-07-16")
    duration_page = c.get("/?min_duration_minutes=10&max_duration_minutes=2")
    assert 'class="filter-menu" open' in date_page.text
    assert 'role="alert">Start date must not follow end date.' in date_page.text
    assert 'role="alert">Minimum duration must not exceed maximum duration.' in duration_page.text


def test_extreme_iso_dates_do_not_overflow_html_or_api(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()

    for query in ("date_from=0001-01-01", "date_to=9999-12-31"):
        api = c.get(f"/api/files?{query}")
        page = c.get(f"/?{query}")
        assert api.status_code == 200
        assert page.status_code == 200
        assert [item["id"] for item in api.json()["files"]] == ["d", "c", "b", "a"]


def test_unknown_source_legacy_plaud_and_uncategorized_counts_match(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, Tag
    from localplaud.db.session import session_scope

    with session_scope() as session:
        tag = Tag(name="Tagged")
        session.add(tag)
        session.flush()
        legacy = PlaudFile(
            id="legacy",
            filename="Legacy Plaud",
            status=FileStatus.done,
            start_time_ms=2_000,
            scene=None,
            origin=None,
        )
        tagged = PlaudFile(
            id="tagged",
            filename="Tagged only",
            status=FileStatus.done,
            start_time_ms=1_000,
            scene=3,
            origin="local",
        )
        tagged.tags.append(tag)
        session.add_all([legacy, tagged])

    assert _ids(c, "?scene=unknown") == ["legacy"]
    assert _ids(c, "?origin=plaud") == ["legacy"]
    assert _ids(c, "?view=uncategorized") == ["legacy"]
    page = c.get("/")
    assert "Unknown capture source" in page.text
    assert 'href="/?view=uncategorized"' in page.text
    assert ">1</span>" in page.text.split('href="/?view=uncategorized"', 1)[1].split("</a>", 1)[0]


def test_unknown_source_and_origin_facets_are_localized(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    preferences = c.get("/api/preferences/workspace").json()
    assert c.put(
        "/api/preferences/workspace",
        json=preferences | {"locale": "zh-Hant-TW"},
    ).status_code == 200
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="unknown-source",
                filename="Unknown source",
                status=FileStatus.done,
                scene=None,
                origin=None,
            )
        )

    page = c.get("/")
    source_filters = page.text.split('<span class="lbl">來源</span>', 1)[1].split(
        "</div>", 1
    )[0]
    origin_filters = page.text.split('<span class="lbl">來源類型</span>', 1)[1].split(
        "</div>", 1
    )[0]
    assert "未知錄製來源" in source_filters
    assert ">Unknown capture source" not in source_filters
    assert "Plaud 雲端" in origin_filters


def test_literal_wildcards_do_not_expand_title_search(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(id="percent", filename="100% ready", status=FileStatus.done),
                PlaudFile(id="underscore", filename="plan_v2", status=FileStatus.done),
                PlaudFile(id="plain", filename="ordinary", status=FileStatus.done),
            ]
        )

    assert _ids(c, "?q=%25") == ["percent"]
    assert _ids(c, "?q=_") == ["underscore"]


def test_index_page_renders_table_and_controls(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/")
    assert r.status_code == 200
    assert "rectable" in r.text  # sortable table present
    assert ".table-wrap { border:0;overflow:visible; }" in r.text
    assert ".select-cell input:focus-visible { opacity:1; }" in r.text
    assert "Bravo call" in r.text
    assert "Trash" in r.text  # trash view link
    assert "Capture source 1" in r.text  # capture-source facet
    # Sidebar source items stay distinguishable at rail width: short visible
    # label, full label preserved on the link for hover/assistive context.
    assert ">Source 1</span>" in r.text
    assert 'title="Capture source 1"' in r.text
    # Mobile rows are title-first cards: no repeated per-cell column labels,
    # duration and recorded date collapse into one muted meta line.
    assert "content:attr(data-label)" not in r.text
    assert 'data-label="Name"' not in r.text
    assert '<td class="num dur-cell">' in r.text
    assert '<td class="num rec-cell">' in r.text
    assert ".rectable tbody td.rec-cell::before { content:'· '; }" in r.text
    assert ".rectable .nm { max-width:none; text-align:left;" in r.text
    assert 'id="app-view" hx-history-elt' in r.text
    assert 'id="recording-file-list"' not in r.text
    assert 'href="/file/b?return_to=%2F"' in r.text
    assert 'class="filter-close"' in r.text
    assert "event.key === 'Escape' && filterMenu?.open" in r.text
    assert "filterReturnFocus?.isConnected ? filterReturnFocus : filterTrigger" in r.text
    assert 'class="file-subline"><span>error</span>' in r.text
    assert 'name="date_from"' in r.text and 'name="date_to"' in r.text
    assert 'name="min_duration_minutes"' in r.text
    assert 'name="max_duration_minutes"' in r.text


def test_filter_urls_and_detail_filelist_preserve_range_state(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    query = (
        "sort=name&dir=asc&state=done&scene=1&date_from=1970-01-01&"
        "date_to=1970-01-01&min_duration_minutes=2&max_duration_minutes=5"
    )
    page = c.get(f"/?{query}")
    assert page.status_code == 200
    assert "Alpha meeting" in page.text and "Bravo call" not in page.text

    hrefs = [unescape(value) for value in re.findall(r'href="([^"]+)"', page.text)]
    duration_sort = next(value for value in hrefs if "sort=duration" in value)
    duration_params = parse_qs(urlsplit(duration_sort).query)
    expected = {
        "state": ["done"],
        "scene": ["1"],
        "date_from": ["1970-01-01"],
        "date_to": ["1970-01-01"],
        "min_duration_minutes": ["2.0"],
        "max_duration_minutes": ["5.0"],
    }
    for key, value in expected.items():
        assert duration_params[key] == value

    clear_match = re.search(r'class="range-clear" href="([^"]+)"', page.text)
    assert clear_match is not None
    clear_ranges = unescape(clear_match.group(1))
    clear_params = parse_qs(urlsplit(clear_ranges).query)
    assert clear_params["state"] == ["done"] and clear_params["scene"] == ["1"]

    return_to = f"/?{query}"
    detail = c.get("/file/a", params={"return_to": return_to, "tab": "notes"})
    assert detail.status_code == 200
    assert 'data-recording-id="a"' in detail.text
    assert 'data-recording-id="b"' not in detail.text
    assert "tab=notes" in detail.text


def test_library_paginates_without_truncating_ask_scope(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            PlaudFile(
                id=f"bulk-{index:03d}",
                filename=f"Bulk recording {index:03d}",
                status=FileStatus.done,
                start_time_ms=10_000 + index,
            )
            for index in range(101)
        )

    first = c.get("/")
    second = c.get("/?page=2")
    ask = c.get("/?ask=true")
    assert first.text.count('class="row-select"') == 100
    assert ".quickadd { min-width:0;flex-basis:100%;margin-left:0; }" in first.text
    assert ".rectable tbody td.name-cell { padding:0 44px 3px 12px!important; }" in first.text
    assert ".rectable tbody td.select-cell ~ td.name-cell { padding-left:40px!important; }" in first.text
    assert "Page 1 / 2" in first.text and "Bulk recording 100" in first.text
    assert "Alpha meeting" not in first.text
    assert "Page 2 / 2" in second.text and "Alpha meeting" in second.text
    assert "Bulk recording 100" in ask.text and "Alpha meeting" in ask.text

    detail = c.get("/file/a", params={"return_to": "/?page=1", "tab": "mindmap"})
    assert detail.status_code == 200
    assert detail.text.count('data-recording-id="') == 101
    assert 'data-recording-id="a"' in detail.text
    assert "Selected recording" in detail.text
    assert 'class="card on pinned-recording"' in detail.text
    assert 'aria-label="Recording list pages"' in detail.text
    assert '<span class="fl-page-label">1 / 2</span>' in detail.text
    assert "data-replace-filelist" in detail.text
    assert "return_to=%2F%3Fpage%3D2&amp;tab=mindmap" in detail.text
    assert "!trigger?.closest?.('[data-replace-filelist]')" in detail.text


def test_index_trash_view_shows_banner(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/?view=trash")
    assert r.status_code == 200
    assert "Trashed thing" in r.text
    assert "read-only recovery view" in r.text
    assert "never deletes" in r.text


def test_index_invalid_params_ok(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/?sort=xyz&dir=nope&state=bad&scene=notint&view=??")
    assert r.status_code == 200


def test_state_aliases_match_ops_card_buckets_exactly(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    statuses = {
        "s-done": FileStatus.done,
        "s-error": FileStatus.error,
        "s-partial": FileStatus.partial,
        "s-processing": FileStatus.processing,
        "s-downloading": FileStatus.downloading,
        "s-downloaded": FileStatus.downloaded,
        "s-discovered": FileStatus.discovered,
        "s-metadata": FileStatus.metadata_only,
    }
    with session_scope() as s:
        for rid, status in statuses.items():
            s.add(PlaudFile(id=rid, filename=rid, status=status,
                            duration_ms=1000, start_time_ms=0))

    def ids(state):
        return {f["id"] for f in c.get(f"/api/files?state={state}").json()["files"]}

    # Each aggregate alias filters exactly the statuses its displayed count
    # sums, so a clicked number always lands on that many rows. discovered is
    # queued for automatic download, so it is pending work, not manual import.
    assert ids("generating") == {"s-processing", "s-downloading", "s-downloaded", "s-discovered"}
    assert ids("attention") == {"s-error", "s-partial"}
    assert ids("cloud") == {"s-metadata"}
    assert ids("done") == {"s-done"}

    page = c.get("/")
    card = page.text.split('data-testid="ops-card"', 1)[1].split("</div>", 1)[0]
    assert '<a class="ops-stat" href="/?state=generating"><strong>4</strong> generating</a>' in card
    assert '<a class="ops-stat" href="/?state=attention"><strong class="ops-attn">2</strong> need attention</a>' in card
    assert '<a class="ops-stat" href="/?state=cloud"><strong>1</strong> in cloud</a>' in card
    assert '<a class="ops-stat" href="/?state=done"><strong>1</strong> ready</a>' in card


def test_ops_card_cloud_only_is_not_all_caught_up(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="c-done", filename="Done", status=FileStatus.done,
                        duration_ms=1000, start_time_ms=0))
        s.add(PlaudFile(id="c-cloud", filename="Cloud", status=FileStatus.metadata_only,
                        duration_ms=1000, start_time_ms=0))

    card = c.get("/").text.split('data-testid="ops-card"', 1)[1].split("</div>", 1)[0]
    assert "Cloud-only recordings await import" in card
    assert "All caught up" not in card
    assert '<a class="ops-stat" href="/?state=cloud"><strong>1</strong> in cloud</a>' in card


def test_discovered_only_workspace_counts_as_pending_not_manual_import(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="d-only", filename="Just discovered", status=FileStatus.discovered,
                        duration_ms=1000, start_time_ms=0))

    card = c.get("/").text.split('data-testid="ops-card"', 1)[1].split("</div>", 1)[0]
    # The poller downloads discovered rows automatically: pending, never
    # presented as awaiting a manual import.
    assert '<a class="ops-stat" href="/?state=generating"><strong>1</strong> generating</a>' in card
    assert "in cloud" not in card
    assert "Cloud-only recordings await import" not in card
    assert "View system status" in card


def test_home_generating_tile_matches_destination_with_trashed_pending_row(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="p-live", filename="Pending live", status=FileStatus.processing,
                        duration_ms=1000, start_time_ms=0))
        s.add(PlaudFile(id="p-trash", filename="Pending trashed", status=FileStatus.processing,
                        duration_ms=1000, start_time_ms=0, is_trash=True))

    destination_rows = len(c.get("/api/files?state=generating").json()["files"])
    home = c.get("/home").text
    tile = home.split('class="tile" href="/?state=generating"', 1)[1].split("</a>", 1)[0]
    import re as _re

    tile_count = int(_re.search(r'class="v">(\d+)<', tile).group(1))
    # The tile's number equals its linked destination: trashed pending rows
    # are excluded from both.
    assert destination_rows == 1
    assert tile_count == destination_rows
