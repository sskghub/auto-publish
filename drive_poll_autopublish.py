#!/usr/bin/env python3
"""
Poll a Google Drive folder for new video files; POST each to the Modal autopublish
endpoint sequentially. Durable idempotency via a JSON state file.

Env (see deploy/env.example):
  GOOGLE_SERVICE_ACCOUNT_FILE, DRIVE_FOLDER_ID, MODAL_AUTOPUBLISH_URL, API_AUTH_TOKEN
  optional: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, AUTOPUBLISH_STATE_FILE, LOG_FILE
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)
DEFAULT_STATE = {"version": 1, "processed_ids": []}
MAX_NAME_LOG = 200
MODAL_TIMEOUT_SEC = 320.0
MIN_FILE_BYTES = 1  # skip empty / not yet fully uploaded (0 bytes)

log = logging.getLogger("autopublish-drive-poll")


def _load_env() -> None:
    """Load .env from CWD or next to this script if python-dotenv is available."""
    try:
        from dotenv import load_dotenv  # type: ignore

        here = Path(__file__).resolve().parent
        for p in (Path.cwd() / ".env", here / ".env"):
            if p.is_file():
                load_dotenv(p)
                return
    except ImportError:
        return


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    log.addHandler(h)
    log_path = os.environ.get("LOG_FILE", "").strip()
    if log_path:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000]},
            timeout=15.0,
        )
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("State file corrupt or unreadable: %s", e)
        raise
    if "processed_ids" not in data:
        data["processed_ids"] = []
    if "version" not in data:
        data["version"] = 1
    return data


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix="state.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def drive_service():
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not sa_path or not Path(sa_path).is_file():
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_FILE must point to a service account JSON key"
        )
    creds = service_account.Credentials.from_service_account_file(
        sa_path, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_videos(service, folder_id: str) -> list[dict[str, Any]]:
    q = (
        f"'{folder_id}' in parents and trashed = false and "
        "mimeType contains 'video/'"
    )
    out: list[dict[str, Any]] = []
    page_token = None
    while True:
        try:
            resp = (
                service.files()
                .list(
                    q=q,
                    fields="nextPageToken, files(id, name, createdTime, size, mimeType)",
                    orderBy="createdTime asc",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
        except HttpError as e:
            log.error("Drive list failed: %s", e)
            raise
        for f in resp.get("files", []):
            out.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def build_modal_payload(file_id: str, filename: str) -> dict[str, str]:
    video_url = (
        f"https://drive.usercontent.google.com/download?id={file_id}"
        f"&export=download&confirm=t"
    )
    return {"video_url": video_url, "caption": filename}


def call_modal(url: str, token: str, body: dict[str, str]) -> httpx.Response:
    # follow_redirects=True so a Modal edge 3xx (e.g. cold-start handover) does
    # not get treated as failure and re-fired — that path was the dup-publish bug.
    return httpx.post(
        url.rstrip("/"),
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(MODAL_TIMEOUT_SEC, connect=30.0),
        follow_redirects=True,
    )


def run_once() -> int:
    _load_env()
    folder_id = os.environ.get("DRIVE_FOLDER_ID", "").strip()
    modal_url = os.environ.get("MODAL_AUTOPUBLISH_URL", "").strip()
    api_token = os.environ.get("API_AUTH_TOKEN", "").strip()
    state_path = Path(
        os.environ.get(
            "AUTOPUBLISH_STATE_FILE",
            str(Path.home() / ".autopublish-drive" / "processed.json"),
        )
    )

    if not folder_id:
        raise ValueError("DRIVE_FOLDER_ID is required")
    if not modal_url:
        raise ValueError("MODAL_AUTOPUBLISH_URL is required")
    if not api_token:
        raise ValueError("API_AUTH_TOKEN is required")

    state = load_state(state_path)
    processed: set[str] = set(state.get("processed_ids", []))

    service = drive_service()
    files = list_videos(service, folder_id)
    new_count = 0
    for f in files:
        fid = f.get("id") or ""
        name = f.get("name") or ""
        if not fid or not name:
            continue
        if fid in processed:
            continue
        size = f.get("size")
        if size is not None:
            try:
                if int(size) < MIN_FILE_BYTES:
                    log.info("skip empty or tiny file id=%s name=%s", fid, name[:80])
                    continue
            except (TypeError, ValueError):
                pass

        new_count += 1
        log.info("posting file id=%s name=%s", fid, name[:MAX_NAME_LOG])
        body = build_modal_payload(fid, name)
        try:
            r = call_modal(modal_url, api_token, body)
        except httpx.RequestError as e:
            err = f"request error: {e}"
            log.error("Modal request failed for %s: %s", name[:80], err)
            send_telegram(
                f"AUTOPUBLISH-DRIVE-POLL — Modal request failed for {name[:120]}\n{err[:500]}"
            )
            return 1

        if not 200 <= r.status_code < 300:
            err = f"HTTP {r.status_code} {r.text[:500]}"
            log.error("Modal bad status for %s: %s", name[:80], err)
            send_telegram(
                f"AUTOPUBLISH-DRIVE-POLL — Modal HTTP {r.status_code} for {name[:120]}\n{err[:500]}"
            )
            return 1

        try:
            data = r.json()
        except json.JSONDecodeError:
            err = r.text[:500]
            log.error("Modal not JSON for %s: %s", name[:80], err)
            send_telegram(
                f"AUTOPUBLISH-DRIVE-POLL — Modal non-JSON for {name[:120]}\n{err}"
            )
            return 1

        st = (data.get("status") or "").strip()
        if st == "completed":
            processed.add(fid)
            state["processed_ids"] = sorted(processed)
            save_state(state_path, state)
            log.info("completed job_id=%s file=%s", data.get("job_id", ""), name[:80])
        else:
            err = data.get("error", data.get("message", r.text)) or st
            err_s = str(err)[:800]
            log.error("Modal returned non-completed for %s: %s", name[:80], err_s)
            send_telegram(
                f"AUTOPUBLISH-DRIVE-POLL — Modal failed for {name[:120]}\n"
                f"status={st}\n{err_s}"
            )
            return 1

    log.info(
        "poll finished: modal_attempts_this_run=%s drive_files_seen=%s tracked_ids=%s",
        new_count,
        len(files),
        len(processed),
    )
    return 0


def main() -> None:
    _setup_logging()
    try:
        code = run_once()
        sys.exit(code)
    except Exception as e:
        msg = f"AUTOPUBLISH-DRIVE-POLL — {type(e).__name__}: {str(e)[:800]}"
        log.exception("fatal: %s", e)
        send_telegram(msg)
        raise


if __name__ == "__main__":
    main()
