"""
Tests for `_schedule_iso_central` — converts a date + wall-clock time into an
RFC3339 timestamp anchored to America/Chicago. Tests both CST and CDT, plus
12-hour AM/PM parsing.
"""

import pytest

from lib.captions import _schedule_iso_central


def test_cdt_summer_offset():
    """April 30 is CDT (UTC-5)."""
    iso = _schedule_iso_central("2026-04-30", "9:00 AM")
    assert iso == "2026-04-30T09:00:00-05:00"


def test_cst_winter_offset():
    """January 15 is CST (UTC-6)."""
    iso = _schedule_iso_central("2026-01-15", "9:00 AM")
    assert iso == "2026-01-15T09:00:00-06:00"


def test_pm_converts_to_24h():
    iso = _schedule_iso_central("2026-04-30", "3:30 PM")
    assert iso == "2026-04-30T15:30:00-05:00"


def test_midnight_12am():
    iso = _schedule_iso_central("2026-04-30", "12:00 AM")
    assert iso == "2026-04-30T00:00:00-05:00"


def test_noon_12pm():
    iso = _schedule_iso_central("2026-04-30", "12:00 PM")
    assert iso == "2026-04-30T12:00:00-05:00"


def test_24h_input():
    """Time string already in 24h form should pass through."""
    iso = _schedule_iso_central("2026-04-30", "21:45")
    assert iso == "2026-04-30T21:45:00-05:00"


def test_dst_spring_forward_day():
    """March 9 2026 is the day DST starts. Anything 3 AM or later is CDT."""
    iso = _schedule_iso_central("2026-03-09", "3:00 AM")
    assert iso == "2026-03-09T03:00:00-05:00"


def test_dst_fall_back_day():
    """November 1 2026 is the day DST ends. By 3 AM we're back to CST."""
    iso = _schedule_iso_central("2026-11-01", "3:00 AM")
    assert iso == "2026-11-01T03:00:00-06:00"


@pytest.mark.parametrize(
    "time_str,expected_h",
    [
        ("1:00 AM", "01:00:00"),
        ("9:00 AM", "09:00:00"),
        ("11:00 AM", "11:00:00"),
        ("1:00 PM", "13:00:00"),
        ("11:00 PM", "23:00:00"),
    ],
)
def test_ampm_conversions(time_str, expected_h):
    iso = _schedule_iso_central("2026-04-30", time_str)
    assert iso.startswith(f"2026-04-30T{expected_h}")
