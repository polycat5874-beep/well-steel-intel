# -*- coding: utf-8 -*-
"""Who gets a message, and how much of it they are allowed to see.

WHY THIS EXISTS
---------------
The LINE Official Account broadcasts to EVERYONE who added it. That is how the
team gets the news - and it is also why the full message can never go out that
way: an alert card carries this operator's own risk profile (which furnace the
plant runs, which standards it is exposed to, where it sits), and the digest
carries the watchlist, which is a list of what this company is afraid of.

So a message exists in two versions:
  * audience="full"   -> the private destination (LINE_USER_ID push / Telegram)
  * audience="public" -> the team broadcast: the NEWS, without the reading of it

HOW THE SECRET IS KEPT (read this before changing anything here)
----------------------------------------------------------------
The protection is at the DATA layer (an allow-list of FIELDS), not at the TEXT
layer (a deny-list of words). A real headline may legitimately contain "เตา IF",
"มอก." or "WHA" - those are the news. Blanking words out of a finished message
is both useless (it mangles the news) and dangerous (a short replacement token
once ate the middle of the English word "WHAT").

Instead, the public message builder is handed a dict PROJECTED down to
PUBLIC_ROW_FIELDS. A secret field that somebody adds to the schema in 2027 is
simply NOT IN THE DICT, so it cannot be printed even if every renderer forgets
about it.

Three layers must fail together before anything leaks:
  1. public_row()          - the field never reaches the renderer
  2. audience="public"     - the renderer does not emit the internal blocks
  3. guard_public_blocks() - a last look at the finished text before it is sent
"""
import hashlib
import logging
import os
import re

from . import notifier, storage
from .matcher import load_profile_overlay
from .sources.base import now_bkk

log = logging.getLogger("steel_intel.audience")


# =========================================================================
# A. field projection - the layer everything else rests on
# =========================================================================

# Default-DENY. A field not listed here can never reach the public channel,
# including fields added to the schema after this line was written.
PUBLIC_ROW_FIELDS = frozenset({
    "id", "title", "url", "source", "source_name",
    "published", "published_datetime", "fetched_at",
    "summary", "level", "topics", "also_reported",
})

# For reference, the row fields deliberately kept OUT: score, critical_hits,
# impact_notes, watchlist_hits, hash, story_key, alerted. The first four are
# the operator's own reading of the news; the rest are internal bookkeeping.

REDACTED_BLOCK = ("(ระบบตัดข้อความส่วนนี้ออก — พบข้อมูลภายในปนเข้ามาในฉบับสาธารณะ "
                  "กรุณาแจ้งผู้ดูแลระบบ)")


def public_row(row):
    """Project one row down to the publicly showable fields.

    Always returns a NEW dict and never mutates the input - the caller still
    owns the row that storage handed it.

    Recursion into `also_reported` is not optional: cluster.group_stories does
    `view["also_reported"] = members[1:]`, i.e. it stores the REAL member dicts,
    not copies. A shallow projection would keep those members whole and carry
    their impact_notes straight into the public card.
    """
    if not isinstance(row, dict):
        return {}
    out = {}
    for key, value in row.items():
        if key not in PUBLIC_ROW_FIELDS:
            continue
        if key == "also_reported":
            out[key] = [public_row(other) for other in (value or [])
                        if isinstance(other, dict)]
        elif isinstance(value, list):
            out[key] = list(value)          # copy: never alias the caller's list
        else:
            out[key] = value
    return out


def public_rows(rows):
    """public_row() over a sequence. Returns a new list of new dicts."""
    return [public_row(r) for r in (rows or [])]


# =========================================================================
# B. the guard - last look at a finished public message
# =========================================================================

# Only WHOLE SENTENCES this long or longer are treated as secrets.
MIN_SECRET_LEN = 12

_secrets_cache = None


def _collect(bucket, text):
    """Keep `text` as a secret only if it is a full sentence, not a keyword."""
    value = (text or "").strip()
    # DO NOT relax this length gate to catch keywords. The profile keywords are
    # words like "เตา IF", "มอก.", "WHA" - words that appear in real headlines
    # for entirely innocent reasons. Redacting on those would gut the news, and
    # a short needle matches inside longer words: blanking "WHA" out of a
    # message turns the English word "WHAT" into "T". Sentences only.
    if len(value) >= MIN_SECRET_LEN and value not in bucket:
        bucket.append(value)


