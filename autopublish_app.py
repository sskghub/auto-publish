"""
Auto-Publish V2 -- Modal Endpoint
Replix Lab

Single endpoint: autopublish
Google Drive / URL -> Blotato upload -> Groq Whisper -> OpenAI captions -> post to all platforms
Supports retry via job_id: skips transcription + caption gen, only re-posts to failed platforms.
Parsed schedule times use America/Chicago (US Central, CST/CDT), not local server time.

Platforms: IG (normal + trial), YT Shorts, FB Reels, X
"""

import json
import modal
import os
import re
from fastapi import Header, HTTPException

app = modal.App("replix-autopublish")

image = (
    modal.Image.debian_slim()
    .pip_install("openai", "groq", "fastapi", "httpx", "tavily-python", "tzdata")
    .apt_install("ffmpeg")
    .add_local_python_source("lib")
)

job_cache = modal.Dict.from_name("autopublish-jobs", create_if_missing=True)
# Last finished autopublish job_id per Telegram chat (for "/retry IG" without pasting id).
telegram_last_job = modal.Dict.from_name(
    "autopublish-telegram-last-job", create_if_missing=True
)
# Cooldown for same chat + job + platform set (Telegram double-tap / repeated /retry).
retry_dedupe = modal.Dict.from_name("autopublish-retry-dedupe", create_if_missing=True)

# ---------------------------------------------------------------------------
# Account map
#
# Loaded lazily from env var ACCOUNTS_JSON (single-line JSON). Lazy load is
# deliberate: Modal secrets are injected at function execution time, not at
# image import. Provide via Modal secret `accounts-json` (or local .env for
# tests). Schema matches accounts.example.json in the repo root. Missing or
# empty = `_get_accounts()` raises 503.
# ---------------------------------------------------------------------------


def _get_accounts() -> dict:
    raw = os.environ.get("ACCOUNTS_JSON", "").strip()
    if not raw:
        raise HTTPException(
            status_code=503,
            detail="ACCOUNTS_JSON env var is required (see accounts.example.json)",
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=503,
            detail=f"ACCOUNTS_JSON is not valid JSON: {e}",
        ) from e


BLOTATO_BASE = "https://backend.blotato.com/v2"

# Pure helpers extracted to lib/ for testability. Re-imported here so existing
# call sites in this file keep working unchanged.
from lib.retry_keys import (  # noqa: E402
    VALID_RETRY_KEYS,
    _failed_keys_from_cached_posts,
    _infer_retry_keys_natural,
    _keys_match_job_tag,
)
from lib.diagnostics import _diagnose_blotato_error  # noqa: E402

JOB_ID_TOKEN_RE = re.compile(r"^[a-f0-9]{8,14}$", re.I)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """You parse transcripts from Whisper speech-to-text. For Telugu videos, the transcript is a Whisper translation to English which may have minor errors on product names.

Extract the following as JSON:

{
  "topic": "One sentence: what this video actually teaches.",
  "search_query": "A precise Google search query (5-10 words) to find the exact tool/feature/update this video covers. Use inferred real names, not phonetic spellings. Example: 'Claude by Anthropic Excel add-in install' not 'cloud excel add in'.",
  "tools_raw": ["List of tool/product names as heard in transcript"],
  "tools_inferred": ["Your best guess at the real names, using these patterns: clad/klad/cloud=Claude, car sir/karsar=Cursor, chat jpt=ChatGPT, co pilot=Copilot, supa base=Supabase, fast api=FastAPI, git hub=GitHub, n eight n/n8n=n8n, mid journey=Midjourney, eleven labs=ElevenLabs, cap cut=CapCut, ver cel=Vercel, open ai=OpenAI, olayam/ollama=Ollama"],
  "key_actions": ["3-5 exact steps shown: commands typed, buttons clicked, settings changed"],
  "primary_keyword": "The single most important search keyword phrase for this video (e.g. 'Claude Code Obsidian setup', 'Gemma 4 Google AI')",
  "uncertain": "Anything you could not confidently identify"
}

Return only valid JSON. No markdown."""

CAPTION_PROMPT = """You write English social media metadata for Sai (ssktechy), a Telugu creator teaching AI tools to 180K+ IG followers and 87K+ YT subscribers.

You receive: transcript topic, key actions, inferred tool names, primary keyword, and Tavily search results with verified facts. Use Tavily to ground every claim. If a tool name from the transcript conflicts with Tavily, use Tavily's version. Never invent facts, numbers, dates, or specs not in the input.

================================================================
YOUTUBE TITLE (yt_title)
================================================================
One title only. This must be high-CTR and search-optimized.

Rules:
- 25-55 characters. Front-load the primary keyword in the first 30 chars.
- The exact tool/product name must appear (YouTube indexes it for search).
- Must accurately describe what happens in the video. No misleading clickbait.
- Titles that match content get higher completion rates, which is the #1 ranking signal for Shorts in 2026.
- Use natural, searchable phrasing that matches what a viewer would actually type.
- No ALL CAPS. No excessive punctuation. No em dashes.
- Never use angle brackets < or > in the title (YouTube rejects uploads that contain them).
- High-CTR patterns (pick what fits naturally):
  "How to [action] with [Tool]"
  "[Tool]: [specific outcome] in [timeframe]"
  "I [did X] with [Tool]"
  "Stop [wrong way], use [Tool] instead"
  "[Tool] [action] that [specific result]"

================================================================
YOUTUBE DESCRIPTION (yt_description)
================================================================
3-5 sentences. Keyword-optimized for YouTube and Google search.

Rules:
- Sentence 1: primary keyword phrase, naturally written. YouTube heavily weighs the first 2 sentences for ranking.
- Sentences 2-3: specific steps, commands, or outcomes from the video. Be concrete, not vague.
- Sentence 4: secondary keyword variation or related search term, woven naturally.
- Final line: "#ssktechy" on its own line.
- Never use angle brackets < or > anywhere in the description (YouTube API returns 422).
- No CTAs (no "subscribe", "like", "comment", "link in bio"). No timestamps. No chapters.
- No generic filler like "In this video I show you..." or "Check out this amazing tool..."

================================================================
INSTAGRAM CAPTION (ig_caption)
================================================================
Instagram captions are now indexed by Google and Instagram search. Keywords drive reach 30% more than hashtags alone. Write for search AND saves.

Rules:
- 150-250 characters of caption text (before hashtags).
- Line 1 = primary keyword phrase + scroll-stopping hook. This line carries the most algorithmic weight. Open with the result, the surprising fact, or the specific outcome. Never open with "Here's how..." or "Check this out..."
- Lines 2-3: specific details from the video (tool names, commands, numbers). Specificity drives saves, and saves count 3x more than likes.
- Use natural keyword repetition and synonyms throughout. Instagram uses semantic search, so related phrases help.
- End the caption naturally. No CTAs unless the video script explicitly contains one.
- After caption, one blank line, then exactly 5 hashtags on a single line:
  First 4 hashtags: tightly relevant to the specific video topic. Mix of mid-volume and long-tail. Must relate to the actual tools/concepts shown. Never use generic tags like #tech or #viral. Never repeat a hashtag that doesn't match the video content.
  5th hashtag: always #ssktechy (hardcoded, non-negotiable).

================================================================
X POST (x_post)
================================================================
Short, punchy post for X (Twitter). Under 280 characters.

Rules:
- Conversational, opinionated, direct. Like texting a friend about something you just found.
- Lead with the outcome or the surprising fact, not the setup.
- No hashtags. No emojis. No threads.
- Can include the tool name but don't force it if it reads better without.
- One or two sentences max. Brevity is everything on X.

================================================================
VOICE (applies to all outputs)
================================================================
Smart friend who just discovered something useful. Direct, specific, conversational. Contractions always. Talk to one person.

Banned: em dashes, en dashes, emojis, ALL CAPS words.
Banned phrases: "game changer", "revolutionize", "unlock", "the future is here", "In today's world", "Here's the thing", "you won't believe", "mind-blowing", "next level", "must-have", "absolutely".

================================================================
OUTPUT FORMAT (strict JSON, no markdown, no code blocks)
================================================================
{
  "ig_caption": "...",
  "yt_title": "...",
  "yt_description": "...",
  "x_post": "..."
}
"""

