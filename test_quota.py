# -*- coding: utf-8 -*-
"""Verify the LINE push-quota guard end-to-end (stdlib only, no pytest).

    python test_quota.py            run all checks
    python test_quota.py --verbose  keep the application logs visible

WHAT IS BEING PROVEN
    A. plan_requests   packs whole alert cards into as few requests as possible
                       and never cuts a card in half.
    B. notifier        one batch of alerts = ONE HTTP request (the actual bug).
    C. quota           counters/budgets are correct and roll over on Bangkok
                       time, and can never raise (they run next to the
                       dead-man's switch).
    D. realtime_job    end-to-end: N news -> 1 request, exactly the rows that
                       went out are marked alerted, nothing is silently lost.
    E. digest          summary + quota warning + dead-man's switch still work.

SAFETY: this file never touches the real Supabase DB, never sends to LINE, and
never fetches news (collect_cycle is stubbed).
"""
import os
import sys

# --- SAFETY FIRST: strip anything that could reach a real service ---------
os.environ.pop("DATABASE_URL", None)          # never write to Supabase
for _k in ("LINE_CHANNEL_ID", "LINE_CHANNEL_SECRET",
           "LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID"):
    os.environ.pop(_k, None)                  # never push to the real LINE OA
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_k, None)

import logging                                # noqa: E402
import shutil                                 # noqa: E402
import tempfile                               # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import main                                   # noqa: E402  (never load_env()!)
from src import notifier, quota, storage, summarizer  # noqa: E402
from src.matcher import Matcher               # noqa: E402
from src.sources.base import BKK_TZ, now_bkk  # noqa: E402

VERBOSE = "--verbose" in sys.argv
if not VERBOSE:
    logging.disable(logging.CRITICAL)

# The collector must NEVER run here: it would fetch ~2,700 live articles.
main.collect_cycle = lambda matcher_obj: (0, 0)

TMP_DIR = tempfile.mkdtemp(prefix="steel-intel-quota-test-")
TEST_DB = os.path.join(TMP_DIR, "test_news.db")
_real_connect = storage.connect
storage.connect = lambda *a, **k: _real_connect(TEST_DB)

SETTINGS = {
    "alert_max_per_cycle": 8,
    "alert_push_budget_per_day": 2,
    "alert_override_levels": ["RED"],
    "alert_override_max_per_day": 1,
    "line_monthly_quota": 300,
    "line_recipients": 2,
    "summary_reserve_per_day": 3,
    "quota_warn_ratio": 0.7,
    "lookback_hours": 24,
    "drop_if_no_date": False,
    "priority_alert_keywords": ["มอก."],
}

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    mark = "[PASS]" if ok else "[FAIL]"
    line = f"{mark} {name}"
    if not ok and detail:
        line += f"\n        -> {detail}"
    print(line)


# --- fixtures ------------------------------------------------------------

def fresh_db():
    """Brand-new empty DB; returns an open connection."""
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    return storage.connect()


def make_matcher(**overrides):
    m = Matcher()
    m.settings = dict(SETTINGS)
    m.settings.update(overrides)
    return m


def card(i, size=600):
    return f"CARD-{i} " + ("ก" * size)


def seed_news(con, n, level="RED", score=12, fresh=True, start=0, tag="ข่าว"):
    """Insert n alert-eligible rows (critical_hits non-empty, level alertable)."""
    when = now_bkk() if fresh else now_bkk() - timedelta(days=3)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
    pairs = []
    for i in range(start, start + n):
        item = {
            "title": f"{tag} {i} สมอ. เตรียมแก้ มอก. 24-2559 ตัดเหล็กเส้นเตา IF",
            "url": f"https://example.test/{tag}/{i}",
            "source": "ทดสอบ",
            "source_name": "ประชาชาติธุรกิจ",
            "published": stamp,
            "published_datetime": stamp,
            "summary": f"เนื้อหาข่าวทดสอบชิ้นที่ {i} สำหรับตรวจสอบการรวบ push ของ LINE.",
        }
        analysis = {
            "topics": ["มาตรฐาน มอก."],
            "critical_hits": ["มอก."],
            "score": score,
            "level": level,
            "impact_notes": ["กระทบสายผลิตเหล็กเส้นเตา IF"],
            "watchlist_hits": [],
        }
        pairs.append((item, analysis))
    storage.insert_many(con, pairs)


