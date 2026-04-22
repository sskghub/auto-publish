# Drive poller deployment (VPS)

Replaces the n8n Schedule + Google Drive search workflow with a single Python process: list folder, skip already-processed file IDs, POST each new video to Modal **sequentially**.

## Layout on VPS

```
/opt/autopublish-drive/
  venv/                         # python3 -m venv venv && ./venv/bin/pip install -r deploy/requirements-drive-poll.txt
  drive_poll_autopublish.py     # copy from repo root (same folder as autopublish_app.py)
  .env                          # from deploy/env.example (chmod 600)
  secrets/google-sa.json        # service account key (chmod 600)
  state/processed.json          # created automatically (processed Drive file IDs)
```

Log file optional: set `LOG_FILE=/var/log/autopublish-drive.log` in `.env` and ensure the service user can write it, or rely on `journalctl`.

## Google Cloud setup

1. Enable **Google Drive API** for a project.
2. Create a **service account**, download JSON key → place at `secrets/google-sa.json` on the VPS.
3. **Share** your `Auto_Publish` Drive folder with the service account email (Viewer is enough).

## Environment

See [`env.example`](env.example). Required: `GOOGLE_SERVICE_ACCOUNT_FILE`, `DRIVE_FOLDER_ID`, `MODAL_AUTOPUBLISH_URL`, `API_AUTH_TOKEN`.

`API_AUTH_TOKEN` must match the Modal secret **`api-auth-token`** (see [`Modal & Deployment/CLAUDE.md`](../../Modal%20&%20Deployment/CLAUDE.md), section **Shared Bearer auth**) — **the same value** used for Content Repurpose, cold email, collab reply, and every other Modal app that mounts that secret.

## systemd

```bash
sudo cp deploy/autopublish-drive-poll.service /etc/systemd/system/
sudo cp deploy/autopublish-drive-poll.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autopublish-drive-poll.timer
```

Check:

```bash
sudo systemctl list-timers autopublish-drive-poll.timer
sudo journalctl -u autopublish-drive-poll.service -n 80 --no-pager
```

Manual one-shot:

```bash
sudo systemctl start autopublish-drive-poll.service
```

## Local test (Mac)

From the Auto Publish folder, with `.env` and a service account that can see the folder:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r deploy/requirements-drive-poll.txt
export GOOGLE_SERVICE_ACCOUNT_FILE=...
export DRIVE_FOLDER_ID=...
export MODAL_AUTOPUBLISH_URL=...
export API_AUTH_TOKEN=...
python drive_poll_autopublish.py
```

## Confirm only one trigger is live

Make sure only this poller (or your own equivalent) triggers new runs. Two triggers = duplicate posts.
