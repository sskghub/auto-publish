"""
Caption parsing helpers — extract language tag, schedule, and publish mode
from a Drive filename like 'Topic EN #now.mp4' or 'Topic TE 2026-04-30 9:00 AM'.

Pure functions only. No I/O, no Modal, no globals.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

MONTH_MAP = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

PUBLISH_NOW_TRIGGERS = {
    "#publish",
    "#live",
    "#now",
    "publish now",
    "go live",
    "post now",
    "publish immediately",
}


def _convert_12_to_24(t: str) -> str:
    t = t.strip()
    ampm_match = re.search(r"(AM|PM)", t, re.IGNORECASE)
    numeric = re.sub(r"(AM|PM)", "", t, flags=re.IGNORECASE).strip()
    parts = numeric.split(":")
    h = int(parts[0])
    m = parts[1] if len(parts) > 1 else "00"
    if ampm_match:
        is_pm = ampm_match.group(1).upper() == "PM"
        if is_pm and h < 12:
            h += 12
        if not is_pm and h == 12:
            h = 0
    return f"{h:02d}:{m}:00"


def _schedule_iso_central(date_str: str, time_str: str) -> str:
    """RFC3339 instant for Blotato: wall-clock in America/Chicago (CST/CDT, DST-aware)."""
    t24 = _convert_12_to_24(time_str)
    parts = t24.split(":")
    h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    y, mo, d = map(int, date_str.split("-"))
    tz = ZoneInfo("America/Chicago")
    dt = datetime(y, mo, d, h, m, s, tzinfo=tz)
    return dt.isoformat(timespec="seconds")


def _format_schedule_label(iso_str: str) -> str:
    """Formats ISO schedule time as 'April 18, 9:00 AM CDT'."""
    dt = datetime.fromisoformat(iso_str).astimezone(ZoneInfo("America/Chicago"))
    tz_name = "CDT" if dt.dst() and dt.dst().total_seconds() > 0 else "CST"
    return dt.strftime(f"%B %-d, %-I:%M %p {tz_name}")


def _parse_caption(caption: str) -> dict:
    lower = caption.lower()

    tag = None
    if "#te" in lower or "telugu" in lower or re.search(r"\bte\b", lower):
        tag = "te"
    elif "#en" in lower or "english" in lower or re.search(r"\ben\b", lower):
        tag = "en"

    scheduled_time = None
    publish_mode = "next_slot"

    iso_match = re.search(
        r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2}\s*(?:AM|PM)?)", caption, re.IGNORECASE
    )
    natural_match = re.search(
        r"(?:on|for|at)?\s*(?:(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4}))\s*(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)?",
        caption,
        re.IGNORECASE,
    )

    if iso_match:
        date_str = iso_match.group(1)
        time_str = iso_match.group(2).strip()
        scheduled_time = _schedule_iso_central(date_str, time_str)
        publish_mode = "scheduled"
    elif natural_match:
        month_name = natural_match.group(1).lower()
        day = int(natural_match.group(2))
        year = int(natural_match.group(3))
        month_num = MONTH_MAP.get(month_name)
        if month_num and day and year:
            date_str = f"{year}-{month_num:02d}-{day:02d}"
            time_str = (
                natural_match.group(4).strip() if natural_match.group(4) else "10:00 AM"
            )
            scheduled_time = _schedule_iso_central(date_str, time_str)
            publish_mode = "scheduled"

    for trigger in PUBLISH_NOW_TRIGGERS:
        if trigger in lower:
            publish_mode = "publish"
            scheduled_time = None
            break

    has_notrial = (
        ("#notrial" in lower) or ("no trail" in lower) or ("no trial" in lower)
    )
    has_trialonly = ("#trialonly" in lower) or ("trial only" in lower)
    if has_trialonly and has_notrial:
        publish_scope = "conflict"
    elif has_trialonly:
        publish_scope = "trialonly"
    elif has_notrial:
        publish_scope = "notrial"
    else:
        publish_scope = "full"

    lang_map = {"te": "telugu", "en": "english"}
    return {
        "tag": tag,
        "language": lang_map.get(tag) if tag else None,
        "scheduled_time": scheduled_time,
        "publish_mode": publish_mode,
        "publish_scope": publish_scope,
    }