def db_rows(con):
    return con.execute("SELECT id, level, alerted FROM news ORDER BY id").fetchall()


def n_alerted(con):
    return sum(1 for r in db_rows(con) if r[2] == 1)


class LineStub:
    """Stands in for notifier._line_post and records every HTTP request."""

    def __init__(self, unauthorized_first=False, always_fail=False):
        self.calls = []
        self.unauthorized_first = unauthorized_first
        self.always_fail = always_fail

    def __call__(self, url, payload, token):
        self.calls.append({"url": url, "payload": payload, "token": token})
        if self.unauthorized_first and len(self.calls) == 1:
            return False, True
        if self.always_fail:
            return False, False
        return True, False

    @property
    def texts(self):
        return [m["text"] for c in self.calls for m in c["payload"]["messages"]]

    @property
    def all_text(self):
        return "\n".join(self.texts)


class line_channel:
    """Context manager: pretend LINE is configured, capture the requests."""

    def __init__(self, stub=None, user_id=None):
        self.stub = stub or LineStub()
        self.user_id = user_id

    def __enter__(self):
        self._saved_post = notifier._line_post
        self._saved_issue = notifier._issue_line_token
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "TEST-TOKEN"
        if self.user_id:
            os.environ["LINE_USER_ID"] = self.user_id
        notifier._line_post = self.stub
        notifier._issue_line_token = lambda: "TEST-TOKEN-REMINTED"
        return self.stub

    def __exit__(self, *exc):
        notifier._line_post = self._saved_post
        notifier._issue_line_token = self._saved_issue
        os.environ.pop("LINE_CHANNEL_ACCESS_TOKEN", None)
        os.environ.pop("LINE_USER_ID", None)
        notifier._token_cache["token"] = None
        return False


# =========================================================================
# A. plan_requests
# =========================================================================

def section_a():
    print("\n--- A. plan_requests (การรวบบล็อกเป็น request) ---")

    check("A1 ไม่มีบล็อก -> ไม่มี request",
          notifier.plan_requests([]) == [])

    one = notifier.plan_requests(["สวัสดี"])
    check("A2 บล็อกเดียว -> 1 request 1 object ข้อความตรงเป๊ะ",
          len(one) == 1 and one[0]["objects"] == ["สวัสดี"] and one[0]["blocks"] == [0],
          repr(one))

    eight = [card(i) for i in range(8)]
    p8 = notifier.plan_requests(eight)
    check("A3 การ์ด 8 ใบ -> 1 request", len(p8) == 1, f"ได้ {len(p8)} request")

    twenty = [card(i) for i in range(20)]
    p20 = notifier.plan_requests(twenty)
    longest = max(len(o) for r in p20 for o in r["objects"])
    check("A4 การ์ด 20 ใบ -> 1 request และทุก object <= 4900 ตัวอักษร",
          len(p20) == 1 and longest <= notifier.LINE_TEXT_LIMIT,
          f"requests={len(p20)} longest={longest}")

    joined = notifier.BLOCK_SEP.join(o for r in p20 for o in r["objects"])
    check("A5 ต่อ object กลับได้เท่ากับต่อบล็อกเดิม (ไม่มีตัวอักษรหาย/เกิน)",
          joined == notifier.BLOCK_SEP.join(twenty))

    objs = [o for r in p20 for o in r["objects"]]
    intact = all(any(c in o for o in objs) for c in twenty)
    check("A6 ไม่มีการ์ดใบไหนถูกตัดกลาง (ทุกใบอยู่ครบใน object เดียว)", intact)

    p60 = notifier.plan_requests([card(i) for i in range(60)])
    check("A7 การ์ด 60 ใบ -> อย่างน้อย 2 request", len(p60) >= 2,
          f"ได้ {len(p60)} request")

    big = notifier.plan_requests(["X" * 6000])
    ok_big = (big and all(len(o) <= notifier.LINE_TEXT_LIMIT
                          for r in big for o in r["objects"])
              and "".join(o for r in big for o in r["objects"]) == "X" * 6000)
    check("A8 บล็อกเดี่ยว 6,000 ตัวอักษร -> ผ่าซอย (_chunk) ได้ครบ", ok_big)

    with line_channel() as stub:
        ok, used, covered = notifier.send_blocks([card(i) for i in range(60)],
                                                 max_requests=1)
    expect = set(p60[0]["blocks"]) - set(
        i for r in p60[1:] for i in r["blocks"])
    check("A9 max_requests=1 -> covered = เฉพาะบล็อกใน request แรกจริง",
          used == 1 and covered == expect and len(stub.calls) == 1,
          f"used={used} covered={len(covered)} expect={len(expect)}")


