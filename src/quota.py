# -*- coding: utf-8 -*-
"""LINE push-quota bookkeeping.

WHY THIS EXISTS
---------------
LINE bills a push by  (number of API requests) x (number of recipients).
It does NOT bill per message object: one request may carry up to 5 text objects
of ~4,900 chars each, and that costs exactly the same as a one-line message.

So the unit this module counts is the REQUEST, and the monthly allowance in
requests is  line_monthly_quota // line_recipients .

Arithmetic for the current setup (300 messages/month, 2 recipients):
    300 / 2                      = 150 requests/month
    3 digests/day x 31 days      =  93 requests reserved for the daily rounds
    -> ~57 requests/month left for realtime alerts  (~2/day)

Counters live in the `meta` table so they survive across GitHub Actions runs
(the runner disk is ephemeral; the numbers must live in the DB):

    push_req_<YYYY-MM-DD>            all requests sent that day
    push_req_realtime_<YYYY-MM-DD>   realtime-alert requests that day
    push_req_override_<YYYY-MM-DD>   over-budget emergency requests that day
    push_req_month_<YYYY-MM>         all requests this month
    push_req_realtime_month_<YYYY-MM>  realtime requests this month
    quota_warned_<YYYY-MM>           "the 70% warning was already sent"

Every date is computed from Asia/Bangkok wall-clock (`base.now_bkk`), never
from a bare `datetime.now()`: the GitHub runner is UTC and would roll the day
(and the month) over 7 hours late.

NOTHING HERE MAY RAISE. `record()` is called right next to the dead-man's
switch, i.e. exactly when the database is already misbehaving; a bookkeeping
error must never swallow the alert. On Postgres a failed statement aborts the
whole transaction, so every handler rolls back before returning (same pattern
as storage.insert_if_new).
"""
import calendar
import logging

from . import storage
from .sources.base import now_bkk

log = logging.getLogger("steel_intel.quota")


def _now(now=None):
    """Asia/Bangkok wall-clock (never the server's UTC clock)."""
    return now or now_bkk()


# --- meta keys -----------------------------------------------------------

def day_key(now=None):
    return "push_req_" + _now(now).strftime("%Y-%m-%d")


def realtime_key(now=None):
    return "push_req_realtime_" + _now(now).strftime("%Y-%m-%d")


def override_key(now=None):
    return "push_req_override_" + _now(now).strftime("%Y-%m-%d")


def month_key(now=None):
    return "push_req_month_" + _now(now).strftime("%Y-%m")


def realtime_month_key(now=None):
    return "push_req_realtime_month_" + _now(now).strftime("%Y-%m")


def warn_key(now=None):
    return "quota_warned_" + _now(now).strftime("%Y-%m")


# --- counter primitives (never raise) ------------------------------------

def _rollback(con):
    try:
        con.rollback()
    except Exception:  # already closed / no transaction - nothing to undo
        pass


def _get_int(con, key):
    """Counter value, or 0 for missing/garbage/unreadable. Never raises."""
    if con is None:
        return 0
    try:
        raw = storage.get_meta(con, key)
    except Exception as exc:
        log.warning("cannot read quota counter %s: %s", key, exc)
        _rollback(con)
        return 0
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _incr(con, key, delta):
    """Read-modify-write a counter. Returns the new value (0 on failure)."""
    if con is None:
        return 0
    value = _get_int(con, key) + delta
    try:
        storage.set_meta(con, key, str(value))
    except Exception as exc:
        log.warning("cannot write quota counter %s: %s", key, exc)
        _rollback(con)
        return 0
    return value


def record(con, n_requests, kind="other", override=False, now=None):
    """Book `n_requests` LINE requests. NEVER raises - see module docstring."""
    try:
        if con is None or not n_requests or n_requests <= 0:
            return
        stamp = _now(now)
        _incr(con, day_key(stamp), n_requests)
        _incr(con, month_key(stamp), n_requests)
        if kind == "realtime":
            _incr(con, realtime_key(stamp), n_requests)
            _incr(con, realtime_month_key(stamp), n_requests)
        if override:
            _incr(con, override_key(stamp), n_requests)
    except Exception as exc:  # bookkeeping must never break the alert path
        log.warning("quota bookkeeping skipped: %s", exc)
        _rollback(con)


def set_month_requests(con, value, now=None):
    """Backfill the month counter (CLI --quota-set-month) when the real LINE
    usage is known from the console. Never raises."""
    try:
        storage.set_meta(con, month_key(now), str(int(value)))
        return True
    except Exception as exc:
        log.warning("cannot backfill month counter: %s", exc)
        _rollback(con)
        return False


# --- budgets -------------------------------------------------------------

