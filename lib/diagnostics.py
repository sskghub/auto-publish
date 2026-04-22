"""
Error-message helpers and platform-specific text sanitizers.

Pure functions only. No I/O, no Modal, no globals.
"""

from __future__ import annotations


def _diagnose_blotato_error(err: str) -> str:
    """Short hint for Telegram / logs — what broke and what to change."""
    if not err:
        return "See Blotato dashboard → Failed posts."
    low = err.lower()
    if "description must not contain" in low or (
        "<" in err and ">" in err and "422" in err
    ):
        return (
            "YouTube rejects < and > in title/description. Latest deploy strips "
            "them on post; use /retry or CLI retry."
        )
    if "no available slot" in low:
        return (
            "Blotato has no free calendar slot. Put #now in filename or an "
            "explicit date/time (America/Chicago)."
        )
    if "401" in err or "403" in low or "unauthorized" in low:
        return (
            "Auth issue — check Blotato API key secret and reconnect accounts "
            "in Blotato if needed."
        )
    if "timeout" in low or "timed out" in low:
        return (
            "Network or Blotato slow — retry same platforms; check "
            "status.blotato.com / my.blotato.com/failed."
        )
    if "422" in err:
        return (
            "Blotato rejected the payload — read the JSON message above; fix "
            "field (title, description, media) for that platform."
        )
    return (
        "Open my.blotato.com/failed — search by time; compare platform + errorMessage."
    )


def _youtube_safe_text(s: str) -> str:
    """YouTube Data API rejects descriptions/titles containing < or > (Blotato 422)."""
    if not s:
        return ""
    return str(s).replace("<", "").replace(">", "")