# =========================================================================
# B. notifier send_blocks / send_counted
# =========================================================================

def section_b():
    print("\n--- B. notifier: ยิงจริงกี่ request ---")

    with line_channel() as stub:
        ok, used, covered = notifier.send_blocks([card(i) for i in range(8)])
    check("B1 การ์ด 8 ใบ -> เรียก _line_post 1 ครั้งเดียว (หัวใจของบั๊ก)",
          len(stub.calls) == 1 and used == 1 and ok and len(covered) == 8,
          f"calls={len(stub.calls)} used={used} ok={ok} covered={len(covered)}")

    with line_channel() as stub:
        notifier.send_blocks(["ก"])
    payload = stub.calls[0]["payload"]
    check("B2 ไม่ตั้ง LINE_USER_ID -> broadcast, payload ไม่มีคีย์ 'to'",
          stub.calls[0]["url"] == notifier.LINE_BROADCAST_URL and "to" not in payload,
          f"url={stub.calls[0]['url']} keys={list(payload)}")

    with line_channel(user_id="Utest123") as stub:
        notifier.send_blocks(["ก"])
    payload = stub.calls[0]["payload"]
    check("B3 ตั้ง LINE_USER_ID -> ใช้ push URL และมี 'to'",
          stub.calls[0]["url"] == notifier.LINE_PUSH_URL
          and payload.get("to") == "Utest123",
          f"url={stub.calls[0]['url']} to={payload.get('to')}")

    with line_channel(stub=LineStub(unauthorized_first=True)) as stub:
        ok, used, _ = notifier.send_blocks([card(i) for i in range(3)])
    check("B4 โดน 401 แล้ว re-mint token + ยิงซ้ำ -> นับเป็น 1 request",
          used == 1 and ok and len(stub.calls) == 2
          and stub.calls[1]["token"] == "TEST-TOKEN-REMINTED",
          f"used={used} ok={ok} calls={len(stub.calls)}")

    with line_channel(stub=LineStub(always_fail=True)) as stub:
        ok, used, _ = notifier.send_blocks([card(i) for i in range(3)])
    check("B5 ส่งไม่สำเร็จ -> ok=False แต่ยังนับ request ที่ยิงไป",
          ok is False and used == 1 and len(stub.calls) == 1,
          f"ok={ok} used={used}")

    long_text = "\n".join("A" * 100 for _ in range(120))  # ~12,100 chars
    with line_channel() as stub:
        ok, used = notifier.send_counted(long_text)
    n_obj = len(stub.calls[0]["payload"]["messages"]) if stub.calls else 0
    check("B6 ข้อความยาว ~12,000 ตัวอักษร -> 3 objects ใน 1 request",
          used == 1 and len(stub.calls) == 1 and n_obj == 3,
          f"used={used} calls={len(stub.calls)} objects={n_obj}")

    with line_channel():
        result = notifier.send("ทดสอบ")
    check("B7 send() ยังคืนค่า bool เหมือนเดิม (test_alert.py เรียกอยู่)",
          isinstance(result, bool) and result is True, repr(result))