# ---------------------------------------------------------------------------
# Caption parsing — single source of truth lives in lib/captions.py
# ---------------------------------------------------------------------------

from lib.captions import (  # noqa: E402
    _format_schedule_label,
    _parse_caption,
)
# ---------------------------------------------------------------------------
# Telegram helper (send progress messages from Modal)
# ---------------------------------------------------------------------------


def _tg_send(chat_id: str, text: str, bot_token: str, reply_markup=None):
    """
    Best-effort Telegram send. Failures are logged to stderr (so they show up in
    Modal logs / journalctl) but never re-raised: Telegram is a notification
    channel, not a critical path. If the pipeline can't notify, the pipeline
    must still complete.
    """
    import httpx
    import sys
    import time

    if not bot_token:
        return
    if not chat_id:
        sys.stderr.write("AUTOPUBLISH _tg_send: chat_id empty, skipping\n")
        return

    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            sys.stderr.write(
                f"AUTOPUBLISH _tg_send: HTTP {resp.status_code} on first send, "
                f"retrying with truncated text. body={resp.text[:200]}\n"
            )
            retry_payload = {"chat_id": chat_id, "text": text[:4000]}
            if reply_markup is not None:
                retry_payload["reply_markup"] = reply_markup
            retry_resp = httpx.post(url, json=retry_payload, timeout=10)
            if retry_resp.status_code != 200:
                sys.stderr.write(
                    f"AUTOPUBLISH _tg_send: retry also failed HTTP "
                    f"{retry_resp.status_code}. body={retry_resp.text[:200]}\n"
                )
        time.sleep(0.3)
    except httpx.RequestError as e:
        sys.stderr.write(
            f"AUTOPUBLISH _tg_send: network error {type(e).__name__}: {e}\n"
        )
    except Exception as e:
        sys.stderr.write(
            f"AUTOPUBLISH _tg_send: unexpected error {type(e).__name__}: {e}\n"
        )


def _tg_answer_callback_query(
    callback_query_id: str, bot_token: str, text: str | None = None
):
    """Required after inline button tap — dismisses loading spinner on the client."""
    import httpx

    if not bot_token or not callback_query_id:
        return
    import sys

    try:
        body = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text[:200]
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
            json=body,
            timeout=10,
        )
    except httpx.RequestError as e:
        sys.stderr.write(
            f"AUTOPUBLISH _tg_answer_callback_query: network error "
            f"{type(e).__name__}: {e}\n"
        )
    except Exception as e:
        sys.stderr.write(
            f"AUTOPUBLISH _tg_answer_callback_query: unexpected error "
            f"{type(e).__name__}: {e}\n"
        )


def _telegram_retry_callback_data(job_id: str, key_spec: str) -> str:
    """
    Inline button payload. Telegram limits callback_data to 64 bytes.
    key_spec is ALL or one of VALID_RETRY_KEYS.
    """
    raw = f"r|{job_id}|{key_spec}"
    if len(raw.encode("utf-8")) > 64:
        raise ValueError("callback_data exceeds 64 bytes")
    return raw


RETRY_BUTTON_LABEL = {
    "ig_te": "TE IG",
    "yt_te": "TE YT",
    "fb_te": "TE FB",
    "ig_te_trial": "TE IG trial",
    "ig_en": "EN IG",
    "yt_en": "EN YT",
    "fb_en": "EN FB",
    "x_en": "EN X",
    "ig_en_trial": "EN IG trial",
}


def _retry_dedupe_lookup_key(chat_id: str, job_id: str, keys: list[str]) -> str:
    return f"{chat_id}|{job_id}|{','.join(sorted(keys))}"


def _retry_dedupe_should_block(
    chat_id: str, job_id: str, keys_sorted: list[str]
) -> bool:
    """
    Returns True if this retry is within cooldown (duplicate — do not run again).
    On first seen or expired cooldown, updates timestamp and returns False.
    """
    import os
    import time

    sec = float(os.environ.get("TELEGRAM_RETRY_DEDUPE_SECONDS", "45"))
    k = _retry_dedupe_lookup_key(chat_id, job_id, keys_sorted)
    now = time.time()
    try:
        last = float(retry_dedupe[k])
        if now - last < sec:
            return True
    except KeyError:
        pass
    retry_dedupe[k] = str(now)
    return False


def _retry_dedupe_is_within_cooldown(
    chat_id: str, job_id: str, keys_sorted: list[str]
) -> bool:
    """Read-only: True if `process_telegram_retry` already recorded this combo within the window."""
    import os
    import time

    sec = float(os.environ.get("TELEGRAM_RETRY_DEDUPE_SECONDS", "45"))
    k = _retry_dedupe_lookup_key(chat_id, job_id, keys_sorted)
    now = time.time()
    try:
        last = float(retry_dedupe[k])
        return (now - last) < sec
    except KeyError:
        return False


