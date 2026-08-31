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
    F. clustering      one story carried by several outlets collapses into ONE
                       card, headlines that merely LOOK alike do not, and every
                       merged row is still named on the card and marked alerted.
    G. audience        the team broadcast carries the news and NOTHING from the
                       operator profile, the private channel keeps the full
                       reading, and a malformed destination fails loudly.
    H. archive         the public back-catalogue site carries the news and
                       nothing else: no secret field survives the projection, a
                       whole site built under the sentinel profile is clean, no
                       row is ever hidden, and the build refuses to run when the
                       leak guard would be blind.

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

import contextlib                            # noqa: E402
import io                                     # noqa: E402
import json                                   # noqa: E402
import logging                                # noqa: E402
import shutil                                 # noqa: E402
import tempfile                               # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import main                                   # noqa: E402  (never load_env()!)
from src import audience, cluster, notifier, quota, storage, summarizer  # noqa: E402
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
    # Mirrors config/keywords.json: the team is served the evening digest only,
    # and never the realtime stream.
    "team_digest_rounds": [18],
    "team_realtime_alerts": False,
    "line_team_recipients": 2,
    # Sections A-E measure the quota machinery, and seed_news deliberately builds
    # its rows from ONE template that differs only by an index number - which the
    # story clustering (rightly) reads as the same story. Collapsing is switched
    # OFF here so those checks keep measuring what they were written to measure;
    # section F exercises the clustering on real headlines instead.
    "cluster_enabled": False,
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
# F. story clustering (same-story collapse)
# =========================================================================

# Real Thai steel headlines - the exact shapes that produced the duplicate rows
# this feature exists to fold together.
T_LEAD = "สมอ. ถอนอายัดเหล็ก ซิน เคอ หยวน 6.6 หมื่นเส้น ชี้ผลสอบได้มาตรฐาน"
T_REWRITE = "สมอ. ถอนอายัดเหล็ก ซิน เคอ หยวน 6.6 หมื่นเส้น หลังผลสอบได้มาตรฐาน"
T_SPACING = "สมอ.ถอนอายัดเหล็ก “ซินเคอหยวน” 6.6 หมื่นเส้น ชี้ผลสอบได้มาตรฐาน"
IF_PROTEST = "ผู้ผลิตเตา IF ค้านแนวคิด กมอ. ยกเลิกเหล็กข้ออ้อย"
IF_ANGER = "สมาคมเหล็ก IF โวย มติ กมอ. สั่งเลิกผลิตข้ออ้อย เสียหายแสนล้าน"

SETTINGS_CLUSTER = dict(SETTINGS, cluster_enabled=True)


def r(rid, title, when="2026-08-27T10:00:00", outlet="ทดสอบ", score=12):
    """A bare row dict for the pure clustering checks (no database involved)."""
    return {"id": rid, "title": title, "published_datetime": when,
            "source_name": outlet, "level": "RED", "score": score}


def seed_real_news(con, specs):
    """Insert rows built from REAL headlines.

    seed_news() above is deliberately left untouched: it builds every row from
    one template plus an index, which the clustering correctly reads as a single
    story - fine for the quota sections (they switch clustering off), useless
    here."""
    default = now_bkk().strftime("%Y-%m-%dT%H:%M:%S")
    pairs = []
    for i, spec in enumerate(specs):
        stamp = spec.get("when", default)
        item = {
            "title": spec["title"],
            # A distinct URL per outlet is the whole point: same story, different
            # url+title -> different hash -> separate rows in the database.
            "url": spec.get("url", f"https://example.test/real/{i}"),
            "source": spec.get("outlet", "ทดสอบ"),
            "source_name": spec.get("outlet", "ทดสอบ"),
            "published": stamp,
            "published_datetime": stamp,
            "summary": spec.get("summary", "เนื้อหาข่าวย่อสำหรับทดสอบการยุบเรื่องซ้ำ."),
        }
        analysis = {
            "topics": ["มาตรฐาน มอก."],
            "critical_hits": ["มอก."],
            "score": spec.get("score", 12),
            "level": spec.get("level", "RED"),
            "impact_notes": [],
            "watchlist_hits": [],
        }
        pairs.append((item, analysis))
    storage.insert_many(con, pairs)


