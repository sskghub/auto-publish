"""
Tests for `_infer_retry_keys_natural` — parses Telegram retry commands like
'/retry telugu instagram' or 'EN YT FB' into platform key lists.
"""

import pytest

from autopublish_app import _infer_retry_keys_natural


def test_telugu_instagram():
    keys, err = _infer_retry_keys_natural("telugu instagram", job_tag=None)
    assert err is None
    assert keys == ["ig_te"]


def test_short_form_te_ig():
    keys, err = _infer_retry_keys_natural("TE IG", job_tag=None)
    assert err is None
    assert keys == ["ig_te"]


def test_english_youtube():
    keys, err = _infer_retry_keys_natural("english youtube", job_tag=None)
    assert err is None
    assert keys == ["yt_en"]


def test_multiple_platforms_english():
    keys, err = _infer_retry_keys_natural("EN IG YT FB X", job_tag=None)
    assert err is None
    assert set(keys) == {"ig_en", "yt_en", "fb_en", "x_en"}


def test_trial_implies_instagram():
    keys, err = _infer_retry_keys_natural("EN trial", job_tag=None)
    assert err is None
    assert keys == ["ig_en_trial"]


def test_trial_with_explicit_ig():
    keys, err = _infer_retry_keys_natural("english instagram trial", job_tag=None)
    assert err is None
    assert keys == ["ig_en_trial"]


def test_x_telugu_rejected():
    """There is no Telugu X account — should be rejected with a clear error."""
    keys, err = _infer_retry_keys_natural("TE X", job_tag=None)
    assert keys is None
    assert err is not None and "X" in err


def test_trial_with_youtube_rejected():
    keys, err = _infer_retry_keys_natural("EN YT trial", job_tag=None)
    assert keys is None
    assert err is not None and "Trial" in err


def test_both_languages_rejected():
    keys, err = _infer_retry_keys_natural("TE EN IG", job_tag=None)
    assert keys is None
    assert err is not None and "one language" in err.lower()


def test_no_language_no_job_tag_rejected():
    keys, err = _infer_retry_keys_natural("instagram", job_tag=None)
    assert keys is None
    assert err is not None


def test_no_language_falls_back_to_job_tag():
    """If user just says 'instagram' but the job was tagged 'te', use 'te'."""
    keys, err = _infer_retry_keys_natural("instagram", job_tag="te")
    assert err is None
    assert keys == ["ig_te"]


def test_no_platform_rejected():
    keys, err = _infer_retry_keys_natural("english", job_tag=None)
    assert keys is None
    assert err is not None and "platform" in err.lower()


def test_empty_text_rejected():
    keys, err = _infer_retry_keys_natural("", job_tag=None)
    assert keys is None
    assert err is not None


@pytest.mark.parametrize("phrase", ["facebook", "fb", "FB"])
def test_facebook_aliases(phrase):
    keys, err = _infer_retry_keys_natural(f"EN {phrase}", job_tag=None)
    assert err is None
    assert keys == ["fb_en"]


@pytest.mark.parametrize("phrase", ["twitter", "tweet", "X"])
def test_twitter_aliases(phrase):
    keys, err = _infer_retry_keys_natural(f"EN {phrase}", job_tag=None)
    assert err is None
    assert keys == ["x_en"]


def test_dedupes_repeated_platforms():
    keys, err = _infer_retry_keys_natural("EN IG IG instagram", job_tag=None)
    assert err is None
    assert keys == ["ig_en"]