def _retry_inline_markup(job_id: str, retry_keys: list[str]) -> dict:
    """Telegram reply_markup.inline_keyboard for failed platforms."""
    rows = []
    rows.append(
        [
            {
                "text": "Retry all failed",
                "callback_data": _telegram_retry_callback_data(job_id, "ALL"),
            }
        ]
    )
    row = []
    for k in retry_keys:
        lab = RETRY_BUTTON_LABEL.get(k, k)[:40]
        row.append(
            {"text": lab, "callback_data": _telegram_retry_callback_data(job_id, k)}
        )
        if len(row) >= 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Blotato helpers
# ---------------------------------------------------------------------------


def _blotato_upload_video(video_bytes: bytes, filename: str, api_key: str) -> str:
    import httpx

    presign_resp = httpx.post(
        f"{BLOTATO_BASE}/media/uploads",
        headers={"Content-Type": "application/json", "blotato-api-key": api_key},
        json={"filename": filename},
        timeout=30,
    )
    if presign_resp.status_code != 201:
        raise Exception(
            f"Blotato presign failed: {presign_resp.status_code} {presign_resp.text[:300]}"
        )

    presign_data = presign_resp.json()
    presigned_url = presign_data["presignedUrl"]
    public_url = presign_data["publicUrl"]

    content_type = "video/mp4"
    if filename.endswith(".mov"):
        content_type = "video/quicktime"
    elif filename.endswith(".webm"):
        content_type = "video/webm"

    put_resp = httpx.put(
        presigned_url,
        content=video_bytes,
        headers={"Content-Type": content_type},
        timeout=120,
    )
    if put_resp.status_code >= 400:
        raise Exception(
            f"Blotato PUT failed: {put_resp.status_code} {put_resp.text[:300]}"
        )

    return public_url


