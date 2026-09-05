# -*- coding: utf-8 -*-
"""Notifier module - swappable by design.

Only channel: LINE Messaging API (LINE Official Account, Push/Broadcast).

Channel selection:
  1. LINE      if a token (or channel id + secret to mint one) is configured
  2. DRY-RUN   otherwise -> message printed to console/log (used for tests)

A Telegram fallback used to sit between those two. It was removed on 2026-09-05
(Phase 7a): no TELEGRAM_* variable ever existed in .env or in the GitHub
secrets, so the branch had never once executed, yet every caller still had to
reason about a third channel with different quota rules, different recipient
semantics ("to" is a LINE concept) and a different audience classification.
active_channel() is therefore now TWO-VALUED - "line" or "dry-run" - and callers
may rely on that: anything that is not "line" puts nothing on the wire.

Note on the LINE free-tier quota: LINE bills (number of API requests) x (number
of recipients), NOT the number of message objects. One request may carry up to 5
text objects of ~4,900 chars each (~24,500 chars) at the SAME cost as a one-line
message. Everything here is therefore packed into as few requests as possible:
a long text is chunked (`_chunk`) and whole alert cards are packed with
`plan_requests`, so a whole batch of alerts costs ONE push.
"""
import logging
import os

import requests

log = logging.getLogger("steel_intel.notifier")

# --- LINE Messaging API ---
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"
LINE_TOKEN_URL = "https://api.line.me/v2/oauth/accessToken"
LINE_TEXT_LIMIT = 4900      # LINE hard limit is 5000 chars per text object
LINE_MAX_OBJECTS = 5        # LINE allows up to 5 message objects per push request
LINE_QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
LINE_CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"
BLOCK_SEP = "\n\n"   # joiner between two whole cards packed into one text object

_token_cache = {"token": None}  # in-process cache for an auto-issued token

# Sentinel for "send this to everyone who added the OA", as opposed to None,
# which means "whatever LINE_USER_ID says" (the behaviour that shipped first).
BROADCAST = "__broadcast__"


def _resolve_target(to):
    """Which LINE recipient one send should go to.

    None -> legacy env behaviour; BROADCAST -> force broadcast; else push to id.
    Returns the user id to push to, or None to broadcast.

    NOTE: no id-format validation here on purpose - see src/audience.py. This
    module is a dumb pipe: it sends where it is told. Deciding whether an id is
    well formed (and what to do when it is not) is a routing decision and lives
    with the rest of the routing, so tests and manual pokes can address any
    string they like.
    """
    if to is None:
        return os.getenv("LINE_USER_ID") or None
    if to == BROADCAST:
        return None
    return to or None


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


def plan_requests(blocks, limit=LINE_TEXT_LIMIT, max_objects=LINE_MAX_OBJECTS):
    """Pack whole message BLOCKS into as few LINE requests as possible.

    LINE charges a push by (requests x recipients), not by message objects, so
    one request carrying 5 text objects of 4,900 chars (~24,500 chars) costs the
    SAME as one request carrying a single short line. Sending N alerts as N
    requests - which this system used to do - burns N times the quota for no
    reason.

    `blocks` is a list of self-contained strings (one alert card, a header, a
    footer). They are concatenated with BLOCK_SEP while they fit inside one text
    object; a block is NEVER cut in the middle unless it alone exceeds `limit`
    (then, and only then, it is hard-split by _chunk as a last resort).

    Returns a list of {"objects": [str, ...], "blocks": [int, ...]} - the text
    objects of one request and the indices of the source blocks it carries.
    """
    plan = []
    cur_objects, cur_blocks = [], []
    buf, buf_blocks = "", []

    def close_request():
        if cur_objects:
            seen, ordered = set(), []
            for i in cur_blocks:
                if i not in seen:
                    seen.add(i)
                    ordered.append(i)
            plan.append({"objects": list(cur_objects), "blocks": ordered})
        cur_objects.clear()
        cur_blocks.clear()

    def add_object(text, idxs):
        if len(cur_objects) >= max_objects:
            close_request()
        cur_objects.append(text)
        cur_blocks.extend(idxs)

    def flush_buf():
        nonlocal buf, buf_blocks
        if buf_blocks:
            add_object(buf, buf_blocks)
        buf, buf_blocks = "", []

    for idx, block in enumerate(blocks):
        block = block if block is not None else ""
        if len(block) > limit:
            # Pathological single block: cannot be kept whole, split it.
            flush_buf()
            log.warning("oversized alert block hard-split (%d chars)", len(block))
            for piece in _chunk(block, limit):
                add_object(piece, [idx])
            continue
        candidate = f"{buf}{BLOCK_SEP}{block}" if buf_blocks else block
        if len(candidate) <= limit:
            buf, buf_blocks = candidate, buf_blocks + [idx]
        else:
            flush_buf()
            buf, buf_blocks = block, [idx]
    flush_buf()
    close_request()
    return plan


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
    """Return the name of the channel that send() will use right now.

    Exactly two values: "line" (credentials present, messages really go out) or
    "dry-run" (nothing leaves the process). Callers that used to test for a
    third channel can simply test for "line".
    """
    return "line" if _line_configured() else "dry-run"


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