def section_f():
    print("\n--- F. ยุบข่าวเรื่องเดียวกัน (story clustering) ---")
    cfg = cluster.build_cfg(SETTINGS_CLUSTER)

    # --- must collapse ----------------------------------------------------
    same3 = cluster.group_stories(
        [r(1, T_LEAD, outlet="ประชาชาติธุรกิจ"),
         r(2, T_LEAD, outlet="มติชนออนไลน์"),
         r(3, T_LEAD, outlet="ท็อปนิวส์")], SETTINGS_CLUSTER, label="F1")
    _, info1 = cluster.same_story(r(1, T_LEAD), r(2, T_LEAD), cfg)
    check("F1 พาดหัวเหมือนเป๊ะ 3 สำนัก -> 1 เรื่อง (reason=key) ids ครบ 3",
          len(same3) == 1 and same3[0]["ids"] == [1, 2, 3]
          and info1["reason"] == "key",
          f"stories={len(same3)} info={info1}")

    ok2, info2 = cluster.same_story(r(1, T_LEAD), r(2, T_REWRITE), cfg)
    check("F2 เรียบเรียงต่างเล็กน้อย -> ยุบ (reason=fuzzy)",
          ok2 and info2["reason"] == "fuzzy" and info2["jaccard"] >= 0.62,
          f"info={info2}")

    ok3, info3 = cluster.same_story(r(1, T_LEAD), r(2, T_SPACING), cfg)
    check("F3 เว้นวรรค/อัญประกาศต่าง -> ยุบ (reason=key)",
          ok3 and info3["reason"] == "key", f"info={info3}")

    # --- must NOT collapse ------------------------------------------------
    ok4, info4 = cluster.same_story(r(1, IF_PROTEST), r(2, IF_ANGER), cfg)
    check("F4 ค้าน vs โวย (คนละเหตุการณ์ คำศัพท์ชุดเดียว) -> ห้ามยุบ",
          not ok4 and info4["jaccard"] < 0.62, f"info={info4}")

    ok5, info5 = cluster.same_story(
        r(1, "เอกนัฏแจงตั้ง กมอ. ชุดใหม่ เร่งสางปัญหาเหล็กไม่ได้มาตรฐาน"),
        r(2, "เอกนัฏลั่นซินเคอหยวนต้องรับผิดชอบ สั่งเดินหน้าคดีถึงที่สุด"), cfg)
    check("F5 ชื่อคนเดียวกันแต่คนละเรื่อง -> ห้ามยุบ", not ok5, f"info={info5}")

    ok6, info6 = cluster.same_story(
        r(1, "ส.อ.ท. หนุนมาตรการคุมนำเข้าเหล็กจากจีน"),
        r(2, "10 สมาคมเหล็กยื่นท้วง สมอ. ปมแก้ มอก. เหล็กเส้น"), cfg)
    check("F6 คนละฝ่าย คนละเรื่อง -> ห้ามยุบ", not ok6, f"info={info6}")

    ok7, info7 = cluster.same_story(
        r(1, "สหรัฐขึ้นภาษีนำเข้าเหล็กเป็น 50% กระทบผู้ส่งออกไทย"),
        r(2, "สหรัฐขึ้นภาษีนำเข้าเหล็กเป็น 25% กระทบผู้ส่งออกไทย"), cfg)
    check("F7 ตัวเลข+หน่วยขัดกัน (50% vs 25%) -> ห้ามยุบ แม้ข้อความคล้ายมาก",
          not ok7 and info7["reason"] == "blocked-unit", f"info={info7}")

    ok8, info8 = cluster.same_story(
        r(1, "สมอ. เตรียมแก้ มอก. 24-2559 ตัดเหล็กเส้นจากเตา IF ออกจากมาตรฐาน"),
        r(2, "สมอ. เตรียมแก้ มอก. 20-2559 ตัดเหล็กเส้นจากเตา IF ออกจากมาตรฐาน"), cfg)
    check("F8 มอก. 24 vs มอก. 20 (คนละมาตรฐาน) -> ห้ามยุบ",
          not ok8 and info8["reason"] == "blocked-unit", f"info={info8}")

    ok9, info9 = cluster.same_story(
        r(1, T_LEAD, when="2026-08-27T10:00:00"),
        r(2, T_LEAD, when="2026-08-24T10:00:00"), cfg)
    check("F9 พาดหัวเดียวกันแต่ห่าง 3 วัน (เหตุการณ์ใหม่) -> ห้ามยุบ",
          not ok9 and info9["reason"] == "blocked-date", f"info={info9}")

    naewna_a = "โลกธุรกิจ - สมอ.ลุยตรวจโรงงานเหล็กทั่วประเทศ - แนวหน้า"
    naewna_b = "โลกธุรกิจ - ค่าไฟงวดใหม่จ่อลดลง 15 สตางค์ - แนวหน้า"
    ok10, info10 = cluster.same_story(r(1, naewna_a), r(2, naewna_b), cfg)
    check("F10 กับดักแนวหน้า (โลกธุรกิจ - ... - แนวหน้า) 2 ข่าวคนละเรื่อง -> ห้ามยุบ",
          not ok10 and cluster.normalize_title(naewna_a) != "โลกธุรกิจ",
          f"info={info10} norm={cluster.normalize_title(naewna_a)!r}")

    ok11, info11 = cluster.same_story(
        r(1, "ศุลกากรจับบารากู่ลักลอบนำเข้า มูลค่า 12 ล้านบาท"),
        r(2, "ศุลกากรจับของหนีภาษีสำแดงเท็จ มูลค่า 30 ล้านบาท"), cfg)
    check("F11 ศุลกากรจับคนละของ -> ห้ามยุบ", not ok11, f"info={info11}")

    # --- end to end: realtime_job -----------------------------------------
    m = make_matcher(cluster_enabled=True)
    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_real_news(con, [
        {"title": T_LEAD, "outlet": "ประชาชาติธุรกิจ", "score": 19},
        {"title": T_LEAD, "outlet": "มติชนออนไลน์", "score": 18},
        {"title": T_LEAD, "outlet": "ท็อปนิวส์", "score": 17},
        {"title": IF_PROTEST, "outlet": "ฐานเศรษฐกิจ", "score": 16},
        {"title": IF_ANGER, "outlet": "กรุงเทพธุรกิจ", "score": 15},
    ])
    con.close()
    stub = run_realtime(m)
    con = storage.connect()
    text = stub.all_text
    cards = text.count("\U0001f6a8 [CRITICAL ALERT")
    alerted = n_alerted(con)
    con.close()
    check("F12 realtime 5 แถว (ซ้ำ 3) -> 1 request, การ์ด 3 ใบ, mark alerted ครบ 5 แถว",
          len(stub.calls) == 1 and cards == 3 and alerted == 5,
          f"calls={len(stub.calls)} cards={cards} alerted={alerted}")

    stub2 = run_realtime(m)
    check("F13 รอบถัดมา -> เงียบ ไม่ยิงซ้ำ (แถวที่ถูกยุบก็ถูก mark ครบ)",
          len(stub2.calls) == 0, f"calls={len(stub2.calls)}")

    outlets = ("ประชาชาติธุรกิจ", "มติชนออนไลน์", "ท็อปนิวส์")
    check("F14 การ์ดบอก 'อีก 2 สำนักรายงานเรื่องเดียวกัน' + ชื่อสำนักครบ 3",
          "อีก 2 สำนักรายงานเรื่องเดียวกัน" in text
          and all(o in text for o in outlets),
          f"missing={[o for o in outlets if o not in text]}")

    # --- the no-hiding rule -----------------------------------------------
    pair = cluster.group_stories(
        [r(1, T_LEAD, outlet="ประชาชาติธุรกิจ"),
         r(2, T_REWRITE, outlet="มติชนออนไลน์")], SETTINGS_CLUSTER, label="F15")
    view = pair[0]["row"]
    card = summarizer.build_critical_alert(view, view)
    check("F15 กฎห้ามซ่อน: ยุบแล้วพาดหัวของสมาชิกที่ต่างต้องยังอยู่ในการ์ด",
          len(pair) == 1 and T_LEAD in card and T_REWRITE in card
          and "มติชนออนไลน์" in card,
          f"stories={len(pair)} rewrite_in_card={T_REWRITE in card}")

    # --- switches / edge cases --------------------------------------------
    off = cluster.group_stories(
        [r(1, T_LEAD), r(2, T_LEAD), r(3, T_LEAD)],
        dict(SETTINGS_CLUSTER, cluster_enabled=False), label="F16")
    check("F16 cluster_enabled=false -> 1 แถว 1 เรื่อง (กลับพฤติกรรมเดิม 100%)",
          len(off) == 3 and all(len(s["ids"]) == 1 for s in off)
          and all(s["row"]["also_reported"] == [] for s in off),
          f"stories={len(off)}")

    one = cluster.group_stories([r(1, T_LEAD)], SETTINGS_CLUSTER, label="F17")
    check("F17 rows ว่าง -> [] และ 1 แถว -> 1 เรื่อง",
          cluster.group_stories([], SETTINGS_CLUSTER) == []
          and cluster.group_stories(None, SETTINGS_CLUSTER) == []
          and len(one) == 1 and one[0]["ids"] == [1])

    raised = None
    try:
        big = cluster.group_stories(
            [r(i, T_LEAD) for i in range(1, 4)],
            dict(SETTINGS_CLUSTER, cluster_max_rows=2), label="F18")
    except Exception as exc:  # noqa: BLE001
        raised, big = exc, []
    check("F18 แถวเกิน cluster_max_rows -> ข้ามการยุบ (identity) ไม่ raise",
          raised is None and len(big) == 3, f"raised={raised!r} stories={len(big)}")

    raised = None
    try:
        weird = cluster.group_stories(
            [{"id": 1}, {"id": 2, "title": None},
             {"id": 3, "title": T_LEAD, "published_datetime": "ไม่ใช่วันที่"}],
            SETTINGS_CLUSTER, label="F19")
    except Exception as exc:  # noqa: BLE001
        raised, weird = exc, []
    check("F19 แถวไม่มี title / วันที่พัง -> ไม่ raise และไม่ยุบมั่ว",
          raised is None and len(weird) == 3,
          f"raised={raised!r} stories={len(weird)}")

    blanks = [r(1, "   "), r(2, "!!!"), r(3, "???")]
    blank_groups = cluster.group_stories(blanks, SETTINGS_CLUSTER, label="F21")
    ok_blank, info_blank = cluster.same_story(blanks[0], blanks[1], cfg)
    check("F21 story_key ว่าง ('') ต้องไม่ถือว่าตรงกัน",
          cluster.story_key("!!!") == "" and not ok_blank
          and len(blank_groups) == 3,
          f"stories={len(blank_groups)} info={info_blank}")

    # --- digest ------------------------------------------------------------
    md = make_matcher(cluster_enabled=True)
    saved_quota_status = notifier.line_quota_status
    notifier.line_quota_status = lambda: (None, None)
    try:
        con = fresh_db()
        storage.set_meta(con, "last_summary_at", "2000-01-01T00:00:00")
        seed_real_news(con, [
            {"title": T_LEAD, "outlet": "ประชาชาติธุรกิจ", "score": 19},
            {"title": T_REWRITE, "outlet": "มติชนออนไลน์", "score": 18},
            {"title": T_SPACING, "outlet": "ท็อปนิวส์", "score": 17},
            {"title": IF_PROTEST, "outlet": "ฐานเศรษฐกิจ", "score": 16},
            {"title": IF_ANGER, "outlet": "กรุงเทพธุรกิจ", "score": 15},
        ])
        con.close()
        dstub = run_digest(md)
    finally:
        notifier.line_quota_status = saved_quota_status
    dtext = dstub.all_text
    missing = [t for t in (T_LEAD, T_REWRITE, IF_PROTEST, IF_ANGER) if t not in dtext]
    check("F20 digest: header 'ข่าวใหม่ 3 เรื่อง (จาก 5 ชิ้น)' + พาดหัวทุกใบยังอยู่",
          "ข่าวใหม่ 3 เรื่อง (จาก 5 ชิ้น)" in dtext and not missing
          and "ท็อปนิวส์" in dtext,
          f"missing={missing}")

    # --- backfill ----------------------------------------------------------
    con = fresh_db()
    seed_real_news(con, [
        {"title": T_LEAD, "outlet": "ประชาชาติธุรกิจ", "score": 19},
        {"title": IF_PROTEST, "outlet": "ฐานเศรษฐกิจ", "score": 18},
        {"title": IF_ANGER, "outlet": "กรุงเทพธุรกิจ", "score": 17},
    ])
    con.execute("UPDATE news SET story_key = NULL")
    con.commit()
    blank_before = con.execute(
        "SELECT COUNT(*) FROM news WHERE story_key IS NULL").fetchone()[0]
    first = storage.ensure_story_keys(con)
    still_blank = con.execute(
        "SELECT COUNT(*) FROM news WHERE story_key IS NULL OR story_key = ''"
    ).fetchone()[0]
    second = storage.ensure_story_keys(con)
    # E4 guard: story_key was appended LAST everywhere, so `SELECT *` must still
    # zip against ROW_COLS - a shifted column would silently corrupt level/score.
    back = storage.get_since(con, "")
    cols_ok = (all(row["level"] == "RED" for row in back)
               and sorted(row["score"] for row in back) == [17, 18, 19]
               and all(row["story_key"] for row in back))
    con.close()
    check("F22 backfill: เติม story_key ครบ, เรียกซ้ำ = 0 แถว, คอลัมน์ไม่สลับ (level/score ถูก)",
          blank_before == 3 and first == 3 and still_blank == 0 and second == 0
          and cols_ok,
          f"before={blank_before} first={first} left={still_blank} "
          f"second={second} cols_ok={cols_ok}")


# =========================================================================
# G. audience: who gets what, and what must never leave the building
# =========================================================================

# A profile whose every sentence is unmistakable. If ANY of these strings shows
# up in a public message, something in the chain leaked - there is no innocent
# way for them to appear in a headline.
SENTINEL_NOTE = "ตราลับหนึ่ง ข้อมูลภายในบริษัทห้ามเผยแพร่สู่สาธารณะ"
SENTINEL_WATCH = "ตราลับสอง เรื่องที่บริษัทเกาะติดเป็นความลับภายใน"
SENTINEL_WNOTE = "ตราลับสาม บันทึกภายในห้ามส่งต่อบุคคลภายนอกเด็ดขาด"
SENTINEL_PERSONA = "ตราลับสี่ บทบาทนักวิเคราะห์ที่บอกตัวตนบริษัทเจ้าของระบบ"
SENTINELS = (SENTINEL_NOTE, SENTINEL_WATCH, SENTINEL_WNOTE, SENTINEL_PERSONA)

