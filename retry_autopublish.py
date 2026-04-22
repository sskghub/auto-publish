#!/usr/bin/env python3
"""
Retry failed Modal autopublish platforms (same cached video + captions).

Usage:
  cp deploy/env.example .env   # once; fill API_AUTH_TOKEN and MODAL_AUTOPUBLISH_URL

  python3 retry_autopublish.py JOB_ID yt_en
  python3 retry_autopublish.py JOB_ID yt_en fb_en

Or export API_AUTH_TOKEN and MODAL_AUTOPUBLISH_URL in the shell instead of .env.

Platform keys: ig_te, yt_te, fb_te, ig_te_trial, ig_en, yt_en, fb_en, x_en, ig_en_trial
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Set MODAL_AUTOPUBLISH_URL via env or .env. Pattern after `modal deploy`:
#   https://<workspace>--replix-autopublish-autopublish.modal.run


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main() -> int:
    p = argparse.ArgumentParser(
        description="POST job_id + retry_platforms to Modal autopublish (no re-transcode).",
    )
    p.add_argument("job_id", help="From Telegram: Job ID: ...")
    p.add_argument(
        "platforms",
        nargs="+",
        help="Keys to retry, e.g. yt_en ig_en",
    )
    args = p.parse_args()

    here = Path(__file__).resolve().parent
    _load_dotenv(here / ".env")

    token = os.environ.get("API_AUTH_TOKEN", "").strip()
    url = os.environ.get("MODAL_AUTOPUBLISH_URL", "").strip()

    missing = [
        k
        for k, v in (("API_AUTH_TOKEN", token), ("MODAL_AUTOPUBLISH_URL", url))
        if not v
    ]
    if missing:
        print(
            f"Missing required env vars: {', '.join(missing)}.\n"
            "Set them in your shell or create .env next to this script:\n"
            "  API_AUTH_TOKEN=...\n"
            "  MODAL_AUTOPUBLISH_URL=https://<workspace>--replix-autopublish-autopublish.modal.run",
            file=sys.stderr,
        )
        return 1

    body = json.dumps(
        {
            "job_id": args.job_id.strip(),
            "retry_platforms": [x.strip() for x in args.platforms],
        }
    ).encode("utf-8")

    req = Request(
        url.rstrip("/"),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=360) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except HTTPError as e:
        err = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        print(f"HTTP {e.code}: {err[:800]}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"Request failed: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print(raw[:2000], file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2))
    st = (data.get("status") or "").lower()
    return 0 if st == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