def _send_line_requests(requests_objects, to=None):
    """Send pre-planned LINE requests. `requests_objects` is a list of requests,
    each a list of <=5 text-object strings.

    `to` picks the recipient (see _resolve_target): None keeps the original
    env-driven behaviour, BROADCAST forces a broadcast, an id pushes to it.

    Returns (ok, n_requests) where n_requests is what LINE actually BILLED. A
    401 retry does not count twice: the rejected attempt never reached the
    recipients, so LINE does not charge it.
    """
    if not requests_objects:
        return True, 0
    token = _line_token()
    if not token:
        log.warning("no LINE token available")
        return False, 0

    user_id = _resolve_target(to)
    url = LINE_PUSH_URL if user_id else LINE_BROADCAST_URL
    mode = "push" if user_id else "broadcast"
    log.info("LINE send mode: %s", mode)

    ok, used = True, 0
    for objects in requests_objects:
        messages = [{"type": "text", "text": c} for c in objects]
        payload = {"to": user_id, "messages": messages} if user_id else {"messages": messages}
        sent, unauthorized = _line_post(url, payload, token)
        if unauthorized:  # token expired -> mint a fresh one and retry this batch
            token = _line_token(force_refresh=True)
            sent, _ = _line_post(url, payload, token) if token else (False, False)
        used += 1
        ok = sent and ok
    return ok, used


def _line_plan_for_text(text):
    """Chunk one long text into requests of <=5 objects (legacy packing)."""
    chunks = _chunk(text, LINE_TEXT_LIMIT)
    return [chunks[i:i + LINE_MAX_OBJECTS]
            for i in range(0, len(chunks), LINE_MAX_OBJECTS)]


def _send_line(text, to=None):
    """Send to LINE. If LINE_USER_ID is set -> push to that user; otherwise
    -> broadcast to all friends of the Official Account. Chunks are packed into
    as few requests as possible (<=5 objects each) to conserve push quota. If the
    token is rejected (expired), it is re-minted from id+secret and retried once.
    Returns (ok, n_requests)."""
    return _send_line_requests(_line_plan_for_text(text), to=to)


def _dry_run(text):
    log.info("DRY-RUN (no LINE credentials) - message:")
    print("\n" + "=" * 64)
    print(text)
    print("=" * 64)
    return False


def send_counted(text, to=None):
    """Same as send(), but also reports how many API requests it cost.

    Returns (ok, n_requests). The count is what the quota bookkeeping records;
    on dry-run it is the number of requests the message WOULD have cost, so
    local runs still exercise the budget logic."""
    if active_channel() == "line":
        return _send_line(text, to=to)
    return _dry_run(text), len(_line_plan_for_text(text))


def send(text, to=None):
    """Send a notification through the active channel. Returns True if delivered,
    False on dry-run or delivery failure (failures are logged, never raised)."""
    return send_counted(text, to=to)[0]


def send_blocks(blocks, max_requests=None, to=None):
    """Send whole message blocks packed into as few requests as possible.

    `max_requests` caps how many requests this call may spend; anything that
    does not fit is simply NOT sent (the caller re-offers it next cycle).
    `to` picks the recipient (see _resolve_target).

    Returns (ok, requests_used, covered) where `covered` is the set of block
    indices that were actually delivered in full. A block that got split across
    the boundary between a sent and a dropped request is NOT considered covered
    - the caller must not mark it as done.
    """
    if not blocks:
        return True, 0, set()

    plan = plan_requests(blocks)
    taken = plan if max_requests is None else plan[:max_requests]
    dropped = [] if max_requests is None else plan[max_requests:]
    sent_idx, dropped_idx = set(), set()
    for req in taken:
        sent_idx |= set(req["blocks"])
    for req in dropped:
        dropped_idx |= set(req["blocks"])
    covered = sent_idx - dropped_idx

    if active_channel() == "line":
        ok, used = _send_line_requests([r["objects"] for r in taken], to=to)
        return ok, used, covered
    for req in taken:
        for obj in req["objects"]:
            _dry_run(obj)
    return False, len(taken), covered


def line_quota_status():
    """Read the LINE monthly push allowance and how much of it is already used.

    Returns (limit, used) as ints, or (None, None) when unavailable. These are
    GET endpoints: reading them does NOT consume quota.
    """
    if active_channel() != "line":
        return None, None
    token = _line_token()
    if not token:
        return None, None

    def _get(url, tok):
        return requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=15)

    try:
        resp = _get(LINE_QUOTA_URL, token)
        if resp.status_code == 401:  # expired token -> re-mint once
            token = _line_token(force_refresh=True)
            if not token:
                return None, None
            resp = _get(LINE_QUOTA_URL, token)
        resp.raise_for_status()
        limit = resp.json().get("value")
        resp2 = _get(LINE_CONSUMPTION_URL, token)
        resp2.raise_for_status()
        used = resp2.json().get("totalUsage")
        limit = int(limit) if limit is not None else None
        used = int(used) if used is not None else None
        return limit, used
    except (requests.RequestException, ValueError, TypeError) as exc:
        log.warning("cannot read LINE quota: %s", exc)
        return None, None