def profile_secrets():
    """Full sentences out of the operator profile that must never be broadcast.

    Sources: the note AND the action on every impact boost, the title + note +
    action of every watchlist entry, plus the AI persona. Cached in-process;
    call reset_cache() after changing STEEL_INTEL_PROFILE_JSON.

    An `action` (see src/playbook.py) is if anything MORE sensitive than the
    note it belongs to: a note reads the news, an action says what this company
    has to clear before it may run a furnace. The watchlist actions are watched
    too, although nothing renders them yet - the watchman is deliberately wider
    than the renderer, so the day somebody prints one it is already covered.

    Reads `action` straight off the overlay rather than importing playbook: the
    guard must not depend on the module it is guarding (and playbook imports
    matcher, which would close an import cycle).

    Never raises: an unreadable profile yields [] (the first two layers are the
    real protection - this one is the seatbelt).
    """
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache
    found = []
    try:
        overlay, _source = load_profile_overlay()
        for group in ((overlay.get("company_profile") or {}).get("boosts") or []):
            _collect(found, group.get("note"))
            _collect(found, group.get("action"))
        for item in (overlay.get("watchlist") or []):
            _collect(found, item.get("title"))
            _collect(found, item.get("note"))
            _collect(found, item.get("action"))
        _collect(found, overlay.get("ai_persona"))
    except Exception as exc:  # noqa: BLE001 - a guard may never break a send
        log.warning("cannot load profile secrets for the public guard: %s", exc)
        found = []
    _secrets_cache = found
    return _secrets_cache


def reset_cache():
    """Forget the cached secrets (used by tests that swap the profile)."""
    global _secrets_cache
    _secrets_cache = None


def find_leaks(text):
    """Which profile sentences appear verbatim in `text`. [] when clean."""
    if not text:
        return []
    body = text if isinstance(text, str) else str(text)
    return [s for s in profile_secrets() if s in body]


def guard_public_blocks(blocks):
    """Scan finished public blocks; replace any that leaks. Returns
    (blocks, leaks). Loud on purpose - a silent redaction hides a real bug."""
    out, leaks = [], []
    for block in blocks:
        text = block if isinstance(block, str) else ("" if block is None else str(block))
        found = find_leaks(text)
        if found:
            leaks.extend(found)
            # The sentence itself is NOT logged - the log would become the leak.
            log.error("PUBLIC LEAK BLOCKED: %d internal sentence(s) reached a "
                      "public block; the block was replaced", len(found))
            out.append(REDACTED_BLOCK)
        else:
            out.append(block)
    return out, leaks


def guard_public_text(text):
    """Same guard for a single finished message (the digest).

    Redacts the offending LINES rather than the whole digest, so a stray leak
    costs one line instead of the day's news. If a secret somehow survives that
    (e.g. it spans a line break) the entire message is replaced.
    """
    if not find_leaks(text):
        return text
    log.error("PUBLIC LEAK BLOCKED: internal sentence(s) reached a public "
              "message; the offending line(s) were replaced")
    lines = [REDACTED_BLOCK if find_leaks(line) else line
             for line in text.split("\n")]
    cleaned = "\n".join(lines)
    if find_leaks(cleaned):
        return REDACTED_BLOCK
    return cleaned


# =========================================================================
# C. error masking (EXCEPTION STRINGS ONLY)
# =========================================================================

# NEVER run mask_error() over news text. The 32+ character token rule is written
# for credentials and it will happily eat the path segment of a normal article
# URL. It exists for str(exception) and nothing else.
_MASKS = (
    (re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|redis|amqp)://[^\s'\"]+"),
     "<DSN ถูกปิดบัง>"),
    (re.compile(r"(?i)postgres\.[a-z0-9]{16,}"), "postgres.<ref>"),
    (re.compile(r"(?i)\b[a-z0-9-]+\.(?:supabase\.(?:co|com|net)|pooler\.supabase\.com)\b"),
     "<supabase-host>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<token>"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
)


def mask_error(text):
    """Blank credentials out of an EXCEPTION STRING. Order matters: the DSN goes
    first (it contains a host and a token), the generic token rule goes last."""
    out = text if isinstance(text, str) else ("" if text is None else str(text))
    for pattern, replacement in _MASKS:
        out = pattern.sub(replacement, out)
    return out


