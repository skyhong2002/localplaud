"""Read-only Calendar view: workspace-timezone day counts that match Files filters."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape

import pytest


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'cal.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).astimezone(UTC).timestamp() * 1000)


def _set_preferences(client, **changes) -> None:
    preferences = client.get("/api/preferences/workspace").json()
    response = client.put("/api/preferences/workspace", json=preferences | changes)
    assert response.status_code == 200


def _seed(rows: dict[str, tuple[str | None, bool]]) -> None:
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            PlaudFile(
                id=file_id,
                filename=f"Synthetic {file_id}",
                status=FileStatus.done,
                start_time_ms=_ms(timestamp) if timestamp else None,
                duration_ms=60_000,
                is_trash=trash,
            )
            for file_id, (timestamp, trash) in rows.items()
        )


def _counts(html: str) -> dict[str, int]:
    return {
        day: int(count)
        for day, count in re.findall(r'data-date="([\d-]+)" data-count="(\d+)"', html)
        if int(count)
    }


def _file_ids(client, query: str) -> list[str]:
    return sorted(f["id"] for f in client.get(f"/api/files{query}").json()["files"])


# Taipei is UTC+8: 2026-07-15T16:00Z is the first instant of local 2026-07-16.
TAIPEI_ROWS = {
    "utc-15-local-15": ("2026-07-15T15:59:59.999+00:00", False),
    "utc-15-local-16": ("2026-07-15T16:00:00+00:00", False),
    "utc-16-local-16": ("2026-07-16T15:59:59.999+00:00", False),
    "utc-16-local-17": ("2026-07-16T16:00:00+00:00", False),
    "trashed-local-16": ("2026-07-16T03:00:00+00:00", True),
    "june-utc-local-july": ("2026-06-30T16:30:00+00:00", False),
    "july-utc-local-august": ("2026-07-31T16:30:00+00:00", False),
    "no-start-time": (None, False),
}


def test_counts_use_workspace_timezone_and_exclude_trash(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _set_preferences(client, timezone="Asia/Taipei")
    _seed(TAIPEI_ROWS)

    page = client.get("/calendar?month=2026-07")
    assert page.status_code == 200
    counts = _counts(page.text)
    assert counts == {"2026-07-01": 1, "2026-07-15": 1, "2026-07-16": 2, "2026-07-17": 1}
    assert 'data-cal-total="5"' in page.text
    # The trashed row lands on local 07-16 but is not counted.
    assert "trashed" not in page.text


def test_same_rows_in_utc_shift_across_the_day_and_month_boundary(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _set_preferences(client, timezone="UTC")
    _seed(TAIPEI_ROWS)

    july = _counts(client.get("/calendar?month=2026-07").text)
    assert july == {"2026-07-15": 2, "2026-07-16": 2, "2026-07-31": 1}
    assert _counts(client.get("/calendar?month=2026-06").text) == {"2026-06-30": 1}


def test_day_links_open_files_with_matching_non_trash_rows(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _set_preferences(client, timezone="Asia/Taipei")
    _seed(TAIPEI_ROWS)

    html = client.get("/calendar?month=2026-07").text
    links = dict(
        re.findall(r'data-date="([\d-]+)" data-count="\d+"><a class="cal-day" href="([^"]+)"', html)
    )
    assert set(links) == {"2026-07-01", "2026-07-15", "2026-07-16", "2026-07-17"}
    for day, href in links.items():
        href = unescape(href)
        assert href == f"/?date_from={day}&date_to={day}"
        assert len(_file_ids(client, href[1:])) == _counts(html)[day]
    assert _file_ids(client, "?date_from=2026-07-16&date_to=2026-07-16") == [
        "utc-15-local-16",
        "utc-16-local-16",
    ]
    # Empty days are not links, so the user never lands on an empty Files view.
    assert 'data-date="2026-07-02" data-count="0"><span class="cal-day"' in html


def test_dst_day_counts_follow_local_midnight(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _set_preferences(client, timezone="America/New_York")
    _seed(
        {
            "before": ("2026-03-08T04:59:59+00:00", False),  # 03-07 23:59:59 EST
            "start": ("2026-03-08T05:00:00+00:00", False),  # 03-08 00:00 EST
            "end": ("2026-03-09T03:59:59+00:00", False),  # 03-08 23:59:59 EDT
            "after": ("2026-03-09T04:00:00+00:00", False),  # 03-09 00:00 EDT
        }
    )
    counts = _counts(client.get("/calendar?month=2026-03").text)
    assert counts == {"2026-03-07": 1, "2026-03-08": 2, "2026-03-09": 1}


def test_navigation_today_and_invalid_month(monkeypatch, tmp_path):
    import localplaud.library_calendar as library_calendar

    monkeypatch.setattr(library_calendar, "workspace_today", lambda _tz: date(2026, 11, 20))
    client = _client(monkeypatch, tmp_path)

    current = client.get("/calendar").text
    assert 'data-calendar-month="2026-11"' in current
    assert 'href="/calendar?month=2026-10"' in current
    assert 'href="/calendar?month=2026-12"' in current
    assert 'data-cal-today' not in current  # already on the current month
    assert re.search(r'data-date="2026-11-20"[^>]*><span class="cal-day" aria-current="date"', current)

    january = client.get("/calendar?month=2027-01").text
    assert 'href="/calendar?month=2026-12"' in january
    assert 'href="/calendar?month=2027-02"' in january
    assert 'href="/calendar?month=2026-11" data-cal-today' in january
    assert 'aria-current="date"' not in january

    for bad in ("2026-13", "nope", "0001-01", "9999-12"):
        page = client.get(f"/calendar?month={bad}")
        assert page.status_code == 200
        assert 'data-calendar-month="2026-11"' in page.text
        assert 'role="status"' in page.text


@pytest.mark.parametrize(("month", "missing"), [("0001-02", "rel=\"prev\""), ("9999-11", "rel=\"next\"")])
def test_supported_range_edges_hide_out_of_range_navigation(monkeypatch, tmp_path, month, missing):
    client = _client(monkeypatch, tmp_path)
    _set_preferences(client, timezone="Etc/GMT-14")
    page = client.get(f"/calendar?month={month}")
    assert page.status_code == 200
    assert missing not in page.text


def test_zh_tw_strings_accessible_semantics_and_home_entry(monkeypatch, tmp_path):
    import localplaud.library_calendar as library_calendar

    monkeypatch.setattr(library_calendar, "workspace_today", lambda _tz: date(2026, 7, 16))
    client = _client(monkeypatch, tmp_path)
    _set_preferences(client, timezone="Asia/Taipei", locale="zh-Hant-TW")
    _seed(TAIPEI_ROWS)

    home = client.get("/home").text
    entry = re.search(r"<a\b[^>]*\bdata-home-calendar\b[^>]*>", home)
    assert entry and 'href="/calendar"' in entry.group(0)
    assert 'aria-label="開啟錄音行事曆"' in entry.group(0)
    assert "行事曆" in home
    assert "home-calendar-label" in home

    html = client.get("/calendar").text
    assert "<title>行事曆 — localplaud</title>" in html
    assert "2026 年 7 月" in html
    assert 'aria-label="上個月"' in html and 'aria-label="下個月"' in html
    assert "本月" in html
    assert '<th scope="col" abbr="星期日">' in html
    assert re.search(r"<caption[^>]*>", html)
    assert 'aria-label="2026 年 7 月 16 日 星期四，2 份錄音" aria-current="date"' in html
    assert "2026 年 7 月 2 日 星期四，沒有錄音" in html
    assert 'href="/static/css/calendar.css?v=' in html


def test_english_labels_and_narrow_layout_rules(monkeypatch, tmp_path):
    from tests.assets import with_assets

    client = _client(monkeypatch, tmp_path)
    _set_preferences(client, timezone="UTC")
    _seed({"one": ("2026-07-16T12:00:00+00:00", False)})
    page = client.get("/calendar?month=2026-07")
    assert 'aria-label="Thursday, July 16, 2026, 1 recording"' in page.text
    assert "July 2026" in page.text
    combined = with_assets(client, page)
    assert re.search(r"@media \(max-width: 600px\) \{[^@]*\.cal-day \{[^}]*min-height: 52px", combined)
    assert ".cal-grid {" in combined and "table-layout: fixed" in combined


def test_counts_stay_inside_the_active_private_workspace(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from localplaud.db.models import Base, PlaudFile, Workspace
    from localplaud.db.tenancy import local_file_id, system_scope, workspace_scope
    from localplaud.library_calendar import day_counts

    engine = create_engine(f"sqlite:///{tmp_path / 'tenancy.db'}")
    Base.metadata.create_all(engine)
    with system_scope(), Session(engine) as session:
        session.add(Workspace(id=2, name="other"))
        session.commit()
    seeds = {1: ["mine"], 2: [local_file_id(2, "theirs-a"), local_file_id(2, "theirs-b")]}
    for workspace_id, file_ids in seeds.items():
        with workspace_scope(workspace_id), Session(engine) as session:
            session.add_all(
                PlaudFile(
                    id=file_id,
                    filename=file_id,
                    start_time_ms=_ms("2026-07-16T04:00:00+00:00"),
                )
                for file_id in file_ids
            )
            session.commit()
    for workspace_id, expected in ((1, 1), (2, 2)):
        with workspace_scope(workspace_id), Session(engine) as session:
            assert day_counts(session, 2026, 7, "Asia/Taipei") == {date(2026, 7, 16): expected}
    engine.dispose()
