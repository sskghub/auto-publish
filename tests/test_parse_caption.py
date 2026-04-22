"""
Tests for `_parse_caption` — extracts language tag, schedule, and publish mode
from a Drive filename like 'Topic EN #now.mp4' or 'Topic TE 2026-04-30 9:00 AM'.
"""

import pytest

from autopublish_app import _parse_caption


def test_publish_now_english():
    out = _parse_caption("Claude Code Obsidian setup EN #now.mp4")
    assert out["tag"] == "en"
    assert out["language"] == "english"
    assert out["publish_mode"] == "publish"
    assert out["scheduled_time"] is None


def test_publish_now_telugu_via_publish_tag():
    out = _parse_caption("Cursor TE #publish.mp4")
    assert out["tag"] == "te"
    assert out["language"] == "telugu"
    assert out["publish_mode"] == "publish"
    assert out["scheduled_time"] is None


def test_no_schedule_defaults_to_next_slot():
    out = _parse_caption("Random topic EN.mp4")
    assert out["tag"] == "en"
    assert out["publish_mode"] == "next_slot"
    assert out["scheduled_time"] is None


def test_iso_schedule_telugu():
    out = _parse_caption("Topic TE 2026-04-30 09:00.mp4")
    assert out["tag"] == "te"
    assert out["publish_mode"] == "scheduled"
    assert out["scheduled_time"] is not None
    assert out["scheduled_time"].startswith("2026-04-30T09:00:00")


def test_natural_date_schedule_english():
    out = _parse_caption("Topic EN April 30, 2026 at 9:00 AM.mp4")
    assert out["publish_mode"] == "scheduled"
    assert out["scheduled_time"] is not None
    assert out["scheduled_time"].startswith("2026-04-30T09:00:00")


def test_now_overrides_schedule():
    """If both a date and #now are present, #now wins (publish immediately)."""
    out = _parse_caption("Topic EN 2026-04-30 09:00 #now.mp4")
    assert out["publish_mode"] == "publish"
    assert out["scheduled_time"] is None


def test_no_language_tag_returns_none():
    out = _parse_caption("Just a filename.mp4")
    assert out["tag"] is None
    assert out["language"] is None


def test_telugu_word_recognized_without_hashtag():
    out = _parse_caption("Cursor demo telugu.mp4")
    assert out["tag"] == "te"
    assert out["language"] == "telugu"


def test_english_hashtag_recognized():
    out = _parse_caption("Cursor demo #en.mp4")
    assert out["tag"] == "en"


@pytest.mark.parametrize("trigger", ["#now", "#publish", "#live"])
def test_all_publish_now_triggers(trigger):
    out = _parse_caption(f"Topic EN {trigger}.mp4")
    assert out["publish_mode"] == "publish"