def _int_setting(settings, key, default):
    try:
        return int(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def month_allowance(settings, now=None):
    """Requests/month left for REALTIME alerts after reserving the daily
    digests: monthly_quota/recipients - 3 digests x days_in_month."""
    monthly = _int_setting(settings, "line_monthly_quota", 300)
    recipients = max(1, _int_setting(settings, "line_recipients", 1))
    reserve = _int_setting(settings, "summary_reserve_per_day", 3)
    stamp = _now(now)
    days = calendar.monthrange(stamp.year, stamp.month)[1]
    return max(0, monthly // recipients - reserve * days)


def realtime_budget_left(con, settings, now=None):
    """How many realtime requests may still be spent right now.

    The tighter of the two limits wins: the per-day budget (keeps one busy news
    day from eating the month) and the remaining monthly allowance (keeps the
    month from overshooting the LINE plan)."""
    stamp = _now(now)
    per_day = _int_setting(settings, "alert_push_budget_per_day", 2)
    day_left = max(0, per_day - _get_int(con, realtime_key(stamp)))
    month_left = max(0, month_allowance(settings, stamp)
                     - _get_int(con, realtime_month_key(stamp)))
    return min(day_left, month_left)


def override_left(con, settings, now=None):
    """Emergency requests still allowed today AFTER the normal budget is spent
    (reserved for the levels in alert_override_levels, i.e. RED)."""
    cap = _int_setting(settings, "alert_override_max_per_day", 1)
    return max(0, cap - _get_int(con, override_key(_now(now))))


# --- monthly status / warning -------------------------------------------

def month_status(con, settings, line_used=None, line_limit=None, now=None):
    """Where the month stands, in LINE *messages* (requests x recipients).

    `line_used`/`line_limit` come from the LINE API when reachable (authoritative,
    counts everything including pushes sent from elsewhere); otherwise the local
    request counter multiplied by the number of recipients is used."""
    stamp = _now(now)
    recipients = max(1, _int_setting(settings, "line_recipients", 1))
    limit = int(line_limit) if line_limit else _int_setting(
        settings, "line_monthly_quota", 300)
    local_requests = _get_int(con, month_key(stamp))
    if line_used is not None:
        used, source = int(line_used), "line"
    else:
        used, source = local_requests * recipients, "local"
    ratio = (used / limit) if limit else 0.0
    return {
        "month": stamp.strftime("%Y-%m"),
        "limit": limit,
        "used": used,
        "left": max(0, limit - used),
        "ratio": ratio,
        "recipients": recipients,
        "local_requests": local_requests,
        "source": source,
    }


def pending_month_warning(con, settings, line_used=None, line_limit=None, now=None):
    """Return the month status when the quota crosses `quota_warn_ratio` and the
    warning has not been sent yet this month; otherwise None."""
    try:
        threshold = float(settings.get("quota_warn_ratio", 0.7))
    except (TypeError, ValueError):
        threshold = 0.7
    status = month_status(con, settings, line_used=line_used,
                          line_limit=line_limit, now=now)
    if status["ratio"] < threshold:
        return None
    if _get_int(con, warn_key(_now(now))):  # already warned this month
        return None
    return status


def mark_month_warned(con, now=None):
    """Remember that this month's warning was delivered. Never raises."""
    try:
        storage.set_meta(con, warn_key(_now(now)), "1")
        return True
    except Exception as exc:
        log.warning("cannot mark quota warning: %s", exc)
        _rollback(con)
        return False


# --- human-readable report (CLI --quota) ---------------------------------

def report(con, settings, line_used=None, line_limit=None, now=None):
    """Thai status report. Read-only: sends nothing, costs no quota."""
    stamp = _now(now)
    status = month_status(con, settings, line_used=line_used,
                          line_limit=line_limit, now=now)
    per_day = _int_setting(settings, "alert_push_budget_per_day", 2)
    lines = [
        "โควต้าแจ้งเตือน LINE (นับเป็น 'request' = 1 ครั้งที่ยิง API)",
        f"  วันนี้ {stamp:%Y-%m-%d} : ใช้ไปทั้งหมด {_get_int(con, day_key(stamp))} req"
        f" · เป็นแจ้งเตือนด่วน {_get_int(con, realtime_key(stamp))}/{per_day} req"
        f" · โควต้าฉุกเฉินเหลือ {override_left(con, settings, stamp)} req",
        f"  เดือน {status['month']} : ใช้ไป {status['local_requests']} req"
        f" (= {status['used']} ข้อความ จากเพดาน {status['limit']})"
        f" · เหลือ {status['left']} ข้อความ · ที่มาตัวเลข: {status['source']}",
        f"  งบแจ้งเตือนด่วนที่ยังยิงได้ตอนนี้ : "
        f"{realtime_budget_left(con, settings, stamp)} req"
        f" (เพดานเดือนสำหรับด่วน {month_allowance(settings, stamp)} req)",
        f"  ผู้รับ {status['recipients']} คน → 1 request = {status['recipients']} ข้อความ",
    ]
    return "\n".join(lines)
