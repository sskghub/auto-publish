# Auto-Publish Pipeline V2

Single Modal endpoint that handles the entire autopublish flow: video download, Blotato upload, transcription, caption generation, and multi-platform posting. Supports retry-safe architecture via job_id caching -- retries skip expensive steps and only re-post to failed platforms.

## Flow (fresh run)
1. **Recommended:** VPS Python poller [`drive_poll_autopublish.py`](drive_poll_autopublish.py) lists the Drive folder on a timer, skips Drive file IDs already stored in the poller state file, POSTs each new file to Modal sequentially (see [`deploy/README.md`](deploy/README.md)). **Legacy:** n8n Schedule + Drive search (easy to misconfigure; disable after cutover).
2. Filename becomes `caption`; same Drive download URL shape as before.
3. Modal endpoint (`replix-autopublish`) runs the full pipeline:
   - Download video from Google Drive + upload to Blotato (presigned URL)
   - Transcribe via Groq Whisper (Telugu: large-v3 translation, English: large-v3-turbo)
   - Generate captions via OpenAI gpt-5.4 + Tavily fact-grounding
   - Post to all platforms via Blotato API
   - Cache results at each step under a `job_id` (Modal Dict)
4. Real-time progress messages sent to Telegram at each step
5. Final report includes `job_id` + retry instructions for any failed platforms

Poller writes a Drive file ID to state **only** when Modal returns JSON `status: "completed"` so failures can be fixed and picked up on the next run.

## Flow (retry)
1. POST to same endpoint with `job_id` + `retry_platforms` list
2. Loads cached video URL, transcript, captions from Modal Dict
3. Skips steps 1-3 entirely (zero credits used)
4. Only posts to platforms in `retry_platforms`
5. Merges results into cached state

## Dry run
Pass `"dry_run": true` in the request. Runs steps 1-3 (download, transcribe, caption gen) but skips posting. Returns a `job_id` that can be used to publish later via retry.

## Platforms