SENTINEL_PROFILE = {
    "company_profile": {"boosts": [
        {"keywords": ["เตา IF", "มอก. 24"], "score": 5, "note": SENTINEL_NOTE},
    ]},
    "watchlist": [
        {"id": "sentinel", "title": SENTINEL_WATCH, "deadline": None,
         "keywords": ["มอก.", "เตา IF"], "note": SENTINEL_WNOTE},
    ],
    "ai_persona": SENTINEL_PERSONA,
}

GOOD_ID = "U" + "a1b2c3d4" * 4          # U + 32 hex chars


class using_profile:
    """Context manager: run with a given profile overlay in force."""

    def __init__(self, profile):
        self.profile = profile

    def __enter__(self):
        self._saved = os.environ.get("STEEL_INTEL_PROFILE_JSON")
        if self.profile is None:
            os.environ.pop("STEEL_INTEL_PROFILE_JSON", None)
        else:
            os.environ["STEEL_INTEL_PROFILE_JSON"] = json.dumps(self.profile)
        audience.reset_cache()
        return self

    def __exit__(self, *exc):
        if self._saved is None:
            os.environ.pop("STEEL_INTEL_PROFILE_JSON", None)
        else:
            os.environ["STEEL_INTEL_PROFILE_JSON"] = self._saved
        audience.reset_cache()
        return False


def sentinel_matcher(**overrides):
    """A Matcher carrying the sentinel watchlist (must be built INSIDE
    `using_profile`, since Matcher reads the overlay once, at construction)."""
    m = Matcher()
    m.settings = dict(SETTINGS)
    m.settings.update(overrides)
    return m


def seed_secret_news(con, n=3):
    """Rows whose stored analysis carries the sentinel note + watchlist hit -
    i.e. exactly the fields the public version must not print."""
    stamp = now_bkk().strftime("%Y-%m-%dT%H:%M:%S")
    pairs = []
    for i in range(n):
        item = {
            "title": f"ข่าวลับ {i} สมอ. เตรียมแก้ มอก. 24-2559 ตัดเหล็กเส้นเตา IF",
            "url": f"https://example.test/secret/{i}",
            "source": "ทดสอบ",
            "source_name": "ประชาชาติธุรกิจ",
            "published": stamp,
            "published_datetime": stamp,
            "summary": f"เนื้อหาข่าวทดสอบชิ้นที่ {i} สำหรับตรวจการแยกฉบับสาธารณะ.",
        }
        analysis = {
            "topics": ["มาตรฐาน มอก."],
            "critical_hits": ["มอก."],
            "score": 21,
            "level": "RED",
            "impact_notes": [SENTINEL_NOTE],
            "watchlist_hits": [SENTINEL_WATCH],
        }
        pairs.append((item, analysis))
    storage.insert_many(con, pairs)


def run_realtime_as(matcher_obj, user_id=None):
    with line_channel(user_id=user_id) as s:
        main.realtime_job(matcher_obj)
    return s


def run_digest_as(matcher_obj, user_id=None, round_hour=None):
    # line_quota_status() is a live GET against api.line.me - stubbed here so a
    # test never leaves the machine (same guard section E uses).
    saved = notifier.line_quota_status
    notifier.line_quota_status = lambda: (None, None)
    try:
        with line_channel(user_id=user_id) as s:
            main.daily_summary_job(matcher_obj, "ทดสอบ", round_hour)
    finally:
        notifier.line_quota_status = saved
    return s


def pushed(stub):
    """Text of the requests that went to a specific recipient (push)."""
    return "\n".join(m["text"] for c in stub.calls
                     for m in c["payload"]["messages"] if "to" in c["payload"])


def broadcast(stub):
    """Text of the requests that went to everyone (broadcast)."""
    return "\n".join(m["text"] for c in stub.calls
                     for m in c["payload"]["messages"] if "to" not in c["payload"])


