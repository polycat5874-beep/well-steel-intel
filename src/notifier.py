# -*- coding: utf-8 -*-
"""Notifier module - swappable by design.

Primary channel: LINE Messaging API (LINE Official Account, Push Message).
Fallback channel: Telegram Bot (kept for flexibility / testing).

Channel selection (in order):
  1. LINE      if LINE_CHANNEL_ACCESS_TOKEN + LINE_USER_ID are set
  2. Telegram  if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set
  3. DRY-RUN   otherwise -> message printed to console/log (used for tests)

Note on LINE free-tier quota (~500 push messages/month): to economise, a long
message is split into chunks and packed into a SINGLE push request (LINE allows
up to 5 message objects per request), so one alert = one push whenever possible.
"""
import logging
import os
import time

import requests

log = logging.getLogger("steel_intel.notifier")

# --- LINE Messaging API ---
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
LINE_TOKEN_URL = "https://api.line.me/v2/oauth/accessToken"
LINE_TEXT_LIMIT = 4900      # LINE hard limit is 5000 chars per text object
LINE_MAX_OBJECTS = 5        # LINE allows up to 5 message objects per push request

_token_cache = {"token": None}  # in-process cache for an auto-issued token

# --- Telegram (fallback) ---
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_LIMIT = 4000       # Telegram hard limit is 4096 chars per message


def _chunk(text, limit):
    """Split text on line boundaries into pieces under `limit` chars."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for line in text.split("\n"):
        # a single over-long line is hard-split as a last resort
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def _line_configured():
    """LINE works as long as we can obtain a token (direct token, or id+secret to
    mint one). LINE_USER_ID is OPTIONAL: if set we push to that user, otherwise we
    broadcast to everyone who added the Official Account as a friend."""
    has_token = bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
    has_creds = bool(os.getenv("LINE_CHANNEL_ID") and os.getenv("LINE_CHANNEL_SECRET"))
    return has_token or has_creds


def _issue_line_token():
    """Mint a short-lived channel access token from channel id + secret.
    Returns the token string, or None on failure."""
    cid = os.getenv("LINE_CHANNEL_ID")
    secret = os.getenv("LINE_CHANNEL_SECRET")
    if not (cid and secret):
        return None
    try:
        resp = requests.post(
            LINE_TOKEN_URL,
            data={"grant_type": "client_credentials",
                  "client_id": cid, "client_secret": secret},
            timeout=20,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if token:
            log.info("issued fresh LINE access token from channel id+secret")
        return token
    except requests.RequestException as exc:
        log.warning("could not issue LINE token: %s", exc)
        return None


def _line_token(force_refresh=False):
    """Get a usable LINE token: prefer the one in .env, fall back to (and cache)
    a freshly minted one. force_refresh re-mints (used after a 401)."""
    if not force_refresh:
        env_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        if env_token:
            return env_token
        if _token_cache["token"]:
            return _token_cache["token"]
    _token_cache["token"] = _issue_line_token()
    return _token_cache["token"]


def active_channel():
    """Return the name of the channel that send() will use right now."""
    if _line_configured():
        return "line"
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        return "telegram"
    return "dry-run"


def _post_with_retry(url, *, headers, json=None, data=None, label=""):
    """POST with up to 3 attempts + backoff. Returns True on success."""
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, headers=headers, json=json, data=data, timeout=20)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            body = getattr(getattr(exc, "response", None), "text", "")
            log.warning("%s send failed (%d/3): %s %s", label, attempt, exc, body[:300])
            if attempt < 3:
                time.sleep(3 * attempt)
    return False


def _line_post(url, payload, token):
    """One LINE request. Returns (ok, unauthorized). unauthorized=True means the
    token was rejected (401), so the caller can re-mint and retry once."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code == 401:
            return False, True
        resp.raise_for_status()
        return True, False
    except requests.RequestException as exc:
        body = getattr(getattr(exc, "response", None), "text", "")
        log.warning("line request failed: %s %s", exc, body[:300])
        return False, False


def _send_line(text):
    """Send to LINE. If LINE_USER_ID is set -> push to that user; otherwise
    -> broadcast to all friends of the Official Account. Chunks are packed into
    as few requests as possible (<=5 objects each) to conserve push quota. If the
    token is rejected (expired), it is re-minted from id+secret and retried once."""
    token = _line_token()
    if not token:
        log.warning("no LINE token available")
        return False

    user_id = os.getenv("LINE_USER_ID")
    url = LINE_PUSH_URL if user_id else LINE_BROADCAST_URL
    mode = "push" if user_id else "broadcast"
    log.info("LINE send mode: %s", mode)

    chunks = _chunk(text, LINE_TEXT_LIMIT)
    ok = True
    for i in range(0, len(chunks), LINE_MAX_OBJECTS):
        batch = chunks[i:i + LINE_MAX_OBJECTS]
        messages = [{"type": "text", "text": c} for c in batch]
        payload = {"to": user_id, "messages": messages} if user_id else {"messages": messages}
        sent, unauthorized = _line_post(url, payload, token)
        if unauthorized:  # token expired -> mint a fresh one and retry this batch
            token = _line_token(force_refresh=True)
            sent, _ = _line_post(url, payload, token) if token else (False, False)
        ok = sent and ok
    return ok


def _send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    ok = True
    for chunk in _chunk(text, TELEGRAM_LIMIT):
        ok = _post_with_retry(
            TELEGRAM_URL.format(token=token),
            headers={},
            data={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            label="telegram",
        ) and ok
    return ok


def _dry_run(text):
    log.info("DRY-RUN (no LINE/Telegram credentials) - message:")
    print("\n" + "=" * 64)
    print(text)
    print("=" * 64)
    return False


def send(text):
    """Send a notification through the active channel. Returns True if delivered,
    False on dry-run or delivery failure (failures are logged, never raised)."""
    channel = active_channel()
    if channel == "line":
        return _send_line(text)
    if channel == "telegram":
        return _send_telegram(text)
    return _dry_run(text)