def _blotato_post(
    account_id: str,
    platform: str,
    text: str,
    media_urls: list,
    api_key: str,
    *,
    scheduled_time: str = None,
    use_next_slot: bool = False,
    target_extra: dict = None,
) -> dict:
    import httpx

    target = {"targetType": platform}
    if target_extra:
        target.update(target_extra)

    body = {
        "post": {
            "accountId": account_id,
            "content": {
                "text": text,
                "mediaUrls": media_urls,
                "platform": platform,
            },
            "target": target,
        }
    }

    if scheduled_time:
        body["scheduledTime"] = scheduled_time
    elif use_next_slot:
        body["useNextFreeSlot"] = True

    resp = httpx.post(
        f"{BLOTATO_BASE}/posts",
        headers={"Content-Type": "application/json", "blotato-api-key": api_key},
        json=body,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise Exception(
            f"Blotato post failed ({platform}/{account_id}): {resp.status_code} {resp.text[:300]}"
        )

    return resp.json()


def _blotato_poll(submission_id: str, api_key: str, max_wait: int = 30) -> dict:
    """
    Poll Blotato for post status. Per-iteration network errors are logged but
    not raised: the next iteration retries. Returns 'timeout' if no terminal
    status (published/failed/scheduled) seen within max_wait seconds.
    """
    import httpx
    import sys
    import time

    interval = 3
    last_status = ""
    last_err: str | None = None
    for _ in range(max(1, max_wait // interval)):
        time.sleep(interval)
        try:
            resp = httpx.get(
                f"{BLOTATO_BASE}/posts/{submission_id}",
                headers={"blotato-api-key": api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = (data.get("status") or "").strip()
                last_status = status
                if status == "published":
                    return {"status": "published", "url": data.get("publicUrl", "")}
                if status == "failed":
                    return {
                        "status": "failed",
                        "error": data.get("errorMessage", "unknown"),
                    }
                if status == "scheduled":
                    return {"status": "scheduled"}
        except (httpx.RequestError, ValueError) as e:
            last_err = f"{type(e).__name__}: {e}"
            sys.stderr.write(
                f"AUTOPUBLISH _blotato_poll: transient error on submission "
                f"{submission_id}: {last_err}\n"
            )

    out: dict = {"status": "timeout", "last_status": last_status}
    if last_err:
        out["last_error"] = last_err
    return out


# ---------------------------------------------------------------------------
# Transcription (internal)
# ---------------------------------------------------------------------------


def _transcribe(video_path: str, language: str) -> dict:
    import os
    import subprocess
    from groq import Groq

    audio_path = video_path.rsplit(".", 1)[0] + ".mp3"
    is_telugu = language not in ("english", "en")

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    duration = (
        float(probe.stdout.strip())
        if probe.returncode == 0 and probe.stdout.strip()
        else 0
    )

    ffmpeg_cmd = [
        "ffmpeg",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "libmp3lame",
        "-ab",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "1",
        "-y",
        audio_path,
    ]
    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"FFmpeg failed: {result.stderr[:500]}")

    if os.path.getsize(audio_path) == 0:
        raise Exception("FFmpeg produced empty audio")

    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    if is_telugu:
        with open(audio_path, "rb") as f:
            whisper_resp = groq_client.audio.translations.create(
                model="whisper-large-v3",
                file=f,
                response_format="text",
            )
    else:
        with open(audio_path, "rb") as f:
            whisper_resp = groq_client.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=f,
                language="en",
                response_format="text",
            )

    transcript = (
        whisper_resp.strip()
        if isinstance(whisper_resp, str)
        else str(whisper_resp).strip()
    )
    return {"transcript": transcript, "duration": round(duration, 1)}


# ---------------------------------------------------------------------------
# Caption generation (internal)
# ---------------------------------------------------------------------------


def _generate_captions(transcript: str, language: str) -> dict:
    import os
    import json
    from openai import OpenAI
    from tavily import TavilyClient

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

    extract_resp = client.chat.completions.create(
        model="gpt-5.4",
        max_completion_tokens=600,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": EXTRACT_PROMPT},
            {
                "role": "user",
                "content": f"Language: {language}\n\nTranscript:\n{transcript}",
            },
        ],
    )
    extracted = json.loads(extract_resp.choices[0].message.content.strip())
    search_query = extracted.get(
        "search_query", extracted.get("topic", "AI tool tutorial")
    )

    tavily_context = ""
    try:
        search_results = tavily.search(
            query=search_query, search_depth="basic", max_results=3, include_answer=True
        )
        lines = []
        if search_results.get("answer"):
            lines.append("Summary: " + search_results["answer"])
        for r in search_results.get("results", []):
            title = r.get("title", "")
            content = r.get("content", "")[:300]
            if title or content:
                lines.append(f"- {title}: {content}")
        tavily_context = "\n".join(lines)
    except Exception:
        tavily_context = (
            "Search unavailable. Use inferred tool names from transcript only."
        )

    user_message = f"""Topic: {extracted.get('topic', '')}
Primary keyword: {extracted.get('primary_keyword', '')}
Tools (inferred): {', '.join(extracted.get('tools_inferred', []))}
Key actions: {'; '.join(extracted.get('key_actions', []))}
Uncertain: {extracted.get('uncertain', 'none')}

Tavily search results for "{search_query}":
{tavily_context}

Language the video was in: {language}

Generate the metadata now."""

    caption_resp = client.chat.completions.create(
        model="gpt-5.4",
        max_completion_tokens=800,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CAPTION_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )

    result = json.loads(caption_resp.choices[0].message.content.strip())

    for key in ("ig_caption", "yt_title", "yt_description", "x_post"):
        if key not in result:
            result[key] = ""

    result["_search_query"] = search_query
    return result


# ---------------------------------------------------------------------------
# Build posting plan
# ---------------------------------------------------------------------------

from lib.diagnostics import _youtube_safe_text  # noqa: E402


def _build_post_plan(
    tag: str,
    captions: dict,
    public_url: str,
    scheduled_time: str,
    publish_mode: str,
    publish_scope: str = "full",
) -> list:
    """Returns ordered list of post dicts. Trial reel is always last.
    Each entry has a 'key' field for retry targeting (e.g. 'ig_te', 'yt_te', 'ig_te_trial').
    publish_scope: "full" = all platforms (default), "notrial" = drop ig_*_trial, "trialonly" = only ig_*_trial."""
    accts = _get_accounts()[tag]
    media = [public_url]

    sched_kwargs = {}
    if publish_mode == "scheduled" and scheduled_time:
        sched_kwargs["scheduled_time"] = scheduled_time
    elif publish_mode == "next_slot":
        sched_kwargs["use_next_slot"] = True

    plan = []

    plan.append(
        {
            "key": f"ig_{tag}",
            "label": f"IG {accts['ig']['name']}",
            "account_id": accts["ig"]["account_id"],
            "platform": "instagram",
            "text": captions["ig_caption"],
            "media_urls": media,
            "target_extra": {"mediaType": "reel"},
            "delay_after": 3,
            **sched_kwargs,
        }
    )

    yt_title_safe = _youtube_safe_text(captions.get("yt_title", ""))
    yt_desc_safe = _youtube_safe_text(captions.get("yt_description", ""))

    plan.append(
        {
            "key": f"yt_{tag}",
            "label": f"YT {accts['yt']['name']}",
            "account_id": accts["yt"]["account_id"],
            "platform": "youtube",
            "text": yt_desc_safe,
            "media_urls": media,
            "target_extra": {
                "title": yt_title_safe,
                "privacyStatus": "public",
                "shouldNotifySubscribers": publish_mode == "publish",
            },
            "delay_after": 3,
            **sched_kwargs,
        }
    )

    plan.append(
        {
            "key": f"fb_{tag}",
            "label": f"FB {accts['fb']['name']}",
            "account_id": accts["fb"]["account_id"],
            "platform": "facebook",
            "text": captions["ig_caption"],
            "media_urls": media,
            "target_extra": {"mediaType": "reel", "pageId": accts["fb"]["page_id"]},
            "delay_after": 5 if tag == "te" else 3,
            **sched_kwargs,
        }
    )

    if tag == "en":
        plan.append(
            {
                "key": "x_en",
                "label": f"X {accts['x']['name']}",
                "account_id": accts["x"]["account_id"],
                "platform": "twitter",
                "text": captions.get("x_post", captions["ig_caption"]),
                "media_urls": media,
                "target_extra": {},
                "delay_after": 5,
                **sched_kwargs,
            }
        )

    plan.append(
        {
            "key": f"ig_{tag}_trial",
            "label": f"IG {accts['ig']['name']} (trial)",
            "account_id": accts["ig"]["account_id"],
            "platform": "instagram",
            "text": captions["ig_caption"],
            "media_urls": media,
            "target_extra": {
                "mediaType": "reel",
                "trial": {"graduationStrategy": "SS_PERFORMANCE"},
            },
            "delay_after": 0,
            **sched_kwargs,
        }
    )

    if publish_scope == "notrial":
        plan = [p for p in plan if not p["key"].endswith("_trial")]
    elif publish_scope == "trialonly":
        plan = [p for p in plan if p["key"].endswith("_trial")]

    return plan


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------


@app.function(
    image=image,
    secrets=[
        modal.Secret.from_name("api-auth-token"),
        modal.Secret.from_name("openai-secret"),
        modal.Secret.from_name("groq-api-key"),
        modal.Secret.from_name("tavily-api-key"),
        modal.Secret.from_name("blotato-api-key"),
        modal.Secret.from_name("telegram-autopublish-bot"),
        modal.Secret.from_name("accounts-json"),
    ],
    timeout=300,
)
@modal.fastapi_endpoint(method="POST")
def autopublish(data: dict, authorization: str = Header(None)) -> dict:
    import os
    import sys
    import tempfile
    import httpx
    import uuid

    expected = os.environ.get("API_AUTH_TOKEN")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    if authorization.replace("Bearer ", "") != expected:
        raise HTTPException(status_code=403, detail="Invalid token")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    blotato_key = os.environ.get("BLOTATO_API_KEY", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    if not bot_token:
        sys.stderr.write(
            "AUTOPUBLISH-MODAL — TELEGRAM_BOT_TOKEN missing from secret "
            "'telegram-autopublish-bot'. Pipeline will run silently.\n"
        )
    if not chat_id:
        sys.stderr.write(
            "AUTOPUBLISH-MODAL — TELEGRAM_CHAT_ID missing from secret "
            "'telegram-autopublish-bot'. Pipeline will run silently.\n"
        )

    def tg(msg, reply_markup=None):
        if bot_token and chat_id:
            _tg_send(chat_id, msg, bot_token, reply_markup=reply_markup)

    # -------------------------------------------------------------------
    # Retry path: job_id provided -> load cache, skip steps 1-3
    # -------------------------------------------------------------------
    incoming_job_id = data.get("job_id", "").strip()
    retry_platforms = data.get("retry_platforms", None)

    if incoming_job_id and retry_platforms:
        return _handle_retry(
            incoming_job_id,
            retry_platforms,
            blotato_key,
            chat_id,
            bot_token,
            tg,
        )

    # -------------------------------------------------------------------
    # Fresh run
    # -------------------------------------------------------------------
    job_id = str(uuid.uuid4())[:12]

    video_url = data.get("video_url", "").strip()
    caption = data.get("caption", "").strip()

    if not video_url:
        tg("FAILED: No video URL provided.")
        return {"status": "error", "error": "Missing video_url"}

    parsed = _parse_caption(caption)
    tag = parsed["tag"]
    language = parsed["language"]
    scheduled_time = parsed["scheduled_time"]
    publish_mode = parsed["publish_mode"]
    publish_scope = parsed["publish_scope"]

    if not tag:
        tg(
            'No language tag found. Include #te or #en (or "Telugu"/"English") in your caption.'
        )
        return {"status": "error", "error": "No language tag"}

    if publish_scope == "conflict":
        err = (
            "Conflicting tags: caption contains BOTH #notrial and #trialonly. Pick one."
        )
        tg(f"FAILED: {err}\nCaption: {caption[:200]}")
        return {"status": "error", "error": err}

    mode_label = (
        "PUBLISH NOW"
        if publish_mode == "publish"
        else (
            f"SCHEDULE at {_format_schedule_label(scheduled_time)}"
            if publish_mode == "scheduled"
            else "QUEUE to next slot"
        )
    )
    scope_label = {
        "full": "all platforms",
        "notrial": "no trial reel",
        "trialonly": "trial reel only",
    }.get(publish_scope, publish_scope)
    lang_label = "Telugu" if tag == "te" else "English"
    base_total = 5 if tag == "en" else 4
    if publish_scope == "notrial":
        total_posts = base_total - 1
    elif publish_scope == "trialonly":
        total_posts = 1
    else:
        total_posts = base_total
    tg(
        f"Autopublish started ({lang_label}, {mode_label}, {scope_label})\nJob: {job_id}\n{total_posts} posts planned."
    )

    # -----------------------------------------------------------------------
    # Step 1: Download video + upload to Blotato
    # -----------------------------------------------------------------------
    tg("[1/5] Downloading video and uploading to Blotato...")

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=15, read=120, write=30, pool=15),
            follow_redirects=True,
        ) as dl:
            with dl.stream("GET", video_url) as stream:
                if stream.status_code != 200:
                    error = f"Video download failed: HTTP {stream.status_code}"
                    tg(f"FAILED at step 1/5: {error}\nURL: {video_url}")
                    return {
                        "status": "error",
                        "step": 1,
                        "error": error,
                        "job_id": job_id,
                    }
                ct = stream.headers.get("content-type", "")
                if "text/html" in ct:
                    error = "Got HTML instead of video. If Google Drive, the file may require virus scan confirmation or is not shared publicly."
                    tg(f"FAILED at step 1/5: {error}\nURL: {video_url}")
                    return {
                        "status": "error",
                        "step": 1,
                        "error": error,
                        "job_id": job_id,
                    }
                video_bytes = b""
                for chunk in stream.iter_bytes(chunk_size=1024 * 256):
                    video_bytes += chunk
    except Exception as e:
        error = f"Video download error: {type(e).__name__}: {str(e)[:300]}"
        tg(f"FAILED at step 1/5: {error}\nURL: {video_url}")
        return {"status": "error", "step": 1, "error": error, "job_id": job_id}

    video_size_mb = round(len(video_bytes) / (1024 * 1024), 1)

    is_blotato_url = "blotato.com" in video_url or "database.blotato.com" in video_url
    if is_blotato_url:
        public_url = video_url
        tg(f"[1/5] Video already on Blotato ({video_size_mb}MB). Skipping re-upload.")
    else:
        try:
            public_url = _blotato_upload_video(video_bytes, "video.mp4", blotato_key)
            tg(f"[1/5] Video uploaded to Blotato ({video_size_mb}MB).")
        except Exception as e:
            error = f"Blotato upload failed: {str(e)[:300]}"
            tg(
                f"FAILED at step 1/5: {error}\nVideo was downloaded but could not be uploaded to Blotato.\nFix: check Blotato status at my.blotato.com"
            )
            return {"status": "error", "step": 1, "error": error, "job_id": job_id}

    # Cache after step 1
    job_cache[job_id] = {
        "video_url": public_url,
        "tag": tag,
        "language": language,
        "publish_mode": publish_mode,
        "scheduled_time": scheduled_time,
        "publish_scope": publish_scope,
    }

    # -----------------------------------------------------------------------
    # Step 2: Transcribe
    # -----------------------------------------------------------------------
    tg(f"[2/5] Transcribing audio ({lang_label}, Groq Whisper)...")

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        transcription = _transcribe(tmp_path, tag)
        transcript = transcription["transcript"]
        duration = transcription.get("duration", 0)

        if not transcript:
            raise Exception("Whisper returned empty transcript")

        tg(f"[2/5] Transcribed ({duration}s video, {len(transcript)} chars).")
    except Exception as e:
        error = f"Transcription failed: {type(e).__name__}: {str(e)[:300]}"
        tg(
            f"FAILED at step 2/5: {error}\nJob: {job_id}\nVideo is uploaded to Blotato: {public_url}\nNothing was posted. Fix: check if video has audio, or retry."
        )
        return {
            "status": "error",
            "step": 2,
            "error": error,
            "video_url": public_url,
            "job_id": job_id,
        }
    finally:
        import os as _os

        try:
            _os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except OSError as _e:
            import sys as _sys

            _sys.stderr.write(
                f"AUTOPUBLISH transcribe: tmp cleanup failed for {tmp_path}: {_e}\n"
            )

    # Cache after step 2
    cached = job_cache[job_id]
    cached["transcript"] = transcript
    job_cache[job_id] = cached

    # -----------------------------------------------------------------------
    # Step 3: Generate captions
    # -----------------------------------------------------------------------
    tg("[3/5] Generating captions (OpenAI + Tavily)...")

    try:
        captions = _generate_captions(transcript, language)
        preview = captions.get("yt_title", "")[:60]
        tg(f'[3/5] Captions generated. YT title: "{preview}"')
    except Exception as e:
        error = f"Caption generation failed: {type(e).__name__}: {str(e)[:300]}"
        tg(
            f'FAILED at step 3/5: {error}\nJob: {job_id}\nTranscript: "{transcript[:150]}..."\nVideo URL: {public_url}\nFix: retry or provide captions manually.'
        )
        return {
            "status": "error",
            "step": 3,
            "error": error,
            "video_url": public_url,
            "transcript": transcript[:500],
            "job_id": job_id,
        }

    # Cache after step 3
    cached = job_cache[job_id]
    cached["captions"] = captions
    job_cache[job_id] = cached

    # -----------------------------------------------------------------------
    # Step 4 + 5: Post + Poll (skip if dry_run)
    # -----------------------------------------------------------------------
    dry_run = data.get("dry_run", False)

    if dry_run:
        post_plan = _build_post_plan(
            tag, captions, public_url, scheduled_time, publish_mode, publish_scope
        )
        results = [
            {"key": p["key"], "label": p["label"], "status": "skipped (dry_run)"}
            for p in post_plan
        ]
        tg(
            f"[DRY RUN] Skipped posting to {len(post_plan)} platforms.\nCaptions cached under job_id: {job_id}\nTo publish for real, retry with this job_id."
        )

        cached = job_cache[job_id]
        cached["posts"] = results
        job_cache[job_id] = cached

        lines = [f"DRY RUN COMPLETE ({lang_label})", "", "--- Would post to ---"]
        for r in results:
            lines.append(f"  {r['label']} ({r['key']})")
        lines.append("")
        lines.append(f"Job ID: {job_id}")
        lines.append("To publish for real, POST with:")
        lines.append(f'  "job_id": "{job_id}"')
        all_keys = [r["key"] for r in results]
        lines.append(f'  "retry_platforms": {all_keys}')
        lines.append("")
        lines.append("--- Captions ---")
        lines.append(f"IG: {captions.get('ig_caption', '')[:200]}")
        lines.append(f"YT Title: {captions.get('yt_title', '')}")
        if tag == "en":
            lines.append(f"X: {captions.get('x_post', '')[:200]}")
        tg("\n".join(lines))

        return {
            "status": "dry_run",
            "job_id": job_id,
            "tag": tag,
            "publish_mode": publish_mode,
            "video_url": public_url,
            "captions": {
                "ig_caption": captions.get("ig_caption", ""),
                "yt_title": captions.get("yt_title", ""),
                "yt_description": captions.get("yt_description", ""),
                "x_post": captions.get("x_post", ""),
            },
            "posts": results,
        }

    results = _post_and_poll(
        tag,
        captions,
        public_url,
        scheduled_time,
        publish_mode,
        blotato_key,
        tg,
        filter_keys=None,
        publish_scope=publish_scope,
    )

    # Cache after step 4+5
    cached = job_cache[job_id]
    cached["posts"] = results
    job_cache[job_id] = cached

    # -----------------------------------------------------------------------
    # Final confirmation
    # -----------------------------------------------------------------------
    _send_final_report(
        results,
        tag,
        captions,
        mode_label,
        lang_label,
        job_id,
        tg,
        telegram_chat_id=chat_id,
    )

    return {
        "status": "completed",
        "job_id": job_id,
        "tag": tag,
        "publish_mode": publish_mode,
        "scheduled_time": scheduled_time,
        "video_url": public_url,
        "captions": {
            "ig_caption": captions.get("ig_caption", ""),
            "yt_title": captions.get("yt_title", ""),
            "yt_description": captions.get("yt_description", ""),
            "x_post": captions.get("x_post", ""),
        },
        "posts": results,
    }


# ---------------------------------------------------------------------------
# Retry handler
# ---------------------------------------------------------------------------


def _handle_retry(
    job_id: str,
    retry_platforms: list,
    blotato_key: str,
    chat_id: str,
    bot_token: str,
    tg,
) -> dict:
    try:
        cached = job_cache[job_id]
    except KeyError:
        tg(
            f"Retry FAILED: job_id '{job_id}' not found in cache. Run a fresh autopublish instead."
        )
        return {"status": "error", "error": f"job_id '{job_id}' not found"}

    tag = cached["tag"]
    captions = cached["captions"]
    public_url = cached["video_url"]
    scheduled_time = cached.get("scheduled_time")
    publish_mode = cached.get("publish_mode", "publish")
    lang_label = "Telugu" if tag == "te" else "English"
    mode_label = (
        "PUBLISH NOW"
        if publish_mode == "publish"
        else (
            f"SCHEDULE at {_format_schedule_label(scheduled_time)}"
            if publish_mode == "scheduled"
            else "QUEUE to next slot"
        )
    )

    tg(
        f"Retry started for job {job_id}\n{lang_label}, {mode_label}\nRetrying: {', '.join(retry_platforms)}\nCaptions from cache (no credits used)."
    )

    results = _post_and_poll(
        tag,
        captions,
        public_url,
        scheduled_time,
        publish_mode,
        blotato_key,
        tg,
        filter_keys=retry_platforms,
    )

    # Merge new results into cached posts
    old_posts = cached.get("posts", [])
    retried_keys = {r["key"] for r in results}
    merged = [p for p in old_posts if p.get("key") not in retried_keys] + results
    cached["posts"] = merged
    job_cache[job_id] = cached

    _send_final_report(
        results,
        tag,
        captions,
        mode_label,
        lang_label,
        job_id,
        tg,
        is_retry=True,
        telegram_chat_id=chat_id,
    )

    return {
        "status": "completed",
        "job_id": job_id,
        "tag": tag,
        "retried_platforms": retry_platforms,
        "posts": results,
    }


# ---------------------------------------------------------------------------
# Shared posting + polling logic
# ---------------------------------------------------------------------------


def _post_and_poll(
    tag,
    captions,
    public_url,
    scheduled_time,
    publish_mode,
    blotato_key,
    tg,
    filter_keys=None,
    publish_scope: str = "full",
):
    import time

    post_plan = _build_post_plan(
        tag, captions, public_url, scheduled_time, publish_mode, publish_scope
    )

    if filter_keys is not None:
        post_plan = [p for p in post_plan if p["key"] in filter_keys]

    tg(f"[4/5] Posting to {len(post_plan)} platforms...")

    results = []
    for i, post in enumerate(post_plan):
        label = post["label"]
        try:
            target_extra = post.get("target_extra", {})
            resp = _blotato_post(
                account_id=post["account_id"],
                platform=post["platform"],
                text=post["text"],
                media_urls=post["media_urls"],
                api_key=blotato_key,
                scheduled_time=post.get("scheduled_time"),
                use_next_slot=post.get("use_next_slot", False),
                target_extra=target_extra,
            )
            sub_id = resp.get("postSubmissionId", "")
            results.append(
                {
                    "key": post["key"],
                    "label": label,
                    "status": "submitted",
                    "submission_id": sub_id,
                }
            )
        except Exception as e:
            error_msg = str(e)[:200]
            results.append(
                {
                    "key": post["key"],
                    "label": label,
                    "status": "failed",
                    "error": error_msg,
                }
            )

        delay = post.get("delay_after", 0)
        if delay > 0 and i < len(post_plan) - 1:
            time.sleep(delay)

    submitted = [r for r in results if r["status"] == "submitted"]

    post_summary_lines = []
    for r in results:
        if r["status"] == "submitted":
            post_summary_lines.append(
                f"  {r['label']}: submitted (id: {r['submission_id'][:8]}...)"
            )
        else:
            post_summary_lines.append(f"  {r['label']}: FAILED -- {r['error']}")

    tg("[4/5] Posting complete.\n" + "\n".join(post_summary_lines))

    if submitted:
        tg(f"[5/5] Polling {len(submitted)} posts for status...")

        for r in results:
            if r["status"] != "submitted" or not r.get("submission_id"):
                continue
            poll_result = _blotato_poll(r["submission_id"], blotato_key, max_wait=30)
            r["final_status"] = poll_result.get("status", "unknown")
            r["public_url"] = poll_result.get("url", "")
            if poll_result["status"] == "failed":
                r["final_error"] = poll_result.get("error", "")

    return results


# ---------------------------------------------------------------------------
# Final Telegram report
# ---------------------------------------------------------------------------


def _send_final_report(
    results,
    tag,
    captions,
    mode_label,
    lang_label,
    job_id,
    tg,
    is_retry=False,
    telegram_chat_id=None,
):
    header = "RETRY COMPLETE" if is_retry else f"{mode_label} ({lang_label})"
    lines = [header, "", "--- Platforms ---"]

    for r in results:
        if r["status"] == "failed":
            lines.append(f"{r['label']}: FAILED -- {r['error']}")
        elif r.get("final_status") == "published":
            url_part = f" ({r['public_url']})" if r.get("public_url") else ""
            lines.append(f"{r['label']}: published{url_part}")
        elif r.get("final_status") == "scheduled":
            lines.append(f"{r['label']}: scheduled")
        elif r.get("final_status") == "failed":
            lines.append(f"{r['label']}: REJECTED -- {r.get('final_error', 'unknown')}")
        elif r.get("final_status") == "timeout":
            lines.append(
                f"{r['label']}: submitted (status pending, check my.blotato.com)"
            )
        else:
            lines.append(f"{r['label']}: submitted")

    problem_rows = []
    for r in results:
        if r["status"] == "failed":
            problem_rows.append((r, r.get("error", "") or ""))
        elif r.get("final_status") == "failed":
            problem_rows.append((r, r.get("final_error", "") or ""))

    if problem_rows:
        lines.append("")
        lines.append("--- What failed & how to fix ---")
        for r, err in problem_rows:
            err_s = (err or "")[:400]
            lines.append(f"{r['label']}: {err_s}")
            lines.append(f"  → {_diagnose_blotato_error(err)}")
        retry_keys = list(dict.fromkeys([r["key"] for r, _ in problem_rows]))
        lines.append("")
        lines.append("--- Retry ---")
        lines.append(f"Job ID: {job_id}")
        lines.append(
            "Tap a button below, or type: /retry last OR /retry TE Instagram …"
        )
        lines.append(
            "Telegram (easy): /retry last   OR   /retry TE Instagram   OR   /retry EN YouTube"
        )
        lines.append(f"Telegram (exact): /retry {job_id} {' '.join(retry_keys)}")
        lines.append(
            f"CLI: python3 retry_autopublish.py {job_id} "
            + " ".join(retry_keys)
            + "   (.env API_AUTH_TOKEN next to script)"
        )
        lines.append(f'POST: "job_id": "{job_id}", "retry_platforms": {retry_keys}')
        lines.append("Captions cached — no transcription/caption credits.")
    else:
        lines.append("")
        lines.append(f"Job ID: {job_id}")

    if not is_retry:
        lines.append("")
        lines.append("--- Captions ---")
        lines.append(f"IG: {captions.get('ig_caption', '')[:200]}")
        lines.append(f"YT Title: {captions.get('yt_title', '')}")
        if tag == "en":
            lines.append(f"X: {captions.get('x_post', '')[:200]}")

    final_msg = "\n".join(lines)
    reply_markup = None
    if problem_rows and telegram_chat_id:
        rk = list(dict.fromkeys([r["key"] for r, _ in problem_rows]))
        try:
            reply_markup = _retry_inline_markup(job_id, rk)
        except ValueError:
            reply_markup = None
    tg(final_msg, reply_markup=reply_markup)

    if telegram_chat_id:
        try:
            telegram_last_job[str(telegram_chat_id)] = job_id
        except Exception as e:
            import sys as _sys

            _sys.stderr.write(
                f"AUTOPUBLISH telegram_last_job write failed for chat "
                f"{telegram_chat_id}: {type(e).__name__}: {e}\n"
            )


# ---------------------------------------------------------------------------
# Telegram /retry webhook (spawn — returns fast so Telegram does not timeout)
# ---------------------------------------------------------------------------

TELEGRAM_RETRY_HELP = """Autopublish retry (no caption credits)

After a failed publish, use the buttons under the report if shown (Retry all / per platform).

/retry last
  Retry everything that failed on the last job (remembered per chat).

/retry TE Instagram    /retry telugu instagram    /retry EN YouTube
  Plain language — uses your last job id automatically.
  TE = Telugu channels, EN = English. Platforms: Instagram, YouTube, Facebook, X (Twitter).
  Add "trial" for IG trial reel only.

/retry JOB_ID yt_en …
  Original style with internal keys still works.

/help — this message"""


@app.function(
    image=image,
    secrets=[
        modal.Secret.from_name("blotato-api-key"),
        modal.Secret.from_name("telegram-autopublish-bot"),
        modal.Secret.from_name("accounts-json"),
    ],
    timeout=300,
)
def process_telegram_retry(job_id: str, retry_platforms: list, chat_id: str):
    """Background worker: same logic as POST retry, Telegram notifications go to chat_id."""
    import os

    blotato_key = os.environ.get("BLOTATO_API_KEY", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    def tg(msg, reply_markup=None):
        if bot_token:
            _tg_send(chat_id, msg, bot_token, reply_markup=reply_markup)

    keys_sorted = sorted(str(x) for x in retry_platforms)
    if _retry_dedupe_should_block(chat_id, job_id, keys_sorted):
        sec = int(float(os.environ.get("TELEGRAM_RETRY_DEDUPE_SECONDS", "45")))
        tg(
            f"Skipped duplicate retry (same job + platforms within {sec}s).\n"
            f"job `{job_id}` — wait for the in-progress run or try again in a moment."
        )
        return {
            "status": "duplicate_skipped",
            "job_id": job_id,
            "retry_platforms": keys_sorted,
        }

    return _handle_retry(job_id, retry_platforms, blotato_key, chat_id, bot_token, tg)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("telegram-autopublish-bot")],
    timeout=60,
)
@modal.fastapi_endpoint(method="POST")
def telegram_webhook(body: dict):
    """Telegram Bot API updates: messages (/retry …) and inline button callbacks."""
    import os

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    allowed = {x.strip() for x in allowed_raw.split(",") if x.strip()}
    if not allowed:
        raise HTTPException(
            status_code=503,
            detail="TELEGRAM_ALLOWED_CHAT_IDS env var is required (comma-separated chat IDs)",
        )

    cq = body.get("callback_query")
    if cq:
        cq_id = cq.get("id") or ""
        msg_wrap = cq.get("message") or {}
        chat = msg_wrap.get("chat") or {}
        chat_id = str(chat.get("id", "") or "")
        data = (cq.get("data") or "").strip()
        if chat_id not in allowed or not bot_token:
            _tg_answer_callback_query(cq_id, bot_token, "")
            return {"ok": True}
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "r":
            _tg_answer_callback_query(cq_id, bot_token, "Bad button — use /help")
            return {"ok": True}
        jid, spec = parts[1], parts[2]
        if not JOB_ID_TOKEN_RE.match(jid):
            _tg_answer_callback_query(cq_id, bot_token, "Invalid job in button")
            return {"ok": True}
        try:
            cached = job_cache[jid]
        except KeyError:
            _tg_answer_callback_query(cq_id, bot_token, "Job expired — run /help")
            return {"ok": True}
        job_tag = cached.get("tag") or ""
        posts = cached.get("posts") or []
        if spec == "ALL":
            keys = _failed_keys_from_cached_posts(posts)
            if not keys:
                _tg_answer_callback_query(cq_id, bot_token, "No failures on this job")
                return {"ok": True}
        else:
            if spec not in VALID_RETRY_KEYS:
                _tg_answer_callback_query(cq_id, bot_token, "Unknown platform")
                return {"ok": True}
            keys = [spec]
        if not _keys_match_job_tag(keys, job_tag):
            _tg_answer_callback_query(cq_id, bot_token, "Wrong language for this job")
            return {"ok": True}
        ks = sorted(keys)
        if _retry_dedupe_is_within_cooldown(chat_id, jid, ks):
            _tg_answer_callback_query(cq_id, bot_token, "Already queued — wait")
            return {"ok": True}
        process_telegram_retry.spawn(jid, keys, chat_id)
        _tg_answer_callback_query(cq_id, bot_token, "Queued — watch this chat")
        return {"ok": True}

    message = body.get("message") or body.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", "") or "")

    def reply(msg: str):
        if bot_token and chat_id:
            _tg_send(chat_id, msg, bot_token)

    if not text or not chat_id:
        return {"ok": True}

    if chat_id not in allowed:
        return {"ok": True}

    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower()

    if cmd in ("/help", "/start"):
        reply(TELEGRAM_RETRY_HELP)
        return {"ok": True}

    if cmd != "/retry":
        return {"ok": True}

    tokens = [t.strip() for t in parts[1:] if t.strip()]
    if not tokens:
        reply(TELEGRAM_RETRY_HELP)
        return {"ok": True}

    explicit_jid = None
    start_idx = 0
    if tokens[0].lower() == "last":
        start_idx = 1
    elif JOB_ID_TOKEN_RE.match(tokens[0]):
        explicit_jid = tokens[0]
        start_idx = 1

    remaining_tokens = tokens[start_idx:]
    remaining_text = " ".join(remaining_tokens).strip()

    if explicit_jid:
        jid = explicit_jid
    else:
        try:
            jid = telegram_last_job[chat_id]
        except KeyError:
            jid = None
        if not jid:
            reply(
                "No remembered job yet — finish one autopublish first, or paste:\n"
                "/retry YOUR_JOB_ID te instagram\n"
                "(Job id is in the Telegram report.)"
            )
            return {"ok": True}

    try:
        cached = job_cache[jid]
    except KeyError:
        reply(f"Job `{jid}` is not in cache anymore. Run a fresh publish.")
        return {"ok": True}

    job_tag = cached.get("tag") or ""
    posts = cached.get("posts") or []

    if not remaining_text:
        keys = _failed_keys_from_cached_posts(posts)
        if not keys:
            reply(
                f"No failed platforms on job `{jid[:12]}…`. "
                "To force one platform anyway: /retry TE Instagram"
            )
            return {"ok": True}
        ks = sorted(keys)
        if _retry_dedupe_is_within_cooldown(chat_id, jid, ks):
            reply(
                "Already queued — same retry within cooldown. Wait for the run to finish."
            )
            return {"ok": True}
        process_telegram_retry.spawn(jid, keys, chat_id)
        reply(
            "Retrying all failed on last job:\n"
            + ", ".join(keys)
            + f"\n(job `{jid}`)\nWatch this chat."
        )
        return {"ok": True}

    if remaining_tokens and all(t in VALID_RETRY_KEYS for t in remaining_tokens):
        keys = remaining_tokens
        if not _keys_match_job_tag(keys, job_tag):
            reply(
                f"Those platforms don't match this job's language ({job_tag.upper()}). "
                "Use TE… for Telugu jobs or EN… for English, or paste the right job id."
            )
            return {"ok": True}
        ks = sorted(keys)
        if _retry_dedupe_is_within_cooldown(chat_id, jid, ks):
            reply(
                "Already queued — same retry within cooldown. Wait for the run to finish."
            )
            return {"ok": True}
        process_telegram_retry.spawn(jid, keys, chat_id)
        reply(f"Queued retry\njob `{jid}`\nPlatforms: {', '.join(keys)}")
        return {"ok": True}

    keys, infer_err = _infer_retry_keys_natural(
        remaining_text, job_tag if job_tag in ("te", "en") else None
    )
    if infer_err:
        reply(infer_err)
        return {"ok": True}
    if not keys:
        reply("Could not parse platforms. Example: /retry TE Instagram")
        return {"ok": True}
    if not _keys_match_job_tag(keys, job_tag):
        reply(
            f"This job is {'Telugu (TE)' if job_tag == 'te' else 'English (EN)'}. "
            f"Say {'TE' if job_tag == 'te' else 'EN'} + platform, or use job id from the right run."
        )
        return {"ok": True}

    ks = sorted(keys)
    if _retry_dedupe_is_within_cooldown(chat_id, jid, ks):
        reply(
            "Already queued — same retry within cooldown. Wait for the run to finish."
        )
        return {"ok": True}
    process_telegram_retry.spawn(jid, keys, chat_id)
    reply(
        "Queued retry\n"
        f"job `{jid}`\n" + ", ".join(keys) + "\nWatch this chat for progress."
    )
    return {"ok": True}