def section_g():
    print("\n--- G. แยกฉบับเต็ม/ฉบับสาธารณะ (audience) ---")

    # --- G1/G2: field projection, the layer everything rests on ------------
    row = {
        "id": 7, "title": "พาดหัวข่าว", "url": "https://x.test/a",
        "source_name": "ประชาชาติธุรกิจ", "level": "RED", "topics": ["มอก."],
        "score": 21, "critical_hits": ["มอก."], "impact_notes": ["โน้ตลับ"],
        "watchlist_hits": ["เกาะติดลับ"], "hash": "deadbeef", "story_key": "k",
        "alerted": 0,
        # The whole point: a field nobody has written yet.
        "secret_new_field_2027": "ความลับที่ยังไม่มีใครเขียนโค้ดรองรับ",
    }
    pub = audience.public_row(row)
    dropped = [k for k in ("score", "critical_hits", "impact_notes",
                           "watchlist_hits", "hash", "story_key", "alerted",
                           "secret_new_field_2027") if k in pub]
    check("G1 ฟิลด์ลับ (รวมฟิลด์ใหม่ที่ยังไม่มีใครเขียน) ไม่ติดไปกับฉบับสาธารณะ",
          not dropped and pub["title"] == "พาดหัวข่าว" and pub["level"] == "RED"
          and "secret_new_field_2027" in row,   # ต้นฉบับต้องไม่ถูกแก้
          f"หลุดมา: {dropped}")

    member = {"id": 8, "title": "พาดหัวสำนักอื่น", "source_name": "มติชน",
              "impact_notes": ["โน้ตลับของสมาชิก"], "score": 19}
    grouped = dict(row)
    grouped["also_reported"] = [member]        # cluster.py เก็บ dict ตัวจริง
    pub2 = audience.public_row(grouped)
    inner = pub2["also_reported"][0]
    check("G2 ฉายลึกถึง also_reported (สมาชิกที่ถูกยุบก็ต้องโดนตัดฟิลด์ลับ)",
          "impact_notes" not in inner and "score" not in inner
          and inner["title"] == "พาดหัวสำนักอื่น"
          and "impact_notes" in member,        # ต้นฉบับสมาชิกต้องไม่ถูกแก้
          f"inner={sorted(inner)}")

    # --- G3: the public alert card ---------------------------------------
    full_row = dict(row)
    full_row["published_datetime"] = "2026-08-27T10:00:00"
    full_row["summary"] = "เนื้อหาข่าวย่อสำหรับตรวจการ์ด."
    full_row["also_reported"] = [dict(member)]
    pub_card = summarizer.build_critical_alert(
        audience.public_row(full_row), audience.public_row(full_row),
        index=1, total=1, audience="public")
    full_card = summarizer.build_critical_alert(full_row, full_row, index=1, total=1)
    hidden_ok = not any(m in pub_card for m in
                        ("ผลกระทบต่อบริษัท", "คะแนน", "คำสำคัญที่พบ",
                         "⏳ เกาะติด", "โน้ตลับ", "เกาะติดลับ"))
    kept_ok = all(m in pub_card for m in
                  ("พาดหัวข่าว", "ประชาชาติธุรกิจ", "27/08/2026",
                   "https://x.test/a", "ระดับความสำคัญ", "ต้องรู้วันนี้",
                   "อีก 1 สำนักรายงานเรื่องเดียวกัน", "พาดหัวสำนักอื่น"))
    full_ok = "💥 ผลกระทบต่อบริษัท" in full_card and "โน้ตลับ" in full_card
    check("G3 การ์ดฉบับสาธารณะ: ตัดผลกระทบ/คะแนน/คำสำคัญ/เกาะติด แต่คงข่าว "
          "+ 'อีก N สำนัก' (ฉบับเต็มยังเหมือนเดิม)",
          hidden_ok and kept_ok and full_ok,
          f"hidden_ok={hidden_ok} kept_ok={kept_ok} full_ok={full_ok}\n{pub_card}")

    # --- G4: nothing from the operator profile may reach the broadcast ----
    with using_profile(SENTINEL_PROFILE):
        m_s = sentinel_matcher()
        con = fresh_db()
        storage.set_meta(con, "baseline_seeded", "1")
        seed_secret_news(con, 3)
        con.close()
        rt = run_realtime_as(m_s)
        con = fresh_db()
        storage.set_meta(con, "baseline_seeded", "1")
        storage.set_meta(con, "last_summary_at", "2000-01-01T00:00:00")
        seed_secret_news(con, 3)
        con.close()
        dg = run_digest_as(m_s)
        public_text = rt.all_text + "\n" + dg.all_text
        leaked = [s for s in SENTINELS if s in public_text]
        # No redaction marker either: layers 1-2 must do the job on their own,
        # or the guard is quietly carrying the whole design.
        guard_fired = audience.REDACTED_BLOCK in public_text
        news_ok = "ข่าวลับ 0" in public_text        # the NEWS still goes out

    # Same again against the operator's REAL profile when one is installed on
    # this machine (empty list on CI, where the file is absent by design).
    with using_profile(None):
        real_secrets = audience.profile_secrets()
        m_r = make_matcher()
        con = fresh_db()
        storage.set_meta(con, "baseline_seeded", "1")
        storage.set_meta(con, "last_summary_at", "2000-01-01T00:00:00")
        seed_news(con, 3)
        con.close()
        real_pub = run_digest_as(m_r).all_text
        real_leaked = [s for s in real_secrets if s in real_pub]
    check("G4 โปรไฟล์ลับไม่หลุดออกฉบับสาธารณะเลยสักประโยค "
          f"(ตรา 4 ประโยค + โปรไฟล์จริง {len(real_secrets)} ประโยค)",
          not leaked and not guard_fired and news_ok and not real_leaked,
          f"leaked={leaked} guard_fired={guard_fired} news_ok={news_ok} "
          f"real_leaked={len(real_leaked)}")

    # --- G5: the public digest --------------------------------------------
    wl = [{"id": "w", "title": "เรื่องลับที่เกาะติด", "deadline": None,
           "keywords": [], "note": "โน้ตลับของ watchlist"}]
    items = [{"id": 1, "title": "พาดหัวสรุป", "level": "RED",
              "topics": ["มอก."], "source_name": "ประชาชาติธุรกิจ",
              "published_datetime": "2026-08-27T10:00:00",
              "url": "https://x.test/s", "summary": "ย่อ."}]
    pub_digest = summarizer.build_daily_summary(
        items, wl, "ทดสอบ", health="ระบบปกติ · รอบนี้ตรวจข่าว 5 ชิ้น",
        audience="public")
    pub_empty = summarizer.build_daily_summary(
        [], wl, "ทดสอบ", health="ระบบปกติ · รอบนี้ตรวจข่าว 0 ชิ้น",
        audience="public")
    full_digest = summarizer.build_daily_summary(items, wl, "ทดสอบ")
    check("G5 สรุปฉบับสาธารณะ: ไม่มี watchlist ทั้งกรณีมีข่าวและไม่มีข่าว "
          "แต่ยังมีหัวเรื่อง/พาดหัว/🩺 (ฉบับเต็มยังมี watchlist)",
          "เรื่องที่เกาะติด (Watchlist)" not in pub_digest
          and "เรื่องลับที่เกาะติด" not in pub_digest
          and "เรื่องที่เกาะติด (Watchlist)" not in pub_empty
          and "เรื่องลับที่เกาะติด" not in pub_empty
          and "สรุปข่าวเหล็ก" in pub_digest and "พาดหัวสรุป" in pub_digest
          and "🩺" in pub_digest and "🩺" in pub_empty
          and "เรื่องที่เกาะติด (Watchlist)" in full_digest,
          f"pub_has_wl={'Watchlist' in pub_digest} "
          f"empty_has_wl={'Watchlist' in pub_empty}")

    # --- G6/G7: realtime routing ------------------------------------------
    m = make_matcher()
    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 3)
    con.close()
    no_priv = run_realtime_as(m)
    check("G6 ไม่มี ID ส่วนตัว -> broadcast 1 request เป็นฉบับสาธารณะ "
          "(ทีมยังได้ข่าว)",
          len(no_priv.calls) == 1 and pushed(no_priv) == ""
          and "ข่าว 0" in broadcast(no_priv)
          and "ผลกระทบต่อบริษัท" not in broadcast(no_priv),
          f"calls={len(no_priv.calls)} push={len(pushed(no_priv))}")

    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 3)
    con.close()
    priv = run_realtime_as(m, user_id=GOOD_ID)
    check("G7 มี ID ส่วนตัว -> แจ้งเตือนด่วน push ฉบับเต็มถึงคนเดียว "
          "ทีมไม่ได้รับ",
          len(priv.calls) == 1 and broadcast(priv) == ""
          and "💥 ผลกระทบต่อบริษัท" in pushed(priv)
          and "กระทบสายผลิตเหล็กเส้นเตา IF" in pushed(priv),
          f"calls={len(priv.calls)} broadcast={len(broadcast(priv))}")

    # --- G8/G9: digest routing per round ----------------------------------
    con = fresh_db()
    storage.set_meta(con, "last_summary_at", "2000-01-01T00:00:00")
    seed_news(con, 3)
    con.close()
    even = run_digest_as(m, user_id=GOOD_ID, round_hour=18)
    con = storage.connect()
    team_date = storage.get_meta(con, audience.TEAM_DIGEST_META)
    priv_state = storage.get_meta(con, audience.PRIVATE_STATE_META)
    priv_fp = storage.get_meta(con, audience.PRIVATE_FP_META)
    con.close()
    check("G8 สรุปรอบ 18:00 + มี ID -> 2 request (ส่วนตัวมี watchlist / "
          "ทีม broadcast ไม่มี) และจดวันที่ส่งทีมไว้",
          len(even.calls) == 2
          and "เรื่องที่เกาะติด (Watchlist)" in pushed(even)
          and "เรื่องที่เกาะติด (Watchlist)" not in broadcast(even)
          and "สรุปข่าวเหล็ก" in broadcast(even)
          and team_date == now_bkk().strftime("%Y-%m-%d")
          and priv_state == "ok" and priv_fp == audience.fingerprint(GOOD_ID),
          f"calls={len(even.calls)} team_date={team_date} "
          f"state={priv_state} fp={priv_fp}")

    con = fresh_db()
    storage.set_meta(con, "last_summary_at", "2000-01-01T00:00:00")
    seed_news(con, 3)
    con.close()
    morning = run_digest_as(m, user_id=GOOD_ID, round_hour=7)
    con = storage.connect()
    team_date_morning = storage.get_meta(con, audience.TEAM_DIGEST_META)
    con.close()
    check("G9 สรุปรอบ 07:00 + มี ID -> 1 request (เฉพาะส่วนตัว) ทีมยังไม่ได้รับ",
          len(morning.calls) == 1 and broadcast(morning) == ""
          and "สรุปข่าวเหล็ก" in pushed(morning) and team_date_morning is None,
          f"calls={len(morning.calls)} team_date={team_date_morning}")

    # --- G10: a malformed id must fail LOUDLY, not silently ---------------
    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 3)
    con.close()
    errors = []
    saved_error = audience.log.error
    audience.log.error = lambda *a, **k: errors.append(a[0] if a else "")
    try:
        bad_id = run_realtime_as(m, user_id="Utest123")
        state_bad = None
        os.environ["LINE_USER_ID"] = "Utest123"
        try:
            state_bad = audience.private_user_id()[1]
        finally:
            os.environ.pop("LINE_USER_ID", None)
    finally:
        audience.log.error = saved_error
    check("G10 ID ผิดรูปแบบ -> กลับไป broadcast อย่างเดียว + สถานะ invalid "
          "+ log ERROR (ไม่เงียบหาย)",
          len(bad_id.calls) == 1 and pushed(bad_id) == ""
          and "ผลกระทบต่อบริษัท" not in broadcast(bad_id)
          and state_bad == "invalid" and errors,
          f"calls={len(bad_id.calls)} state={state_bad} errors={len(errors)}")

    # --- G11: dead-man's switch reaches BOTH, and only one gets details ----
    boom = RuntimeError("FATAL: (ENOTFOUND) tenant or user "
                        "postgres.oxthmnbpzkzezrerkdhv not found")
    broken = lambda *a, **k: (_ for _ in ()).throw(boom)  # noqa: E731
    saved_connect = storage.connect
    storage.connect = broken
    raised = None
    try:
        dead = run_digest_as(m, user_id=GOOD_ID, round_hour=18)
    except Exception as exc:  # noqa: BLE001
        raised, dead = exc, None
    finally:
        storage.connect = saved_connect
    pub_dead = broadcast(dead) if dead else ""
    priv_dead = pushed(dead) if dead else ""
    check("G11 DB ล่ม: ทั้งสองปลายทางได้รับแจ้ง · ฉบับสาธารณะไม่มี error ดิบ/"
          "project ref · ไม่มี exception หลุด",
          raised is None and dead is not None and len(dead.calls) == 2
          and "ระบบเฝ้าข่าวขัดข้อง" in pub_dead
          and "ข้อความนี้แปลว่า ระบบพัง ไม่ใช่ ไม่มีข่าว" in pub_dead
          and "ระบบเฝ้าข่าวขัดข้อง" in priv_dead
          and "oxthmnbpzkzezrerkdhv" not in pub_dead
          and "ENOTFOUND" not in pub_dead and "tenant" not in pub_dead
          and "ตรวจ 3 จุดตามลำดับ" not in pub_dead
          and "ตรวจ 3 จุดตามลำดับ" in priv_dead
          and "oxthmnbpzkzezrerkdhv" not in priv_dead,   # masked even in full
          f"raised={raised!r} calls={len(dead.calls) if dead else 0}\n{pub_dead}")

    # --- G12: the guard, layer 3 ------------------------------------------
    with using_profile(SENTINEL_PROFILE):
        blocks, leaks = audience.guard_public_blocks(
            ["บล็อกปกติที่ไม่มีอะไรลับ", f"การ์ดที่มี {SENTINEL_NOTE} ปนเข้ามา"])
        guarded_text = audience.guard_public_text(
            "บรรทัดข่าวปกติ\n" + SENTINEL_WNOTE + "\nบรรทัดท้ายปกติ")
    check("G12 ยามชั้นสาม: บล็อกที่มีประโยคภายในถูกแทนที่ + รายงาน leak "
          "(บล็อกสะอาดไม่โดนแตะ)",
          blocks[0] == "บล็อกปกติที่ไม่มีอะไรลับ"
          and blocks[1] == audience.REDACTED_BLOCK and len(leaks) == 1
          and SENTINEL_WNOTE not in guarded_text
          and "บรรทัดข่าวปกติ" in guarded_text
          and "บรรทัดท้ายปกติ" in guarded_text,
          f"blocks={blocks} leaks={len(leaks)}")

    # --- G13: error masking ----------------------------------------------
    dsn = ("connection failed: postgresql://postgres.abcdefghijklmnop:"
           "S3cretPassw0rd@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres")
    masked = audience.mask_error(dsn)
    tok = audience.mask_error("token AbCdEf0123456789AbCdEf0123456789zz expired")
    mail = audience.mask_error("notify ops.team@example.com failed")
    check("G13 mask_error ปิด DSN / project ref / token / อีเมล",
          "S3cretPassw0rd" not in masked and "pooler.supabase.com" not in masked
          and "abcdefghijklmnop" not in masked
          and "AbCdEf0123456789AbCdEf0123456789zz" not in tok
          and "ops.team@example.com" not in mail,
          f"masked={masked} | tok={tok} | mail={mail}")

    # --- G14: error categories carry no raw text -------------------------
    cat_db = audience.classify_error(RuntimeError(
        "FATAL: (ENOTFOUND) tenant or user postgres.oxthmnbpzkzezrerkdhv not found"))
    cat_timeout = audience.classify_error(TimeoutError("statement timeout after 10s"))
    cat_other = audience.classify_error(ValueError("something odd happened"))
    check("G14 classify_error คืน 'หมวด' ล้วน ไม่มีข้อความ error ดิบติดมา",
          "oxthmnbpzkzezrerkdhv" not in cat_db and "ENOTFOUND" not in cat_db
          and "tenant" not in cat_db and "ฐานข้อมูล" in cat_db
          and cat_timeout != cat_db and "10s" not in cat_timeout
          and cat_other == "ระบบภายในขัดข้อง",
          f"db={cat_db} timeout={cat_timeout} other={cat_other}")

    # --- G15: destination id validation ----------------------------------
    saved_uid = os.environ.pop("LINE_USER_ID", None)
    try:
        unset = audience.private_user_id()
        os.environ["LINE_USER_ID"] = "Utest123"          # เหมือนที่ B3 ใช้
        bad = audience.private_user_id()
        os.environ["LINE_USER_ID"] = GOOD_ID
        good = audience.private_user_id()
        fp = audience.fingerprint(GOOD_ID)
        check("G15 ตรวจรูปแบบ LINE_USER_ID: ว่าง/ผิดรูป/ถูกต้อง + ลายนิ้วมือ 8 ตัว",
              unset == (None, "unset") and bad == (None, "invalid")
              and good == (GOOD_ID, "ok") and len(fp) == 8
              and fp == audience.fingerprint(GOOD_ID) and GOOD_ID not in fp,
              f"unset={unset} bad={bad} good={good[1]} fp={fp}")
    finally:
        os.environ.pop("LINE_USER_ID", None)
        if saved_uid is not None:
            os.environ["LINE_USER_ID"] = saved_uid

    # --- G16: which round the team gets ----------------------------------
    st = dict(SETTINGS, team_digest_rounds=[18])
    con = fresh_db()
    on_round = audience.should_send_team_digest(con, st, 18)
    off_round = audience.should_send_team_digest(con, st, 7)
    catch_up = audience.should_send_team_digest(con, st, 21)   # รอบชดเชย
    no_hour = audience.should_send_team_digest(con, st, None)
    no_con = audience.should_send_team_digest(None, st, 18)
    storage.set_meta(con, audience.TEAM_DIGEST_META,
                     now_bkk().strftime("%Y-%m-%d"))
    already = audience.should_send_team_digest(con, st, 18)
    already_catch = audience.should_send_team_digest(con, st, 22)
    con.close()
    check("G16 รอบของทีม: ตรงรอบ=ส่ง · ก่อนรอบ=ไม่ส่ง · เลยรอบ=ชดเชย · "
          "ส่งแล้ววันนี้=ไม่ส่งซ้ำ",
          on_round and not off_round and catch_up and not no_hour and no_con
          and not already and not already_catch,
          f"on={on_round} off={off_round} catchup={catch_up} "
          f"none={no_hour} nocon={no_con} already={already}/{already_catch}")

    # --- G17: --preview-public is READ-ONLY ------------------------------
    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_news(con, 4)
    con.close()
    con = storage.connect()
    meta_before = con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    alerted_before = n_alerted(con)
    con.close()
    buf = io.StringIO()
    with line_channel() as preview_stub:
        with contextlib.redirect_stdout(buf):
            main.preview_public_cli(m, limit=4)
    con = storage.connect()
    meta_after = con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    alerted_after = n_alerted(con)
    con.close()
    out = buf.getvalue()
    check("G17 --preview-public: ไม่ส่ง LINE · ไม่ mark alerted · ไม่แตะ meta "
          "· รายงานผลสแกน leak",
          len(preview_stub.calls) == 0 and alerted_before == alerted_after == 0
          and meta_before == meta_after
          and "ผลสแกน" in out and "ไม่มี" in out
          and "ผลกระทบต่อบริษัท" not in out.split("ผลสแกน")[0],
          f"calls={len(preview_stub.calls)} alerted={alerted_before}->"
          f"{alerted_after} meta_same={meta_before == meta_after}")


