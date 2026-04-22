# Changelog

## [1.0.0] - 2026-04-22

Initial public release.

### Added

- Modal endpoint (`autopublish_app.py`) — Drive URL or upload → Whisper transcript → Tavily-grounded captions → Blotato multi-platform post (IG / YT / FB / X for Telugu and English).
- VPS poller (`drive_poll_autopublish.py`) — `systemd` timer that watches a Google Drive folder and posts to the Modal endpoint.
- Telegram retry CLI (`retry_autopublish.py`) — local script to retry failed jobs by `job_id` and platform key.
- Single-video posting helper (`post_video.py`) — direct Blotato post bypassing the AI captioning step.
- `accounts.example.json` and `platforms.example.json` templates for Blotato account / platform configuration.
- Deploy guide for the VPS poller (`deploy/README.md` + `systemd` units).

### Security

- All Telegram chat IDs, Modal workspace slugs, Blotato API keys, and Blotato account/platform IDs read from environment / Modal secrets. Nothing hardcoded.
- `accounts.json` and `platforms.json` are gitignored; only `.example` files ship in the repo.
- Telegram retry webhook is fail-closed: if `TELEGRAM_ALLOWED_CHAT_IDS` is unset, the endpoint returns `403`.
- Telegram and Blotato polling failures are logged to stderr (visible in Modal / `journalctl`) instead of silently swallowed.