| Platform | Telugu (#te) | English (#en) |
|----------|-------------|---------------|
| IG normal reel | configured via `accounts-json` secret | same |
| IG trial reel | same as normal IG (separate post per language) | same |
| YT Short | configured via `accounts-json` secret | same |
| FB Reel | configured via `accounts-json` secret + `page_id` | same |
| X tweet | -- | configured via `accounts-json` secret |

Account map schema lives in `accounts.example.json`. Real IDs are injected at runtime via the Modal `accounts-json` secret (single-line JSON), never hardcoded.

Telugu = 4 posts, English = 5 posts per video.

Platform keys for retry: `ig_te`, `yt_te`, `fb_te`, `ig_te_trial`, `ig_en`, `yt_en`, `fb_en`, `x_en`, `ig_en_trial`.

## Input formats

**Fresh run (from n8n):**
```json
{
  "video_url": "https://drive.usercontent.google.com/download?id=FILE_ID&export=download&confirm=t",
  "caption": "Video title EN #now.mp4"
}
```

**Retry (manual / from Cursor):**
```json
{
  "job_id": "abc-123-def",
  "retry_platforms": ["ig_te", "yt_te"]
}
```

**Retry (simple — Mac):** from this folder, add `API_AUTH_TOKEN` to a local `.env` (copy `deploy/env.example`; never commit). Then:
```bash
python3 retry_autopublish.py JOB_ID yt_en
python3 retry_autopublish.py JOB_ID yt_en fb_en
```
Same as curl; reads `MODAL_AUTOPUBLISH_URL` from env or `.env`. After `modal deploy`, the URL pattern is `https://<your-workspace>--replix-autopublish-autopublish.modal.run`.

**Retry (Telegram):** the bot remembers your **last job** per chat (updated whenever a run finishes). You can say `/retry last` (retry all platforms that failed on that job), or plain language without pasting ids: `/retry TE Instagram`, `/retry telugu youtube`, `/retry EN Facebook`, add `trial` for the IG trial reel only. When a run reports failures, the same message can show **inline buttons** (`Retry all failed`, per platform) that call the same `process_telegram_retry` path as `/retry` (no new logic). Still supported: `/retry JOB_ID yt_en` and internal keys.

After `modal deploy`, a second web endpoint `telegram_webhook` is live. Set the bot webhook once (replace `BOT_TOKEN` and the URL from `modal app list` or the deploy output — pattern `https://<workspace>--replix-autopublish-telegram-webhook.modal.run`):
```bash
curl -sS "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<your-workspace>--replix-autopublish-telegram-webhook.modal.run"
```
(Update the hostname if your Modal workspace slug differs.)
In chat, same allowlist as main pipeline: `TELEGRAM_ALLOWED_CHAT_IDS` in Modal secret `telegram-autopublish-bot` (comma-separated chat IDs; **required** — webhook returns 503 if unset). Commands: `/retry JOB_ID yt_en ig_en`, `/help`. The handler **spawns** `process_telegram_retry` and returns immediately so Telegram does not time out; progress and the final report go to the same chat. No `api-auth-token` on the Telegram path (auth = allowed `chat_id` only).

**Dry run:**
```json
{
  "video_url": "https://drive...",
  "caption": "Test EN #now",
  "dry_run": true
}
```

`chat_id` is read from the `TELEGRAM_CHAT_ID` env var on the Modal endpoint (set via the `telegram-autopublish-bot` secret). Not passed from the trigger.

## Caption syntax (in filename)
- `TE` or `EN` -- language (required, case-insensitive, standalone word or hashtag)
- `#now` / `#publish` / `#live` -- publish immediately
- `2026-04-20 3:00 PM` or `April 19, 2026 at 9:00 AM` -- schedule; wall time is parsed as **America/Chicago** (CST/CDT) in Modal, not IST. Natural dates must include a **year**.
- No schedule tag -- queued to next Blotato calendar slot

## Deploy
```bash
modal deploy autopublish_app.py
```

**Drive poller (VPS):** see [`deploy/README.md`](deploy/README.md) — `systemd` timer, `.env`, service account.

## Files
| File | Purpose |
|------|---------|
| autopublish_app.py | Modal endpoint: download + upload + transcription + captions + Blotato posting + retry cache |
| drive_poll_autopublish.py | VPS/Manual: poll Drive folder, POST new files to Modal; state file for idempotency |
| retry_autopublish.py | Mac/local: `python3 retry_autopublish.py JOB_ID yt_en` — retry failed platforms without SSH |
| post_video.py | Mac/local: post a single video to one or more platforms via Blotato |
| accounts.example.json | Schema for `ACCOUNTS_JSON` Modal secret (locale tag → Blotato account IDs) |
| platforms.example.json | Schema for `platforms.json` (used by `post_video.py` CLI) |
| deploy/README.md | Deploy poller: systemd, `.env`, Google service account |
| deploy/env.example | Environment template for poller |
| deploy/requirements-drive-poll.txt | pip deps for poller only |

## Cache
`modal.Dict` named `autopublish-jobs`. Stores per job_id: video_url, tag, language, publish_mode, scheduled_time, transcript, captions, posts. Persists across function calls. No TTL -- entries are tiny text.

## Environment Variables (Modal secrets)
- `blotato-api-key` — `BLOTATO_API_KEY` for posting
- `telegram-autopublish-bot` — `TELEGRAM_BOT_TOKEN` for progress messages, `TELEGRAM_CHAT_ID` for the destination chat, `TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated, **required** for `/retry` webhook), optional `TELEGRAM_RETRY_DEDUPE_SECONDS` (default **45**) blocks duplicate Telegram retries within that window (stored in Modal Dict `autopublish-retry-dedupe`)
- `api-auth-token` — `API_AUTH_TOKEN` for endpoint Bearer auth
- `accounts-json` — `ACCOUNTS_JSON` (single-line JSON, schema = `accounts.example.json`)
- `openai-secret` — `OPENAI_API_KEY` for caption generation
- `groq-api-key` — `GROQ_API_KEY` for transcription
- `tavily-api-key` — `TAVILY_API_KEY` for fact-grounding

## Error handling
- Each pipeline step sends progress to Telegram before and after
- If any step fails, a detailed error message is sent with: what failed, what succeeded, where to debug
- Platform posting is isolated: a failed FB post does not block YT or X
- YouTube rejects titles/descriptions containing `<` or `>`; captions are sanitized before Blotato upload (GPT sometimes outputs angle brackets)
- Trial reel is posted last (gap from normal reel to same IG account)
- Failed platforms listed with retry instructions + job_id in Telegram report
- Debug URLs: my.blotato.com/api-dashboard, my.blotato.com/failed, Modal dashboard

## Google Drive auth

Service account JSON; share the publish folder with the SA email. See [`deploy/README.md`](deploy/README.md).