# =========================================================================
# C. quota counters
# =========================================================================

def section_c():
    print("\n--- C. quota: ตัวนับ/งบ/การข้ามวัน-เดือน ---")

    con = fresh_db()
    check("C1 DB ใหม่ -> ตัวนับเป็น 0 และงบเต็ม",
          quota.realtime_budget_left(con, SETTINGS) == 2
          and quota._get_int(con, quota.day_key()) == 0)

    quota.record(con, 1, kind="realtime")
    quota.record(con, 1, kind="realtime")
    check("C2 record 2 ครั้ง -> งบวันนี้หมด (0)",
          quota.realtime_budget_left(con, SETTINGS) == 0,
          f"left={quota.realtime_budget_left(con, SETTINGS)}")

    tomorrow = now_bkk() + timedelta(days=1)
    same_month = tomorrow.strftime("%Y-%m") == now_bkk().strftime("%Y-%m")
    month_used = quota._get_int(con, quota.realtime_month_key())
    check("C3 ข้ามวัน -> งบรายวันรีเซ็ต แต่ตัวนับรายเดือนไม่รีเซ็ต",
          (quota.realtime_budget_left(con, SETTINGS, now=tomorrow) == 2 or not same_month)
          and month_used == 2,
          f"month_used={month_used}")

    t_aug = datetime(2026, 8, 31, 23, 30, tzinfo=BKK_TZ)
    t_sep = datetime(2026, 9, 1, 0, 30, tzinfo=BKK_TZ)
    utc_month_of_sep = t_sep.astimezone(timezone.utc).strftime("%Y-%m")
    check("C4 ข้ามเดือนตามเวลาไทย (31 ส.ค. 23:30 = ส.ค. / 1 ก.ย. 00:30 = ก.ย.)",
          quota.month_key(t_aug) == "push_req_month_2026-08"
          and quota.month_key(t_sep) == "push_req_month_2026-09"
          and utc_month_of_sep == "2026-08",  # UTC would still say August
          f"{quota.month_key(t_aug)} / {quota.month_key(t_sep)} / utc={utc_month_of_sep}")

    storage.set_meta(con, quota.day_key(), "ขยะ")
    check("C5 ค่าขยะในตัวนับ -> อ่านได้ 0 ไม่ throw",
          quota._get_int(con, quota.day_key()) == 0)

    con.close()
    raised = None
    try:
        quota.record(con, 1, kind="realtime")
        quota._get_int(con, quota.day_key())
    except Exception as exc:  # noqa: BLE001 - that is exactly what must not happen
        raised = exc
    check("C6 record บน connection ที่ปิดแล้ว -> ไม่ raise", raised is None, repr(raised))

    con = fresh_db()
    allowance = quota.month_allowance(SETTINGS)
    storage.set_meta(con, quota.realtime_month_key(), str(allowance))
    check("C7 เพดานเดือนบังคับได้ (แม้เป็นวันใหม่ที่งบรายวันเต็ม)",
          allowance == 300 // 2 - 3 * 31
          and quota.realtime_budget_left(con, SETTINGS) == 0,
          f"allowance={allowance} left={quota.realtime_budget_left(con, SETTINGS)}")

    con.close()
    con = fresh_db()
    storage.set_meta(con, quota.month_key(), "110")   # 110 req x 2 = 220 / 300
    warn1 = quota.pending_month_warning(con, SETTINGS)
    quota.mark_month_warned(con)
    warn2 = quota.pending_month_warning(con, SETTINGS)
    check("C8 เตือน 70% ครั้งเดียวต่อเดือน",
          warn1 is not None and warn1["used"] == 220 and warn2 is None,
          f"warn1={warn1} warn2={warn2}")

    st = quota.month_status(con, SETTINGS, line_used=250)
    st_local = quota.month_status(con, SETTINGS)
    check("C9 month_status ใช้ line_used ถ้ามี ไม่งั้นคำนวณจากตัวนับ x ผู้รับ",
          st["used"] == 250 and st["source"] == "line"
          and st_local["used"] == 220 and st_local["source"] == "local",
          f"{st['used']}/{st['source']} vs {st_local['used']}/{st_local['source']}")
    con.close()


