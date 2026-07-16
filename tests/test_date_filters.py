"""Workspace calendar filters keep exact, reproducible UTC boundaries."""

from __future__ import annotations

import pytest

from localplaud.date_filters import resolve_date_scope


def test_taipei_calendar_range_resolves_to_exact_inclusive_days():
    assert resolve_date_scope("2026-07-01", "2026-07-31", "Asia/Taipei") == {
        "scope_version": 2,
        "date_timezone": "Asia/Taipei",
        "date_from": "2026-07-01",
        "date_from_ms": 1_782_835_200_000,
        "date_to": "2026-07-31",
        "date_to_ms_exclusive": 1_785_513_600_000,
    }


def test_new_york_calendar_days_follow_dst_23_and_25_hour_boundaries():
    spring = resolve_date_scope("2026-03-08", "2026-03-08", "America/New_York")
    fall = resolve_date_scope("2026-11-01", "2026-11-01", "America/New_York")
    assert spring["date_from_ms"] == 1_772_946_000_000
    assert spring["date_to_ms_exclusive"] == 1_773_028_800_000
    assert spring["date_to_ms_exclusive"] - spring["date_from_ms"] == 23 * 60 * 60 * 1000
    assert fall["date_from_ms"] == 1_793_505_600_000
    assert fall["date_to_ms_exclusive"] == 1_793_595_600_000
    assert fall["date_to_ms_exclusive"] - fall["date_from_ms"] == 25 * 60 * 60 * 1000


@pytest.mark.parametrize(
    ("date_from", "date_to", "timezone"),
    [
        ("0001-01-01", None, "Etc/GMT-14"),
        (None, "9999-12-31", "Etc/GMT+12"),
        ("2026-07-02", "2026-07-01", "UTC"),
        ("2026-07-01", None, "Not/A_Timezone"),
    ],
)
def test_invalid_calendar_scope_is_rejected(date_from, date_to, timezone):
    with pytest.raises(ValueError):
        resolve_date_scope(date_from, date_to, timezone)


def test_nearest_supported_dates_convert_at_extreme_offsets():
    assert resolve_date_scope("0001-01-02", None, "Etc/GMT-14")["date_from_ms"] < 0
    assert (
        resolve_date_scope(None, "9999-12-30", "Etc/GMT+12")["date_to_ms_exclusive"]
        > 0
    )