# =========================================================================
# H. the public archive: a site anyone on the internet can read
# =========================================================================

# The archive is built for a PUBLIC GitHub Pages host, so the question these
# checks answer is not "does it look right" but "can a stranger learn anything
# about this company from it". H3 is the headline evidence: a whole site built
# while the sentinel profile is loaded, with every page searched for every
# sentinel sentence.

SETTINGS_ARCHIVE = dict(
    SETTINGS,
    cluster_enabled=True,          # the archive groups same-day duplicates
    archive_enabled=True,
    archive_index_max_kb=900,
    archive_include_level=True,
)

_SITE_N = [0]


def arch_settings(**overrides):
    out = dict(SETTINGS_ARCHIVE)
    out.update(overrides)
    return out


def out_dir():
    """A directory that does NOT exist yet, so 'no file was written' is
    testable rather than assumed."""
    _SITE_N[0] += 1
    return os.path.join(TMP_DIR, "site%d" % _SITE_N[0])


def site_files(outdir):
    """Relative paths of everything actually on disk under outdir."""
    found = []
    for root, _dirs, names in os.walk(outdir):
        for name in names:
            full = os.path.join(root, name)
            found.append(os.path.relpath(full, outdir).replace("\\", "/"))
    return sorted(found)


def read_site(outdir):
    out = {}
    for rel in site_files(outdir):
        with open(os.path.join(outdir, *rel.split("/")), encoding="utf-8") as fh:
            out[rel] = fh.read()
    return out


def html_pages(files):
    return {p: t for p, t in files.items() if p.endswith(".html")}


def seed_archive(con, specs):
    """Rows for the archive checks.

    Unlike seed_real_news, a spec may deliberately omit the publication time
    (`when=None`) - the government listings really do arrive without one, and
    the archive has to fall back to fetched_at and SAY that it did."""
    default = now_bkk().strftime("%Y-%m-%dT%H:%M:%S")
    pairs = []
    for i, spec in enumerate(specs):
        when = spec.get("when", default) or ""
        item = {
            "title": spec["title"],
            "url": spec.get("url", f"https://example.test/arc/{i}"),
            "source": spec.get("outlet", "ทดสอบ"),
            "source_name": spec.get("outlet", "ประชาชาติธุรกิจ"),
            "published": when,
            "published_datetime": when,
            "summary": spec.get("summary", "เนื้อหาย่อสำหรับตรวจคลังข่าวย้อนหลัง."),
        }
        analysis = {
            "topics": spec.get("topics", ["มาตรฐาน มอก."]),
            "critical_hits": ["มอก."],
            "score": spec.get("score", 12),
            "level": spec.get("level", "RED"),
            "impact_notes": spec.get("notes", []),
            "watchlist_hits": spec.get("watch", []),
        }
        pairs.append((item, analysis))
    storage.insert_many(con, pairs)


class no_profile:
    """No operator profile from ANY source.

    using_profile(None) only clears the environment variable; on a developer
    machine config/profile.json still exists and would be picked up, so the
    file path is redirected too. Without this, 'the guard is unarmed' cannot be
    tested at all on the machine where it matters most."""

    def __enter__(self):
        from src import matcher as matcher_mod
        self._mod = matcher_mod
        self._saved_path = matcher_mod.PROFILE_PATH
        self._saved_env = os.environ.get("STEEL_INTEL_PROFILE_JSON")
        matcher_mod.PROFILE_PATH = os.path.join(TMP_DIR, "no-such-profile.json")
        os.environ.pop("STEEL_INTEL_PROFILE_JSON", None)
        audience.reset_cache()
        return self

    def __exit__(self, *exc):
        self._mod.PROFILE_PATH = self._saved_path
        if self._saved_env is not None:
            os.environ["STEEL_INTEL_PROFILE_JSON"] = self._saved_env
        audience.reset_cache()
        return False


def real_profile_sentences():
    """Long strings out of the REAL config/profile.json, as a deny-list.

    Keyword lists are skipped on purpose: profile keywords are words like
    "เตา IF" and "มอก." which appear in real headlines for entirely innocent
    reasons (the same reasoning as src/audience.py). Everything else - notes,
    titles, names, addresses - must never show up in a published page."""
    from src.matcher import PROFILE_PATH
    if not os.path.exists(PROFILE_PATH):
        return None
    with open(PROFILE_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    found = []

    def walk(node, key=""):
        if isinstance(node, str):
            if key != "keywords" and len(node.strip()) >= audience.MIN_SECRET_LEN:
                found.append(node.strip())
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)

    walk(data)
    return found