# =========================================================================
# D. realtime_job end-to-end
# =========================================================================

def run_realtime(matcher_obj, stub=None):
    """Run one realtime cycle with LINE stubbed; returns the stub."""
    with line_channel(stub=stub) as s:
        main.realtime_job(matcher_obj)
    return s


def section_d():
    print("\n--- D. realtime_job ครบวงจร (N ข่าว -> 1 request) ---")
    m = make_matcher()

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    con.close()
    stub = run_realtime(m)
    check("D1 ไม่มีข่าวค้าง -> ไม่ยิง LINE เลย", len(stub.calls) == 0,
          f"calls={len(stub.calls)}")

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 1)
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    check("D2 ข่าว 1 ชิ้น -> 1 request และ mark alerted 1 แถว",
          len(stub.calls) == 1 and n_alerted(con) == 1,
          f"calls={len(stub.calls)} alerted={n_alerted(con)}")
    con.close()

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 8)
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    check("D3 ข่าว 8 ชิ้น -> 1 request เดียว และ alerted ครบ 8 แถว",
          len(stub.calls) == 1 and n_alerted(con) == 8,
          f"calls={len(stub.calls)} alerted={n_alerted(con)}")
    con.close()

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 20)
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    titles_ok = all(f"ข่าว {i} " in stub.all_text for i in range(20))
    check("D4 ข่าว 20 ชิ้น -> 1 request, alerted ครบ 20 แถว, ไม่มีข่าวหาย",
          len(stub.calls) == 1 and n_alerted(con) == 20 and titles_ok,
          f"calls={len(stub.calls)} alerted={n_alerted(con)} titles_ok={titles_ok}")
    con.close()

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 5, level="ORANGE", score=6)
    quota.record(con, 2, kind="realtime")          # daily budget spent
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    check("D5 งบหมด + ไม่มีข่าว RED -> 0 request และไม่ mark แถวไหนเลย",
          len(stub.calls) == 0 and n_alerted(con) == 0,
          f"calls={len(stub.calls)} alerted={n_alerted(con)}")
    con.close()

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 2, level="RED", score=12, tag="แดง")
    seed_news(con, 5, level="ORANGE", score=6, tag="ส้ม")
    quota.record(con, 2, kind="realtime")          # daily budget spent
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    rows = db_rows(con)
    red_alerted = sum(1 for r in rows if r[1] == "RED" and r[2] == 1)
    orange_alerted = sum(1 for r in rows if r[1] == "ORANGE" and r[2] == 1)
    override_used = quota._get_int(con, quota.override_key())
    check("D6 งบหมดแต่มี RED -> ยิง override 1 request เฉพาะ RED, ORANGE ยังค้าง",
          len(stub.calls) == 1 and red_alerted == 2 and orange_alerted == 0
          and override_used == 1,
          f"calls={len(stub.calls)} red={red_alerted} orange={orange_alerted} "
          f"override={override_used}")
    con.close()

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 3, level="RED", score=12)
    quota.record(con, 2, kind="realtime")
    quota.record(con, 1, kind="realtime", override=True)   # override spent too
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    check("D7 งบหมด + โควต้าฉุกเฉินหมด -> 0 request, ไม่ mark แถวไหนเลย",
          len(stub.calls) == 0 and n_alerted(con) == 0,
          f"calls={len(stub.calls)} alerted={n_alerted(con)}")
    con.close()

    con = fresh_db()                                # no baseline_seeded flag
    seed_news(con, 6)
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    seeded = storage.get_meta(con, "baseline_seeded")
    check("D8 (regression) baseline seeding ยังทำงาน: mark ทั้งหมด ไม่ยิงแจ้งเตือน",
          len(stub.calls) == 0 and n_alerted(con) == 6 and seeded == "1",
          f"calls={len(stub.calls)} alerted={n_alerted(con)} flag={seeded}")
    con.close()

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 4, fresh=False)                  # published 3 days ago
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    check("D9 (regression) age gate ยังทำงาน: ข่าวเก่าถูก mark แต่ไม่ยิง",
          len(stub.calls) == 0 and n_alerted(con) == 4,
          f"calls={len(stub.calls)} alerted={n_alerted(con)}")
    con.close()


