#!/usr/bin/env python3
"""
Post a video to any platform via Blotato.

Usage:
  python3 post_video.py --gdrive FILE_ID --platform linkedin --caption "text"
  python3 post_video.py --gdrive FILE_ID --platform ig-te ig-trial-te --caption "text"
  python3 post_video.py --file /path/to/video.mp4 --platform linkedin --caption "text"

The --platform values are loaded at runtime from platforms.json next to this script
(falls back to platforms.example.json with a warning). Add or remove platforms by
editing that file. Standard keys: linkedin, ig-te, ig-en, ig-trial-te, ig-trial-en,
yt-te, yt-en, fb-te, fb-en, x.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import re
from pathlib import Path


def _load_dotenv() -> None:
    """Load BLOTATO_API_KEY from .env next to this script if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv  # type: ignore

        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.is_file():
            load_dotenv(env_path)
    except ImportError:
        pass


_load_dotenv()

BLOTATO_API_KEY = os.environ.get("BLOTATO_API_KEY", "")
BLOTATO_BASE = "https://backend.blotato.com/v2"


def _load_platforms() -> dict:
    """
    Load platform/account map from platforms.json next to this script.
    Falls back to platforms.example.json with a warning so the CLI surface stays usable
    for tests / dry-runs without real Blotato account IDs.
    """
    here = Path(__file__).resolve().parent
    real = here / "platforms.json"
    example = here / "platforms.example.json"
    if real.is_file():
        return json.loads(real.read_text(encoding="utf-8"))
    if example.is_file():
        sys.stderr.write(
            "warning: using platforms.example.json (placeholder IDs). "
            "Copy to platforms.json and fill in real Blotato account IDs.\n"
        )
        return json.loads(example.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        "Missing platforms.json (or platforms.example.json). "
        "Create one next to post_video.py — see README."
    )


PLATFORMS = _load_platforms()


def download_gdrive(file_id: str) -> bytes:
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    print(f"Downloading from Google Drive ({file_id})...")

    with urllib.request.urlopen(req, timeout=120) as resp:
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read()

    if "text/html" in content_type:
        html = data.decode("utf-8", errors="replace")
        match = re.search(r'uuid=([^&"]+)', html) or re.search(
            r'confirm=([^&"]+)', html
        )
        if match:
            key = "uuid" if "uuid=" in html else "confirm"
            val = match.group(1)
            url2 = (
                f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t&uuid={val}"
                if key == "uuid"
                else f"https://drive.google.com/uc?export=download&id={file_id}&confirm={val}"
            )
            req2 = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=300) as resp2:
                data = resp2.read()

    print(f"Downloaded {len(data)/1024/1024:.1f} MB")
    if len(data) < 10_000:
        print("ERROR: Download too small — file may not be public or ID is wrong.")
        sys.exit(1)
    return data


def _require_key() -> str:
    if not BLOTATO_API_KEY:
        sys.stderr.write(
            "ERROR: BLOTATO_API_KEY is not set. Export it or add to .env. "
            "Get one at https://my.blotato.com/api-dashboard.\n"
        )
        sys.exit(2)
    return BLOTATO_API_KEY


def upload_blotato(video_bytes: bytes, filename: str = "video.mp4") -> str:
    api_key = _require_key()
    print("Getting Blotato presigned URL...")
    req = urllib.request.Request(
        f"{BLOTATO_BASE}/media/uploads",
        data=json.dumps({"filename": filename}).encode(),
        headers={"Content-Type": "application/json", "blotato-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    presigned_url = result["presignedUrl"]
    public_url = result["publicUrl"]

    ext = filename.rsplit(".", 1)[-1].lower()
    ctype = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}.get(
        ext, "video/mp4"
    )

    print(f"Uploading {len(video_bytes)/1024/1024:.1f} MB to Blotato...")
    put_req = urllib.request.Request(
        presigned_url,
        data=video_bytes,
        headers={"Content-Type": ctype},
        method="PUT",
    )
    with urllib.request.urlopen(put_req, timeout=300) as r:
        if r.status >= 400:
            print(f"Upload failed: {r.status}")
            sys.exit(1)

    print(f"Uploaded. Public URL: {public_url}")
    return public_url


def post_blotato(
    account_id: str,
    platform: str,
    target: str,
    caption: str,
    public_url: str,
    page_id: str = None,
    scheduled_time: str = None,
    target_extra: dict = None,
) -> dict:
    target_obj = {"targetType": target}
    if page_id:
        target_obj["pageId"] = page_id
    if target_extra:
        target_obj.update(target_extra)

    body = {
        "post": {
            "accountId": account_id,
            "content": {
                "text": caption,
                "mediaUrls": [public_url],
                "platform": platform,
            },
            "target": target_obj,
        }
    }

    if scheduled_time:
        body["scheduledTime"] = scheduled_time

    api_key = _require_key()
    req = urllib.request.Request(
        f"{BLOTATO_BASE}/posts",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "blotato-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Post video to platforms via Blotato")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--gdrive", metavar="FILE_ID", help="Google Drive file ID")
    src.add_argument("--file", metavar="PATH", help="Local video file path")
    src.add_argument("--url", metavar="URL", help="Direct video URL (already uploaded)")
    parser.add_argument(
        "--platform",
        nargs="+",
        required=True,
        choices=list(PLATFORMS),
        metavar="PLATFORM",
        help=f"One or more of: {', '.join(PLATFORMS)}",
    )
    parser.add_argument("--caption", required=True, help="Caption text")
    parser.add_argument(
        "--schedule",
        metavar="ISO_DATETIME",
        help="Schedule time in ISO 8601 UTC, e.g. 2026-04-22T14:00:00Z",
    )
    args = parser.parse_args()

    # Get public URL
    if args.url:
        public_url = args.url
    else:
        if args.gdrive:
            video_bytes = download_gdrive(args.gdrive)
        else:
            with open(args.file, "rb") as f:
                video_bytes = f.read()
            print(f"Read local file: {len(video_bytes)/1024/1024:.1f} MB")
        public_url = upload_blotato(video_bytes)

    # Post to each platform
    results = []
    for p in args.platform:
        cfg = PLATFORMS[p]
        print(f"\nPosting to {cfg['label']}...")
        try:
            result = post_blotato(
                account_id=cfg["account_id"],
                platform=cfg["platform"],
                target=cfg["target"],
                caption=args.caption,
                public_url=public_url,
                page_id=cfg.get("page_id"),
                scheduled_time=args.schedule,
                target_extra=cfg.get("target_extra"),
            )
            submission_id = (
                result.get("postSubmissionId")
                or result.get("submissionId")
                or result.get("id")
            )
            print(f"  OK — submission ID: {submission_id}")
            results.append(
                {"platform": cfg["label"], "status": "submitted", "id": submission_id}
            )
        except Exception as e:
            print(f"  FAILED: {e}")
            results.append(
                {"platform": cfg["label"], "status": "failed", "error": str(e)}
            )

        # 4s delay between IG posts (rate limit)
        if p.startswith("ig-") and args.platform.index(p) < len(args.platform) - 1:
            next_p = args.platform[args.platform.index(p) + 1]
            if next_p.startswith("ig-"):
                time.sleep(4)

    print("\n--- Results ---")
    for r in results:
        status = "OK" if r["status"] == "submitted" else "FAIL"
        detail = r.get("id") or r.get("error", "")
        print(f"  [{status}] {r['platform']}: {detail}")

    print("\nCheck my.blotato.com/failed if anything didn't go through.")


if __name__ == "__main__":
    main()