def section_h():
    print("\n--- H. คลังข่าวย้อนหลัง (archive) ---")
    from src import archive

    # --- H1: the allow-list itself ---------------------------------------
    secret_fields = {"score", "hash", "critical_hits", "impact_notes",
                     "watchlist_hits", "alerted", "story_key"}
    check("H1 ฟิลด์ที่ขึ้นคลัง เป็นสับเซตของฉบับสาธารณะ และไม่มีฟิลด์ลับแม้ตัวเดียว",
          archive.ARCHIVE_FIELDS <= audience.PUBLIC_ROW_FIELDS
          and not (archive.ARCHIVE_FIELDS & secret_fields),
          f"archive={sorted(archive.ARCHIVE_FIELDS)}")

    # --- H2: encode() cannot carry a secret through -----------------------
    con = fresh_db()
    seed_secret_news(con, 3)          # rows whose analysis holds the sentinels
    keys_in_db = [row[0] for row in
                  con.execute("SELECT story_key FROM news").fetchall()]
    rows = archive.rows_for_archive(con)
    con.close()
    doc = archive.encode(rows, SETTINGS_ARCHIVE)
    payload = archive.payload_json(doc)
    # ...and a hand-made row that carries every secret field explicitly, in case
    # a future storage change starts handing extra keys to the archive.
    dirty = dict(rows[0], score=99, impact_notes=[SENTINEL_NOTE],
                 watchlist_hits=[SENTINEL_WATCH], critical_hits=["มอก."],
                 story_key="deadbeefdeadbeef", hash="cafebabe", alerted=1)
    dirty_payload = archive.payload_json(archive.encode([dirty], SETTINGS_ARCHIVE))
    both = payload + dirty_payload
    check("H2 encode: ไม่มีชื่อฟิลด์ลับ ไม่มีค่าลับ (โน้ต/watchlist/story_key) "
          "ในเพย์โหลด",
          not any(t in both for t in archive.FORBIDDEN_TOKENS)
          and not any(s in both for s in SENTINELS)
          and not any(k and k in both for k in keys_in_db)
          and "deadbeefdeadbeef" not in dirty_payload
          and "cafebabe" not in dirty_payload,
          f"rows={len(rows)}")

    # --- H3: THE evidence - a whole site built with the sentinel loaded ----
    outdir3 = out_dir()
    with using_profile(SENTINEL_PROFILE):
        con = fresh_db()
        seed_secret_news(con, 3)
        seed_archive(con, [
            {"title": "สมอ. ทบทวนมาตรฐานเหล็กเส้น รอบใหม่",
             "when": "2026-08-27T10:05:00", "outlet": "ประชาชาติธุรกิจ"},
            {"title": "ศุลกากรจับเหล็กสำแดงเท็จ 200 ตัน",
             "when": "2026-07-02T08:30:00", "outlet": "กรุงเทพธุรกิจ"},
        ])
        stats3 = archive.build_site(con, SETTINGS_ARCHIVE, outdir3,
                                    require_guard=True, min_rows=1)
        con.close()
        files3 = read_site(outdir3)
        pages3 = html_pages(files3)
        sentinel_hits = [(p, s[:14]) for p, t in pages3.items()
                         for s in SENTINELS if s in t]
        guard_hits = [(p, len(audience.find_leaks(t))) for p, t in pages3.items()
                      if audience.find_leaks(t)]
    check("H3 สร้างคลังทั้งไซต์ใต้โปรไฟล์ตราลับ -> ไม่มีประโยคภายในในหน้าใดเลย "
          "(หลักฐานหลัก)",
          bool(pages3) and not sentinel_hits and not guard_hits
          and stats3["rows"] == 5 and stats3["leaks"] == 0,
          f"pages={len(pages3)} sentinel={sentinel_hits} guard={guard_hits} "
          f"stats={ {k: stats3[k] for k in ('rows', 'pages', 'index_rows')} }")

    # --- H4: no raw row was ever dumped -----------------------------------
    token_hits = [(p, t) for p, text in files3.items()
                  for t in archive.FORBIDDEN_TOKENS if t in text]
    check("H4 ทุกหน้าไม่มีคำต้องห้าม (impact_notes/critical_hits/watchlist_hits/"
          "story_key/alerted/score/hash)",
          not token_hits, f"hits={token_hits}")

    # --- H5: the REAL profile, not the sentinel ---------------------------
    deny = real_profile_sentences()
    if deny is None:
        print("[SKIP] H5 ไม่มี config/profile.json ในเครื่องนี้ "
              "(ตรวจไม่ได้ ไม่ใช่ผ่าน)")
    else:
        real_hits = [(p, len(s)) for p, t in pages3.items() for s in deny if s in t]
        check(f"H5 ประโยคจริงจาก config/profile.json ({len(deny)} ประโยค) "
              "ไม่โผล่ในหน้าใดเลย", not real_hits, f"hits={real_hits}")

    # --- H6: an unarmed guard must stop the build, not bless it -----------
    outdir6 = out_dir()
    con = fresh_db()
    seed_archive(con, [{"title": "ข่าวทดสอบยามไม่ติดอาวุธ"}])
    raised6, secrets6 = None, None
    with no_profile():
        secrets6 = len(audience.profile_secrets())
        try:
            archive.build_site(con, SETTINGS_ARCHIVE, outdir6,
                               require_guard=True, min_rows=1)
        except Exception as exc:            # noqa: BLE001 - any refusal is fine
            raised6 = exc
    con.close()
    check("H6 require_guard=True แต่ไม่มีโปรไฟล์ -> ปฏิเสธการสร้าง และไม่เขียนไฟล์",
          raised6 is not None and secrets6 == 0 and site_files(outdir6) == [],
          f"raised={raised6!r} secrets={secrets6} files={site_files(outdir6)}")

    # --- H7: building the archive must change nothing else -----------------
    outdir7 = out_dir()
    con = fresh_db()
    storage.set_meta(con, "baseline_seeded", "1")
    seed_archive(con, [{"title": "ข่าวคลัง A"}, {"title": "ข่าวคลัง B"},
                       {"title": "ข่าวคลัง C"}])
    first_id = db_rows(con)[0][0]
    storage.mark_alerted(con, [first_id])
    meta_before = con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    alerted_before = n_alerted(con)
    con.close()
    con = storage.connect()
    with line_channel() as arch_stub:
        stats7 = archive.build_site(con, SETTINGS_ARCHIVE, outdir7, min_rows=1)
    meta_after = con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    alerted_after = n_alerted(con)
    con.close()
    check("H7 สร้างคลังเต็มรอบ: ไม่ยิง LINE · alerted เท่าเดิม · meta ไม่เปลี่ยน",
          len(arch_stub.calls) == 0 and alerted_before == alerted_after == 1
          and meta_before == meta_after and stats7["rows"] == 3,
          f"calls={len(arch_stub.calls)} alerted={alerted_before}->{alerted_after} "
          f"meta_same={meta_before == meta_after}")

    # --- H8: a headline is not markup, and a link is not code -------------
    outdir8 = out_dir()
    con = fresh_db()
    seed_archive(con, [
        {"title": "</script><script>alert(1)</script>",
         "url": "javascript:alert(1)", "when": "2026-08-20T09:00:00"},
        {"title": "ข่าวปกติคู่กัน", "when": "2026-08-20T08:00:00"},
    ])
    archive.build_site(con, SETTINGS_ARCHIVE, outdir8, min_rows=1)
    con.close()
    page8 = read_site(outdir8)["index.html"]
    tag = '<script id="d" type="application/json">'
    block8 = page8.split(tag, 1)[1].split("</script>", 1)[0]
    try:
        parsed8 = json.loads(block8)
        titles8 = [r[1] for r in parsed8["rows"]]
    except ValueError as exc:
        parsed8, titles8 = None, [repr(exc)]
    check("H8 พาดหัวที่มี </script> และลิงก์ javascript: -> บล็อกข้อมูลไม่ถูกปิดกลางคัน "
          "และไม่มี href=javascript:",
          parsed8 is not None and "</script><script>alert(1)</script>" in titles8
          and 'href="javascript:' not in page8
          and "&lt;/script&gt;" in page8
          and page8.count(tag) == 1,
          f"titles={titles8}")

    # --- H9: an empty archive must never overwrite a good one -------------
    outdir9 = out_dir()
    con = fresh_db()
    raised9 = None
    try:
        archive.build_site(con, SETTINGS_ARCHIVE, outdir9, min_rows=1)
    except Exception as exc:                # noqa: BLE001
        raised9 = exc
    con.close()
    check("H9 ตารางว่าง -> ปฏิเสธการสร้าง และไม่มีไฟล์ถูกเขียน",
          raised9 is not None and site_files(outdir9) == [],
          f"raised={raised9!r} files={site_files(outdir9)}")

    # --- H10: the off switch is silent, not fatal -------------------------
    outdir10 = out_dir()
    con = fresh_db()
    seed_archive(con, [{"title": "ข่าวที่จะไม่ถูกสร้างเป็นคลัง"}])
    raised10, stats10 = None, None
    try:
        stats10 = archive.build_site(con, arch_settings(archive_enabled=False),
                                     outdir10, min_rows=1)
    except Exception as exc:                # noqa: BLE001
        raised10 = exc
    con.close()
    check("H10 archive_enabled=false -> ข้ามเงียบๆ ไม่ raise และไม่มีไฟล์",
          raised10 is None and (stats10 or {}).get("skipped") is True
          and site_files(outdir10) == [],
          f"raised={raised10!r} stats={stats10} files={site_files(outdir10)}")

    # --- H11: the index is a shortcut, never the only copy ----------------
    con = fresh_db()
    seed_archive(con, [
        {"title": f"ข่าวคลังลำดับที่ {i} เรื่องมาตรฐานเหล็กเส้นและการนำเข้า",
         "when": f"2026-0{(i % 3) + 4}-1{i % 9}T0{i % 9}:00:00",
         "url": f"https://example.test/cap/{i}"}
        for i in range(40)])
    small = arch_settings(archive_index_max_kb=4)
    rows11 = archive._group_by_day(archive.rows_for_archive(con), small)
    pages11 = archive.pages(rows11, small)
    con.close()
    all_ids = {r["id"] for r in rows11}
    index_ids = {r["id"] for r in pages11[0]["rows"]}
    union_ids = {r["id"] for p in pages11[1:] for r in p["rows"]}
    check("H11 เพดานหน้าแรกเล็ก -> หน้าแรกไม่ครบ แต่รวมทุกหน้าไตรมาสแล้วครบทุกแถว",
          len(all_ids) == 40 and 0 < len(index_ids) < len(all_ids)
          and union_ids == all_ids,
          f"all={len(all_ids)} index={len(index_ids)} union={len(union_ids)}")

    # --- H12: the quarter boundary ---------------------------------------
    con = fresh_db()
    seed_archive(con, [
        {"title": "ข่าวปลายไตรมาสสอง", "when": "2026-06-30T23:59:00"},
        {"title": "ข่าวต้นไตรมาสสาม", "when": "2026-07-01T00:01:00"},
    ])
    rows12 = archive._group_by_day(archive.rows_for_archive(con),
                                   SETTINGS_ARCHIVE)
    pages12 = archive.pages(rows12, SETTINGS_ARCHIVE)
    con.close()
    paths12 = {p["path"]: [r["title"] for r in p["rows"]] for p in pages12[1:]}
    check("H12 30/06/2026 -> Q2 · 01/07/2026 -> Q3 (ทั้งฟังก์ชันและหน้าไตรมาสจริง)",
          archive.quarter_key("2026-06-30T23:59") == "2026-Q2"
          and archive.quarter_key("2026-07-01T00:01") == "2026-Q3"
          and paths12.get("q/2026-Q2.html") == ["ข่าวปลายไตรมาสสอง"]
          and paths12.get("q/2026-Q3.html") == ["ข่าวต้นไตรมาสสาม"],
          f"paths={paths12}")

    # --- H13: a missing publication time is admitted, not faked -----------
    con = fresh_db()
    seed_archive(con, [{"title": "ข่าวไม่มีวันที่แต่มีเวลาเก็บ", "when": None},
                       {"title": "ข่าวมีวันที่ครบ", "when": "2026-08-11T07:00:00"}])
    rows13 = archive.rows_for_archive(con)
    pages13 = archive.pages(archive._group_by_day(rows13, SETTINGS_ARCHIVE),
                            SETTINGS_ARCHIVE)
    con.close()
    by_title = {r["title"]: r for r in rows13}
    fallback = by_title.get("ข่าวไม่มีวันที่แต่มีเวลาเก็บ", {})
    dated = by_title.get("ข่าวมีวันที่ครบ", {})
    on_a_page = {r["id"] for p in pages13[1:] for r in p["rows"]}

    # A row with NO usable time at all. It cannot be produced through the table:
    # storage.get_since filters `fetched_at > ''`, so a row with an empty
    # fetched_at is invisible to the query in the first place - and a NULL in
    # either column (what a half-finished migration leaves behind) reaches the
    # code as None, which is where a naive [:16] would blow up. Fed straight to
    # the real function through a stand-in cursor.
    def raw_row(**vals):
        base = {col: "" for col in storage.ROW_COLS}
        base.update({"id": 1, "score": 0, "alerted": 0, "topics": "[]",
                     "critical_hits": "[]", "impact_notes": "[]",
                     "watchlist_hits": "[]"})
        base.update(vals)
        return tuple(base[col] for col in storage.ROW_COLS)

    class _StubCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _StubCon:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, _sql, _params=()):
            return _StubCursor(self._rows)

    raised13 = None
    try:
        timeless = archive.rows_for_archive(_StubCon([
            raw_row(id=91, title="ไม่มีเวลาเลยสักอย่าง",
                    published_datetime=None, fetched_at=None),
            raw_row(id=92, title="ว่างเปล่าทั้งสองช่อง",
                    published_datetime="", fetched_at=""),
        ]))
        pages_timeless = archive.pages(
            archive._group_by_day(timeless, SETTINGS_ARCHIVE), SETTINGS_ARCHIVE)
    except Exception as exc:                # noqa: BLE001
        raised13, timeless, pages_timeless = exc, [], []
    undated_ids = {r["id"] for p in pages_timeless[1:]
                   if p["key"] == archive.UNDATED for r in p["rows"]}

    check("H13 ไม่มี published_datetime -> ใช้เวลาที่เก็บ + ติดธง · ไม่มีเวลาเลย -> "
          "ไม่พัง และยังมีหน้าให้อยู่",
          fallback.get("df") == 1 and fallback.get("disp")
          and dated.get("df") == 0 and dated.get("disp") == "2026-08-11T07:00"
          and on_a_page == {r["id"] for r in rows13}
          and raised13 is None and len(timeless) == 2
          and all(r["disp"] == "" and r["df"] == 0 for r in timeless)
          and undated_ids == {91, 92},
          f"raised={raised13!r} fallback={fallback.get('disp')!r} "
          f"timeless={[(r['id'], r['disp'], r['df']) for r in timeless]} "
          f"undated={undated_ids}")

    # --- H14: same story, one group number, and still three rows ----------
    con = fresh_db()
    same = "สมอ. ถอนอายัดเหล็ก ซิน เคอ หยวน 6.6 หมื่นเส้น ชี้ผลสอบได้มาตรฐาน"
    seed_archive(con, [
        {"title": same, "when": "2026-08-25T09:00:00", "outlet": "ประชาชาติธุรกิจ",
         "url": "https://example.test/g/1"},
        {"title": same, "when": "2026-08-25T10:00:00", "outlet": "มติชนออนไลน์",
         "url": "https://example.test/g/2"},
        {"title": same, "when": "2026-08-25T11:00:00", "outlet": "ท็อปนิวส์",
         "url": "https://example.test/g/3"},
        {"title": "ข่าวคนละเรื่องวันเดียวกัน ศุลกากรจับตู้สินค้า",
         "when": "2026-08-25T12:00:00", "outlet": "กรุงเทพธุรกิจ",
         "url": "https://example.test/g/4"},
    ])
    rows14 = archive._group_by_day(archive.rows_for_archive(con), SETTINGS_ARCHIVE)
    con.close()
    groups14 = {r["title"]: r["g"] for r in rows14}
    same_gs = [r["g"] for r in rows14 if r["title"] == same]
    check("H14 พาดหัวเดียวกัน 3 สำนัก วันเดียวกัน -> g เดียวกัน แต่ยังอยู่ครบ 3 แถว "
          "(กฎห้ามซ่อน)",
          len(rows14) == 4 and len(same_gs) == 3 and len(set(same_gs)) == 1
          and groups14["ข่าวคนละเรื่องวันเดียวกัน ศุลกากรจับตู้สินค้า"] != same_gs[0],
          f"gs={same_gs} all={sorted(groups14.values())}")

    # --- H15: the compact tables decode back to the original --------------
    con = fresh_db()
    seed_archive(con, [
        {"title": "ข่าวกูเกิลนิวส์", "outlet": "ฐานเศรษฐกิจ",
         "url": "https://news.google.com/rss/articles/CBMiabc123",
         "topics": ["มาตรฐาน มอก."], "when": "2026-08-24T09:00:00"},
        {"title": "ข่าวลิงก์ธรรมดา", "outlet": "กรุงเทพธุรกิจ",
         "url": "https://www.bangkokbiznews.com/news/1",
         "topics": ["นำเข้า/ส่งออก", "มาตรฐาน มอก."], "when": "2026-08-23T09:00:00"},
        {"title": "ข่าวลิงก์ http", "outlet": "กรมโรงงานอุตสาหกรรม",
         "url": "http://www.diw.go.th/news/2",
         "topics": [], "when": "2026-08-22T09:00:00"},
    ])
    rows15 = archive.rows_for_archive(con)
    con.close()
    doc15 = archive.encode(rows15, SETTINGS_ARCHIVE)
    ok15 = len(rows15) == 3
    for src_row, enc in zip(rows15, doc15["rows"]):
        url = (doc15["pre"][enc[2]] + enc[3]) if enc[2] >= 0 else enc[3]
        ok15 = ok15 and url == src_row["url"]
        ok15 = ok15 and doc15["src"][enc[4]] == src_row["source_name"]
        ok15 = ok15 and [doc15["top"][i] for i in enc[9]] == list(src_row["topics"])
    check("H15 ดัชนี pre/src/top ถอดกลับได้ตรงต้นฉบับทุกแถว (round-trip)", ok15,
          f"pre={doc15['pre']} src={doc15['src']} top={doc15['top']}")

    # --- H16: publishing the news without the reading of it ---------------
    outdir16 = out_dir()
    con = fresh_db()
    seed_archive(con, [{"title": "ข่าวไม่ระบุระดับความสำคัญ", "level": "RED"},
                       {"title": "ข่าวอีกชิ้นไม่ระบุระดับ", "level": "ORANGE"}])
    no_level = arch_settings(archive_include_level=False)
    stats16 = archive.build_site(con, no_level, outdir16, min_rows=1)
    con.close()
    files16 = read_site(outdir16)
    page16 = files16["index.html"]
    doc16 = json.loads(page16.split(tag, 1)[1].split("</script>", 1)[0])
    check("H16 archive_include_level=false -> เพย์โหลดไม่มีระดับความสำคัญ "
          "และหน้ายังสร้างได้ปกติ",
          stats16["rows"] == 2 and doc16["lv"] == 0
          and all(r[8] == "" for r in doc16["rows"])
          and "🔴" not in page16 and "🟠" not in page16,
          f"lv={doc16['lv']} levels={[r[8] for r in doc16['rows']]}")

    # --- H17: not for search engines ---------------------------------------
    robots_meta = [p for p, t in pages3.items()
                   if '<meta name="robots" content="noindex' not in t]
    check("H17 ทุกหน้ามี meta robots noindex + มี robots.txt ที่ Disallow: /",
          not robots_meta and "robots.txt" in files3
          and "Disallow: /" in files3["robots.txt"]
          and ".nojekyll" in files3,
          f"missing_meta={robots_meta} files={sorted(files3)}")

    # --- H18: offline means offline ---------------------------------------
    external = []
    for path, text in pages3.items():
        low = text.lower()
        for marker in ("<script src", "<link ", "<img", "<iframe",
                       "@import", "url(http", "://fonts."):
            if marker in low:
                external.append((path, marker))
    check("H18 ไม่มีไฟล์ภายนอกเลย (ไม่มี script src / link / img / @import / CDN)",
          not external, f"external={external}")

    # --- H19: the digest links to the archive, at no extra push cost ------
    # The archive is worthless if nobody can find it, and a separate "here is
    # the link" message would cost a LINE request every day out of a ~150/month
    # budget. So the link rides inside the digest that is already going out -
    # and while archive_url is empty the message must not change by one byte.

    def seed19(con):
        """Fixed content AND a fixed publication time: the two digests are
        compared character by character, so nothing may drift between runs."""
        stamp = now_bkk().strftime("%Y-%m-%d") + "T09:15:00"
        pairs = []
        for i in range(3):
            pairs.append(({
                "title": f"ข่าวทดสอบลิงก์คลัง {i} สมอ. ทบทวนมาตรฐานเหล็กเส้น",
                "url": f"https://example.test/arc-link/{i}",
                "source": "ทดสอบ",
                "source_name": "ประชาชาติธุรกิจ",
                "published": stamp,
                "published_datetime": stamp,
                "summary": "เนื้อหาย่อคงที่สำหรับเทียบข้อความสองรอบ.",
            }, {
                "topics": ["มาตรฐาน มอก."],
                "critical_hits": ["มอก."],
                "score": 12,
                "level": "RED",
                "impact_notes": [],
                "watchlist_hits": [],
            }))
        storage.insert_many(con, pairs)

    def digest19(**over):
        con = fresh_db()
        seed19(con)
        con.close()
        stub = run_digest_as(make_matcher(**over))
        return broadcast(stub), len(stub.calls)

    plain19, calls19 = digest19()
    linked19, calls19b = digest19(archive_url="https://example.test/steel/")
    link19 = "📚 คลังข่าวย้อนหลัง: https://example.test/steel/"
    stripped19 = linked19.replace("\n" + link19, "")
    check("H19 archive_url ว่าง -> ข้อความเท่าเดิมเป๊ะ · ตั้งค่าแล้ว -> เพิ่มบรรทัด"
          "ลิงก์คลัง 1 บรรทัด และจำนวน request เท่าเดิม",
          link19 not in plain19 and linked19.count(link19) == 1
          and stripped19 == plain19
          and linked19.count("\n") == plain19.count("\n") + 1
          and calls19 == calls19b == 1,
          f"calls={calls19}/{calls19b} เท่ากันหลังถอดบรรทัด="
          f"{stripped19 == plain19}")

    # --- H20: the front page stays inside its budget ----------------------
    outdir20 = out_dir()
    con = fresh_db()
    seed_archive(con, [
        {"title": f"ข่าวทดสอบเพดานขนาดหน้าแรก ลำดับที่ {i} "
                  "เรื่องมาตรการตอบโต้การทุ่มตลาดเหล็กนำเข้า",
         "when": f"2026-05-2{i % 9}T0{i % 9}:30:00",
         "url": f"https://example.test/size/{i}"}
        for i in range(60)])
    capped = arch_settings(archive_index_max_kb=8)
    stats20 = archive.build_site(con, capped, outdir20, min_rows=1)
    con.close()
    limit20 = 8 * 1024 * 1.15
    check("H20 ขนาดข้อมูลหน้าแรกที่สร้างจริง <= เพดาน x 1.15",
          stats20["index_bytes"] <= limit20
          and stats20["index_rows"] < stats20["rows"] == 60,
          f"index={stats20['index_bytes']}B limit={limit20:.0f}B "
          f"rows={stats20['index_rows']}/{stats20['rows']}")

    # --- H21: the news is shipped ONCE, as data ---------------------------
    # The first version of the archive rendered every headline into HTML next to
    # the very same headline inside the embedded JSON: index.html came out at
    # 1.14 MB with 62% of it duplicated text no reader ever saw twice. The page
    # shell (stylesheet + script + fallback) is a FIXED cost, so this is measured
    # on a realistically sized archive - on two rows any shell looks enormous.
    outdir21 = out_dir()
    con = fresh_db()
    marker21 = "ข่าวหลักฐานว่าหน้าเว็บไม่ได้เรนเดอร์รายการล่วงหน้าเอาไว้ซ้ำอีกชุด"
    specs21 = [{"title": marker21, "when": "2026-01-05T08:00:00",
                "url": "https://example.test/once/0",
                "summary": "แถวเก่าที่สุด จึงไม่อยู่ในบล็อกสำรองของหน้าเว็บ"}]
    for i in range(500):
        specs21.append({
            "title": "ข่าวคลังชุดวัดขนาดลำดับที่ %d เรื่องมาตรการตอบโต้การทุ่มตลาด"
                     "เหล็กนำเข้าและการทบทวนมาตรฐานผลิตภัณฑ์เหล็กเส้นเสริมคอนกรีต"
                     % i,
            "when": "2026-%02d-%02dT%02d:%02d:00"
                    % (4 + (i % 6), 1 + (i % 28), i % 24, i % 60),
            "url": "https://example.test/bulk/%d" % i,
            "outlet": ["ประชาชาติธุรกิจ", "กรุงเทพธุรกิจ", "ฐานเศรษฐกิจ"][i % 3],
            "summary": "เนื้อหาย่อสำหรับวัดขนาดไฟล์จริงของหน้าเว็บคลังข่าว "
                       "ชิ้นที่ %d ในชุดทดสอบ" % i,
        })
    seed_archive(con, specs21)
    stats21 = archive.build_site(con, SETTINGS_ARCHIVE, outdir21, min_rows=1)
    con.close()
    page21 = read_site(outdir21)["index.html"]
    body21 = page21.split(tag, 1)[1].split("</script>", 1)[0]
    file21 = len(page21.encode("utf-8"))
    pay21 = len(body21.encode("utf-8"))
    ns21 = page21.count('class="nsitem"')
    check("H21 หน้าแรกไม่มีรายการข่าวที่เรนเดอร์ล่วงหน้า: ขนาดไฟล์ <= เพย์โหลด x 1.4 "
          "และพาดหัวนอกบล็อกสำรองปรากฏในไฟล์ครั้งเดียว",
          stats21["index_rows"] == 501 and file21 <= pay21 * 1.4
          and ns21 <= archive.NOSCRIPT_ROWS and page21.count(marker21) == 1,
          f"file={file21}B payload={pay21}B ratio={file21 / max(1, pay21):.2f} "
          f"fallback={ns21} marker={page21.count(marker21)}")

    # --- H22: the page reads its news from the JSON block, not from markup --
    js22 = archive.asset("app.js")
    check("H22 หน้าเว็บมีบล็อกข้อมูล JSON ก้อนเดียว และสคริปต์อ่านรายการจากบล็อกนั้น "
          "(getElementById + JSON.parse)",
          page21.count(tag) == 1
          and "getElementById" in js22 and "JSON.parse" in js22
          and "getElementById" in page21 and "JSON.parse" in page21,
          f"tags={page21.count(tag)} js={len(js22)}B")

    # --- H23: the front-end sources themselves are clean -------------------
    # Not just the built pages: a class or variable named after an internal
    # column is how a raw row starts leaking in the first place, and the name
    # would be shipped inside every page from then on.
    banned23 = set(archive.FORBIDDEN_TOKENS) | {
        "impact_notes", "critical_hits", "watchlist_hits", "story_key",
        "alerted", "score", "hash"}
    web23, hits23 = {}, []
    for name23 in sorted(os.listdir(archive.WEB_DIR)):
        full23 = os.path.join(archive.WEB_DIR, name23)
        if not os.path.isfile(full23):
            continue
        with open(full23, encoding="utf-8") as fh:
            web23[name23] = fh.read()
        for word23 in sorted(banned23):
            if word23 in web23[name23]:
                hits23.append((name23, word23))
    check("H23 ไฟล์ใน web/ ทุกไฟล์ไม่มีชื่อฟิลด์ภายในเลยแม้ในคอมเมนต์",
          bool(web23) and not hits23,
          f"files={sorted(web23)} hits={hits23}")


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
        section_f()
        section_g()
        section_h()
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
