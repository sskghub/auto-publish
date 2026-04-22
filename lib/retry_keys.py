"""
Retry-key helpers — translate loose Telegram retry text ('telugu instagram',
'EN YT FB', 'trial') into the canonical platform keys the pipeline expects.

Pure functions only. No I/O, no Modal, no globals.
"""

from __future__ import annotations

import re

VALID_RETRY_KEYS = frozenset(
    {
        "ig_te",
        "yt_te",
        "fb_te",
        "ig_te_trial",
        "ig_en",
        "yt_en",
        "fb_en",
        "x_en",
        "ig_en_trial",
    }
)

KEYS_TE = frozenset({"ig_te", "yt_te", "fb_te", "ig_te_trial"})
KEYS_EN = frozenset({"ig_en", "yt_en", "fb_en", "x_en", "ig_en_trial"})


def _failed_keys_from_cached_posts(posts: list) -> list[str]:
    out = []
    for r in posts or []:
        if r.get("status") == "failed" or r.get("final_status") == "failed":
            k = r.get("key")
            if k:
                out.append(k)
    return list(dict.fromkeys(out))


def _infer_retry_keys_natural(
    text: str, job_tag: str | None
) -> tuple[list[str] | None, str | None]:
    """
    Parse loose language like 'telugu instagram', 'TE IG', 'english youtube trial'.
    Returns (keys, None) or (None, error_message).
    """
    low = text.lower().strip()
    if not low:
        return None, "Say which platform (e.g. Instagram, YouTube) or use /retry last."

    has_te = bool(re.search(r"\b(te|telugu)\b", low))
    has_en = bool(re.search(r"\b(en|english)\b", low))
    if has_te and has_en:
        return None, "Pick one language: TE/Telugu or EN/English."

    lang: str | None = "te" if has_te else ("en" if has_en else None)
    if lang is None:
        if job_tag in ("te", "en"):
            lang = job_tag
        else:
            return None, "Say TE/Telugu or EN/English (e.g. 'TE Instagram')."

    trial = bool(re.search(r"\b(trial|trials|test)\b", low))

    want_ig = bool(
        re.search(r"\b(instagram|insta)\b", low)
        or re.search(r"(?<![a-z0-9])ig(?![a-z0-9])", low)
    )
    want_yt = bool(
        re.search(r"\b(youtube|shorts)\b", low)
        or re.search(r"(?<![a-z0-9])yt(?![a-z0-9])", low)
    )
    want_fb = bool(re.search(r"\b(facebook|fb)\b", low))
    want_x = bool(
        re.search(r"\b(twitter|tweet|tweets)\b", low)
        or re.search(r"(?<![a-z0-9])x(?![a-z0-9])", low)
    )

    if trial and not want_ig:
        want_ig = True

    if not any((want_ig, want_yt, want_fb, want_x)):
        return None, "Say the platform: Instagram, YouTube, Facebook, or X (Twitter)."

    keys: list[str] = []
    if want_ig:
        keys.append(f"ig_{lang}_trial" if trial else f"ig_{lang}")
    if want_yt:
        if trial:
            return None, "Trial applies to Instagram only; say YouTube without 'trial'."
        keys.append(f"yt_{lang}")
    if want_fb:
        if trial:
            return None, "Trial applies to Instagram only."
        keys.append(f"fb_{lang}")
    if want_x:
        if lang == "te":
            return None, "There is no X post for Telugu runs; use EN + X."
        if trial:
            return None, "Trial applies to Instagram only."
        keys.append("x_en")

    seen = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    bad = [k for k in uniq if k not in VALID_RETRY_KEYS]
    if bad:
        return None, f"Internal key error: {bad}"
    return uniq, None


def _keys_match_job_tag(keys: list, job_tag: str) -> bool:
    for k in keys:
        if k in KEYS_TE and job_tag != "te":
            return False
        if k in KEYS_EN and job_tag != "en":
            return False
    return True