# Category -> the marker words that identify it in an exception string. Only the
# Thai label is ever emitted; the raw text never leaves this function.
_ERROR_KINDS = (
    ("ฐานข้อมูลติดต่อไม่ได้ (อาจถูกพักโปรเจค)",
     ("tenant or user not found", "enotfound", "could not translate host",
      "connection refused", "name or service not known", "server closed")),
    ("ฐานข้อมูลตอบช้าเกินกำหนด", ("timeout", "timed out")),
    ("สิทธิ์เข้าฐานข้อมูลไม่ผ่าน",
     ("password authentication", "authentication failed", "permission denied",
      "not authorized")),
    ("โครงสร้างฐานข้อมูลไม่ตรงกับที่ระบบต้องการ",
     ("does not exist", "no such table", "no such column", "undefinedtable")),
    ("เครือข่าย/ปลายทางแจ้งเตือนขัดข้อง",
     ("ssl", "certificate", "connectionerror", "http")),
)


def classify_error(exc):
    """A short Thai CATEGORY for a failure - never any of the raw text.

    The team is told that the watcher is down and roughly why; the machine
    names, DSNs and stack details stay on the private channel.
    """
    try:
        body = str(exc).lower()
    except Exception:  # noqa: BLE001
        return "ระบบภายในขัดข้อง"
    for label, markers in _ERROR_KINDS:
        if any(m in body for m in markers):
            return label
    return "ระบบภายในขัดข้อง"


# =========================================================================
# D. destinations
# =========================================================================

# A LINE id is U/C/R + 32 hex characters. THE FORMAT CHECK LIVES HERE AND
# NOWHERE ELSE - notifier stays a dumb pipe on purpose, so a test (or an
# operator poking at it) can push to any string it likes.
USER_ID_RE = re.compile(r"^[UCR][0-9a-fA-F]{32}$")

TEAM_DIGEST_META = "team_digest_date"
PRIVATE_STATE_META = "dest_private_state"
PRIVATE_FP_META = "dest_private_fp"


def private_user_id():
    """(user_id, state) where state is "ok" | "unset" | "invalid".

    A typo must never make the private channel die quietly, so an id that does
    not parse is reported loudly and the caller falls back to broadcast-only.
    """
    raw = (os.getenv("LINE_USER_ID") or "").strip()
    if not raw:
        return None, "unset"
    if not USER_ID_RE.match(raw):
        log.error("LINE_USER_ID is not a valid LINE id (expected U/C/R plus 32 "
                  "hex chars, got %d chars) - private channel disabled, "
                  "falling back to broadcast only", len(raw))
        return None, "invalid"
    return raw, "ok"


def fingerprint(uid):
    """Stable 8-char tag for an id, so the CLI can prove WHICH id is configured
    without ever printing it."""
    if not uid:
        return "-"
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:8]


