# -*- coding: utf-8 -*-
"""Proactive watchlist deadline nudges.

WHY THIS EXISTS
---------------
summarizer.build_watchlist_block already prints a countdown on every digest.
That is PASSIVE, and passive stopped working: the AD wire-rod entry
(deadline 2026-05-31) has printed "เลยกำหนดแล้ว N วัน ตรวจผลด่วน" on every
single digest since June, three rounds a day, and nothing was ever done about
it. A line that appears identically ~270 times becomes furniture.

This module turns the countdown into an EVENT. An entry speaks up only on the
days it has something new to say - on a configured set of days BEFORE its
deadline (settings.watchlist_warn_days) and then on a fixed cadence once it is
OVERDUE (settings.watchlist_overdue_repeat_days) - so when the block does show
up it is rare enough to still be read.

TIME
----
Every date comparison goes through sources.base.now_bkk(). NEVER datetime.now():
this repo has already been bitten once by a UTC runner making Thai timestamps
7 hours wrong, and a reminder that fires on the wrong calendar day is worse
than no reminder.

WHERE IT MAY APPEAR
-------------------
FULL AUDIENCE ONLY. A watchlist title is a dated list of what this company is
afraid of - src/audience.py counts every one of them as a protected sentence,
and summarizer already drops the whole watchlist block from the public digest.
This block rides in exactly the same place, under the same audience test.

COST
----
Zero extra LINE requests. The block is appended to a digest that is being sent
anyway; plan_requests packs a request with up to 5 text objects of ~4,900 chars,
so a handful of extra lines never buys a second request.

NEVER RAISES
------------
This sits on the daily-summary path, which is where the dead-man's switch
lives. A malformed profile, an unparseable deadline or a dead database yields
"" and a log warning - never an exception. An exception here would be reported
as "the watcher is down", which would be a lie.
"""
import hashlib
import logging

from .sources.base import now_bkk

log = logging.getLogger("steel_intel.reminder")

# Days-before-deadline on which an entry speaks up. Sparse on purpose: a nudge
# every day is the very failure this module exists to fix.
DEFAULT_WARN_DAYS = (30, 14, 7, 3, 1)
# Once overdue, repeat this often (in days) rather than every round.
DEFAULT_OVERDUE_REPEAT_DAYS = 7

NUDGE_HEAD = "⏰ ใกล้ครบกำหนด"

# meta keys. Both are pure bookkeeping in the existing `meta` key/value table -
# NO SCHEMA CHANGE. The id is hashed to ASCII (see _entry_id) so a Thai title
# can never end up as a database key.
DAY_META_PREFIX = "wl_nudge_"     # wl_nudge_<id>_<YYYY-MM-DD> -> "1"
LAST_META_PREFIX = "wl_last_"     # wl_last_<id>               -> "YYYY-MM-DD"

_SAFE_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _entry_id(entry):
    """A stable ASCII key for one watchlist entry.

    Prefers the entry's own `id` (config/profile.json already gives every entry
    one) as long as it is plain ASCII; otherwise falls back to a hash of the
    title. The fallback is a hash rather than the title itself so that a Thai
    (or renamed) title never becomes a meta key.
    """
    raw = entry.get("id") if isinstance(entry, dict) else None
    text = str(raw).strip() if raw else ""
    if text and all(ch in _SAFE_ID for ch in text):
        return text[:64]
    title = (entry.get("title") or "") if isinstance(entry, dict) else ""
    return "h" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]


def _parse_deadline(value):
    """'YYYY-MM-DD' -> date, or None. Never raises."""
    if not value:
        return None
    try:
        from datetime import date
        parts = str(value).strip()[:10].split("-")
        if len(parts) != 3:
            return None
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (TypeError, ValueError):
        return None


def _warn_days(settings):
    """The configured days-before list, cleaned. Falls back to the default."""
    raw = (settings or {}).get("watchlist_warn_days", DEFAULT_WARN_DAYS)
    out = set()
    try:
        for value in (raw or []):
            out.add(int(value))
    except (TypeError, ValueError):
        return set(DEFAULT_WARN_DAYS)
    return out or set(DEFAULT_WARN_DAYS)