# =========================================================================
# E. daily digest + dead-man's switch
# =========================================================================

def section_e():
    print("\n--- E. สรุปรายวัน + dead-man's switch ---")
    m = make_matcher()
    saved_quota_status = notifier.line_quota_status
    notifier.line_quota_status = lambda: (None, None)   # never call LINE API
    try:
        con = fresh_db()
        storage.set_meta(con, "last_summary_at", "2000-01-01T00:00:00")
        seed_news(con, 3)
        con.close()
        stub = run_digest(m)
        con = storage.connect()
        day_req = quota._get_int(con, quota.day_key())
        rt_req = quota._get_int(con, quota.realtime_key())
        check("E1 digest ปกติ -> ส่ง 1 request และบันทึกโควต้าแบบ summary",
              len(stub.calls) == 1 and day_req == 1 and rt_req == 0
              and "สรุปข่าวเหล็ก" in stub.all_text,
              f"calls={len(stub.calls)} day={day_req} realtime={rt_req}")
        con.close()

        con = fresh_db()
        storage.set_meta(con, "last_summary_at", "2000-01-01T00:00:00")
        storage.set_meta(con, quota.month_key(), "110")   # 220/300 = 73%
        seed_news(con, 99)
        con.close()
        stub = run_digest(m)
        con = storage.connect()
        all_titles = all(f"ข่าว {i} " in stub.all_text for i in range(99))
        warned = storage.get_meta(con, quota.warn_key())
        check("E2 digest ยาว (RED 99 ชิ้น) -> ส่งครบทุกชิ้น + มีคำเตือนโควต้า",
              all_titles and "โควต้า LINE เดือน" in stub.all_text and warned == "1",
              f"titles_ok={all_titles} warn_in_msg="
              f"{'โควต้า LINE เดือน' in stub.all_text} flag={warned} "
              f"requests={len(stub.calls)}")
        con.close()

        broken = lambda *a, **k: (_ for _ in ()).throw(  # noqa: E731
            RuntimeError("tenant or user not found"))
        saved_connect = storage.connect
        storage.connect = broken
        raised = None
        try:
            stub = run_digest(m)
        except Exception as exc:  # noqa: BLE001
            raised = exc
            stub = None
        finally:
            storage.connect = saved_connect
        got_alert = bool(stub) and "ระบบเฝ้าข่าวขัดข้อง" in stub.all_text
        check("E3 DB ล่ม -> dead-man's switch ยังส่งได้ และไม่มี exception หลุด",
              raised is None and got_alert,
              f"raised={raised!r} alert_sent={got_alert}")
    finally:
        notifier.line_quota_status = saved_quota_status


def run_digest(matcher_obj):
    with line_channel() as s:
        main.daily_summary_job(matcher_obj, "ทดสอบ")
    return s


# =========================================================================

def main_test():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 70)
    print("test_quota.py - ตรวจระบบคุมโควต้า push ของ LINE")
    print("=" * 70)
    try:
        section_a()
        section_b()
        section_c()
        section_d()
        section_e()
    finally:
        storage.connect = _real_connect
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 70)
    print(f"ผลรวม: {passed}/{total} ผ่าน")
    failed = [n for n, ok in RESULTS if not ok]
    if failed:
        print("ไม่ผ่าน:")
        for n in failed:
            print(f"  - {n}")
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main_test()