def _int_setting(settings, key, default):
    try:
        return int((settings or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def private_target(con=None):
    """The full-detail destination, or None when no valid id is configured.

    `con` is accepted so every destination helper has the same shape; nothing
    here reads the database (the destination must not depend on DB health -
    this is the channel the dead-man's switch reports on).
    """
    uid, _state = private_user_id()
    if not uid:
        return None
    return {
        "key": "private",
        "to": uid,
        "audience": "full",
        "label": "ผู้ดูแล/ผู้บริหาร (ช่องส่วนตัว)",
        "recipients": 1,
    }


def team_target(settings=None):
    """The broadcast destination. Always public."""
    return {
        "key": "team",
        "to": notifier.BROADCAST,
        "audience": "public",
        "label": "ทีมงาน (broadcast ทุกคนที่แอด OA)",
        "recipients": max(1, _int_setting(settings, "line_team_recipients", 1)),
    }


def realtime_targets(settings, con=None):
    """Instant alerts.

    With a private id: the private channel only - realtime fires often and the
    team does not need a stream of raw alerts (set team_realtime_alerts=true to
    change that). Without one: the team gets the public version, because a
    silent watcher is worse than a redacted one.
    """
    targets = []
    private = private_target(con)
    if private:
        targets.append(private)
    if not private or (settings or {}).get("team_realtime_alerts"):
        targets.append(team_target(settings))
    return targets


def should_send_team_digest(con, settings, round_hour):
    """Does the team get the digest on THIS round?

    Includes a CATCH-UP arm: loop.yml hands the baton on roughly every 5 hours,
    so the 18:00 round can fall inside a handover and simply never run. Without
    catch-up the team would get no news that day and nobody would know. Any
    round later than the last configured hour therefore still sends, as long as
    the team has not already been served today.
    """
    try:
        rounds = [int(h) for h in ((settings or {}).get("team_digest_rounds") or [])]
    except (TypeError, ValueError):
        rounds = [18]
    if not rounds:
        return False

    last = None
    if con is not None:
        try:
            last = storage.get_meta(con, TEAM_DIGEST_META)
        except Exception as exc:  # noqa: BLE001 - never break the digest
            log.warning("cannot read %s: %s", TEAM_DIGEST_META, exc)
            last = None
    try:
        today = now_bkk().strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        today = None
    if last and today and last == today:
        return False

    if round_hour is None:
        return False
    try:
        hour = int(round_hour)
    except (TypeError, ValueError):
        return False
    if hour in rounds:
        return True
    return hour > max(rounds)          # catch-up for a missed evening round


def digest_targets(settings, con=None, round_hour=None):
    """Daily summary destinations.

    The private channel gets every round in full. The team gets the public
    version on its configured round(s) - or on every round when there is no
    private channel at all, since then the broadcast is the only channel.
    """
    targets = []
    private = private_target(con)
    if private:
        targets.append(private)
        if should_send_team_digest(con, settings, round_hour):
            targets.append(team_target(settings))
    else:
        targets.append(team_target(settings))
    return targets


def mode_report(settings, con=None):
    """Read-only Thai description of the current routing (CLI --audience).

    Prints a FINGERPRINT, never the id itself: this output ends up in terminals,
    logs and screenshots.
    """
    uid, state = private_user_id()
    state_text = {
        "ok": f"ตั้งค่าแล้ว (ลายนิ้วมือ {fingerprint(uid)})",
        "unset": "ยังไม่ได้ตั้ง LINE_USER_ID",
        "invalid": "⚠️ LINE_USER_ID ผิดรูปแบบ (ต้องเป็น U/C/R + เลขฐาน 16 อีก 32 ตัว)",
    }[state]
    # Imported HERE, not at module scope: playbook imports matcher and this
    # module is imported by summarizer, so a top-level import would close a
    # cycle. The report is also the only thing in this file that needs it.
    from . import playbook

    rounds = (settings or {}).get("team_digest_rounds") or []
    team_rt = bool((settings or {}).get("team_realtime_alerts"))
    pb = playbook.stats()
    last_team = None
    if con is not None:
        try:
            last_team = storage.get_meta(con, TEAM_DIGEST_META)
        except Exception:  # noqa: BLE001
            last_team = None

    lines = [
        "ปลายทางแจ้งเตือนและระดับข้อมูล (อ่านอย่างเดียว ไม่ส่งอะไรทั้งสิ้น)",
        "=" * 66,
        f"  ช่องทางที่ใช้งานอยู่      : {notifier.active_channel()}",
        f"  ช่องส่วนตัว (ฉบับเต็ม)   : {state_text}",
        f"  ทีมงาน (broadcast)      : เปิดเสมอ · ฉบับสาธารณะ "
        f"({team_target(settings)['recipients']} ผู้รับ)",
        "",
        "  รอบที่ทีมงานได้รับสรุป   : "
        + (", ".join(f"{h:02d}:00" for h in rounds) if rounds else "ไม่ส่ง")
        + " (+ รอบชดเชยถ้าเลยเวลาแล้วยังไม่ได้ส่งวันนี้)",
        f"  ทีมงานรับแจ้งเตือนด่วน   : {'ใช่' if team_rt else 'ไม่ (เฉพาะสรุปตามรอบ)'}",
        f"  ส่งสรุปให้ทีมล่าสุด      : {last_team or '-'}",
        "",
        "  ฉบับเต็ม (ส่วนตัว)  = พาดหัว + แหล่ง + เวลา + ลิงก์ + ผลกระทบต่อบริษัท"
        " + คะแนน + คำสำคัญ + watchlist",
        "  ฉบับสาธารณะ (ทีม)  = พาดหัว + แหล่ง + เวลา + ลิงก์ + ระดับความสำคัญ"
        " + 'อีก N สำนักรายงานเรื่องเดียวกัน'",
        f"  ประโยคภายในที่ยามเฝ้าอยู่ : {len(profile_secrets())} ประโยค",
        f"  คู่มือสิ่งที่ต้องทำ       : {pb['with_action']}/{pb['groups']} "
        "กลุ่มความเสี่ยงมี action",
    ]
    if state == "unset":
        lines += [
            "",
            "  หมายเหตุ: ยังไม่มีช่องส่วนตัว → ทุกอย่างออกทาง broadcast เป็นฉบับ"
            "สาธารณะเท่านั้น",
            "  ตั้ง LINE_USER_ID ใน .env (หรือ GitHub secret) แล้วตรวจด้วย"
            " `python main.py --verify-recipient`",
        ]
    return "\n".join(lines)