def _repeat_days(settings):
    """Overdue repeat cadence in days (>=1). Falls back to the default."""
    try:
        value = int((settings or {}).get("watchlist_overdue_repeat_days",
                                         DEFAULT_OVERDUE_REPEAT_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_OVERDUE_REPEAT_DAYS
    return value if value >= 1 else DEFAULT_OVERDUE_REPEAT_DAYS


def _get_meta(con, key):
    """storage.get_meta that swallows a dead database. None when unknown."""
    if con is None:
        return None
    try:
        from . import storage
        return storage.get_meta(con, key)
    except Exception as exc:  # noqa: BLE001 - a reminder may never break a send
        log.warning("cannot read reminder bookkeeping (%s): %s", key, exc)
        return None


def _set_meta(con, key, value):
    """storage.set_meta that swallows a dead database."""
    if con is None:
        return
    try:
        from . import storage
        storage.set_meta(con, key, value)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot record reminder bookkeeping (%s): %s", key, exc)


def due_entries(watchlist, settings=None, con=None, now=None, mark=True):
    """Which watchlist entries have something to say TODAY.

    Returns a list of dicts: {"id", "title", "deadline", "days_left",
    "overdue_days"}; overdue_days is 0 while the deadline is still ahead.

    Two independent gates, both of which must open:
      1. CADENCE - days_left is one of watchlist_warn_days, or the entry is
         overdue and the last nudge was at least watchlist_overdue_repeat_days
         ago (an overdue entry with no history always speaks once).
      2. ONCE A DAY - meta key wl_nudge_<id>_<today>. Digests run three times a
         day; without this the 07:00 nudge would repeat at 12:00 and 18:00.

    `mark=False` computes without writing anything (used by read-only previews).
    Never raises: any failure yields [].
    """
    try:
        today = (now or now_bkk()).date()
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot read the current Bangkok date: %s", exc)
        return []

    try:
        warn_on = _warn_days(settings)
        repeat = _repeat_days(settings)
        stamp = today.isoformat()
        out = []
        for entry in (watchlist or []):
            if not isinstance(entry, dict):
                continue
            deadline = _parse_deadline(entry.get("deadline"))
            if deadline is None:
                continue          # "เฝ้าความเคลื่อนไหว" entries have no clock
            days_left = (deadline - today).days
            eid = _entry_id(entry)

            if days_left >= 0:
                if days_left not in warn_on:
                    continue
            else:
                last = _parse_deadline(_get_meta(con, LAST_META_PREFIX + eid))
                if last is not None and (today - last).days < repeat:
                    continue

            day_key = "%s%s_%s" % (DAY_META_PREFIX, eid, stamp)
            if _get_meta(con, day_key):
                continue          # already nudged today (another round)

            if mark:
                _set_meta(con, day_key, "1")
                _set_meta(con, LAST_META_PREFIX + eid, stamp)
            out.append({
                "id": eid,
                "title": (entry.get("title") or "").strip(),
                "deadline": deadline,
                "days_left": days_left,
                "overdue_days": -days_left if days_left < 0 else 0,
            })
        return out
    except Exception as exc:  # noqa: BLE001 - never break the digest
        log.warning("cannot work out the watchlist nudges: %s", exc)
        return []


def build_nudge_block(entries):
    """Thai block for `entries`, or "" when there is nothing to say.

    Returning "" is load-bearing: the caller appends nothing at all, so a digest
    on a quiet day is byte-for-byte the digest that shipped before this feature
    existed.
    """
    if not entries:
        return ""
    lines = [NUDGE_HEAD]
    for item in entries:
        title = item.get("title") or "(ไม่มีชื่อเรื่อง)"
        deadline = item.get("deadline")
        when = deadline.strftime("%d/%m/%Y") if deadline else "-"
        overdue = item.get("overdue_days") or 0
        if overdue > 0:
            lines.append("• %s — เลยกำหนดแล้ว %d วัน · ยังไม่มีการบันทึกผล"
                         % (title, overdue))
        else:
            lines.append("• %s — เหลือ %d วัน (ครบกำหนด %s)"
                         % (title, item.get("days_left", 0), when))
    return "\n".join(lines)


def nudge_block(watchlist, settings=None, con=None, now=None, mark=True):
    """due_entries() + build_nudge_block() in one call. "" when nothing is due.

    THE RESULT IS FULL-AUDIENCE ONLY - it names watchlist titles. Hand it to
    summarizer.build_daily_summary(due_block=...), which drops it for
    audience="public"; never paste it into a public message by another route.
    """
    return build_nudge_block(
        due_entries(watchlist, settings=settings, con=con, now=now, mark=mark))
