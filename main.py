# -*- coding: utf-8 -*-
"""steel-intel - steel industry news monitor + executive alerts.

Schedules (per approved spec):
  * realtime check every 15 minutes -> instant Telegram alert on critical news
  * daily summary at 07:00 / 12:00 / 18:00 (morning / noon / evening rounds)

Two audiences (src/audience.py): the private destination gets the full message,
the Official Account broadcast gets the same news without this operator's own
reading of it.

CLI:
  python main.py            run the scheduler (production mode)
  python main.py --once     run one fetch+alert cycle then exit (smoke test)
  python main.py --summary  force a daily summary now then exit
  python main.py --quota    print the LINE push-quota status (sends nothing)
  python main.py --quota-set-month N   backfill this month's request counter
  python main.py --cluster-report      read-only same-story duplicate audit
  python main.py --backfill-story-keys fill story_key for pre-existing rows
  python main.py --audience            who receives what (sends nothing)
  python main.py --verify-recipient    one test push to the private destination
  python main.py --preview-public      show the team's version (sends nothing)
  python main.py --playbook            what to DO about the current top news
                                       (full version, sends nothing; --limit N)
  python main.py --build-archive       build the public back-catalogue site
                                       (read-only; --out DIR --require-guard
                                        --min-rows N)
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from src import (  # noqa: E402
    archive, audience, storage, notifier, summarizer, quota, cluster,
)
from src.matcher import Matcher  # noqa: E402
from src.sources import google_news, rss_feeds, gov_sites  # noqa: E402
from src.sources.base import (  # noqa: E402
    enrich_article, summarise_text, is_junk_title, is_fresh, now_bkk,
)

log = logging.getLogger("steel_intel")

SOURCES_PATH = os.path.join(BASE_DIR, "config", "sources.json")
# Where --build-archive writes the public back-catalogue. Kept relative to this
# file (never a hard-coded drive letter) so it works on the runner too.
ARCHIVE_DIR = os.path.join(BASE_DIR, "site")
ROUND_LABELS = {7: "เช้า 07:00", 12: "เที่ยง 12:00", 18: "เย็น 18:00"}

# Article enrichment (extra HTTP round-trip per item) is gated to high-impact
# levels and capped per cycle, so the remote-DB collect stays well under the
# GitHub Actions / Supabase latency budget (~58s proven baseline).
ENRICH_LEVELS = ("RED", "ORANGE")
ENRICH_MAX_PER_CYCLE = 12


def load_env():
    """Load .env (python-dotenv if available, manual parse as fallback)."""
    env_path = os.path.join(BASE_DIR, ".env")
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return
    except ImportError:
        pass
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())
    except OSError as exc:
        log.warning("cannot read .env: %s", exc)


def setup_logging():
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    fileh = logging.handlers.RotatingFileHandler(
        os.path.join(BASE_DIR, "logs", "steel_intel.log"),
        maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)


def load_sources_cfg():
    with open(SOURCES_PATH, encoding="utf-8") as f:
        return json.load(f)


def collect_cycle(matcher_obj):
    """Fetch all 7 source groups, analyze, store new relevant items.
    Returns (n_fetched, n_new)."""
    cfg = load_sources_cfg()
    trusted = cfg.get("trusted_sources", {})
    items = []
    # Trust gate (anti-fake-news) applies to Google News; gov_pages + direct RSS
    # feeds are trusted by origin.
    items += google_news.fetch_all(cfg.get("google_news", {}), trusted)
    items += rss_feeds.fetch_all(cfg.get("rss_feeds", []))
    items += gov_sites.fetch_all(cfg.get("gov_pages", []))

    # Freshness window (anti-stale): an article older than this is ignored even
    # if it's brand-new to the DB. Timezone-aware in Asia/Bangkok (see is_fresh).
    lookback_hours = matcher_obj.settings.get("lookback_hours", 24)
    keep_if_unknown = not matcher_obj.settings.get("drop_if_no_date", False)

    con = storage.connect()
    try:
        pairs = []
        enriched = 0
        dropped_stale = 0
        for item in items:
            if not item.get("title") or is_junk_title(item["title"]):
                continue  # drop nav/page-title junk before scoring
            analysis = matcher_obj.analyze(item)
            if not analysis["is_relevant"]:
                continue  # keep news.db focused on steel-relevant items
            # Enrich high-impact items: fetch the article page for a precise
            # published_datetime and a clean lead-paragraph summary. Capped per
            # cycle to protect the remote-DB time budget.
            # Google News links are redirect URLs we can't follow to the real
            # article, so enrichment only helps direct (gov/RSS) URLs.
            url = item.get("url", "")
            is_direct = url and "news.google.com" not in url
            if (analysis["level"] in ENRICH_LEVELS
                    and is_direct
                    and enriched < ENRICH_MAX_PER_CYCLE
                    and (not item.get("published_datetime") or not item.get("summary"))):
                dt, summ = enrich_article(item.get("url", ""))
                if dt and not item.get("published_datetime"):
                    item["published_datetime"] = dt
                if summ:
                    item["summary"] = summ
                enriched += 1
            # Lookback guard: drop anything published outside the window. Runs
            # AFTER enrichment so an undated high-impact item gets its real date
            # first. Empty/unparseable dates are governed by keep_if_unknown
            # (never silently treated as "now").
            if not is_fresh(item.get("published_datetime", ""),
                            lookback_hours=lookback_hours,
                            keep_if_unknown=keep_if_unknown):
                dropped_stale += 1
                continue
            # Always store a tidied summary (enriched lead or trimmed feed text).
            item["summary"] = summarise_text(item.get("summary", ""))
            pairs.append((item, analysis))
        # Bulk insert (few round-trips) — critical for the remote-DB deployment;
        # a re-fetch is mostly duplicates and per-row inserts would time out.
        n_new = storage.insert_many(con, pairs)
    finally:
        con.close()
    log.info("collect cycle: fetched=%d new_relevant=%d enriched=%d stale_dropped=%d",
             len(items), n_new, enriched, dropped_stale)
    return len(items), n_new


def build_alert_blocks(detailed, overflow, n_stories, aud):
    """The blocks of one realtime push, rendered for ONE audience.

    Index-compatible across audiences BY CONSTRUCTION: same header, same cards
    in the same order, same optional tail. That is what lets `block_rows` be
    built once and still line up with every destination's plan, which in turn is
    what makes "mark alerted only what every destination carried" correct.

    For the public audience the row is projected FIRST (audience.public_row), so
    the renderer never even sees the internal fields.
    """
    blocks = [summarizer.build_alert_batch_header(n_stories, len(detailed))]
    for i, story in enumerate(detailed, 1):
        row = story["row"]
        if aud == "public":
            row = audience.public_row(row)
        blocks.append(summarizer.build_critical_alert(
            row, row, index=i, total=len(detailed), audience=aud))
    if overflow:
        rows = [s["row"] for s in overflow]
        if aud == "public":
            rows = audience.public_rows(rows)
        blocks.append(summarizer.build_extra_headlines(rows))
    return blocks


def realtime_job(matcher_obj):
    """15-minute loop: collect then alert every unalerted critical item."""
    log.info("=== realtime job start ===")
    try:
        collect_cycle(matcher_obj)
    except Exception as exc:
        log.error("collect cycle failed: %s", exc)

    con = storage.connect()
    try:
        try:
            storage.ensure_story_keys(con)
        except Exception as exc:  # never let a backfill cost an alert
            log.warning("story_key backfill skipped: %s", exc)
        pending = storage.get_unalerted_critical(
            con, matcher_obj.settings.get("priority_alert_keywords", []))
        if not pending:
            log.info("no critical news pending alert")
            return

        # Baseline seeding: on the very first run the DB is empty, so every
        # existing headline looks "new". Mark them all as already-known instead
        # of firing hundreds of alerts at once. Only genuinely new items in
        # later cycles will trigger alerts. The first daily summary still shows
        # today's top items in grouped form, so nothing important is lost.
        if storage.get_meta(con, "baseline_seeded") != "1":
            storage.mark_alerted(con, [r["id"] for r in pending])
            storage.set_meta(con, "baseline_seeded", "1")
            log.info("baseline seeded: %d existing items marked known (no alert)",
                     len(pending))
            return

        # Age gate. get_unalerted_critical selects on alerted=0 with no notion of
        # time, so ANY widening of the alert rule instantly makes the whole
        # never-alerted backlog eligible at once. That is not hypothetical: on
        # 2026-08-27 admitting priority YELLOW items fired 109 alerts about news
        # from June-August. An instant alert is for BREAKING news, so anything
        # past the lookback window is aged out here - marked known, never sent.
        lookback_hours = matcher_obj.settings.get("lookback_hours", 24)
        keep_if_unknown = not matcher_obj.settings.get("drop_if_no_date", False)
        fresh, stale_ids = [], []
        for row in pending:
            if is_fresh(row.get("published_datetime", ""),
                        lookback_hours=lookback_hours,
                        keep_if_unknown=keep_if_unknown):
                fresh.append(row)
            else:
                stale_ids.append(row["id"])
        if stale_ids:
            storage.mark_alerted(con, stale_ids)
            log.info("aged out %d critical items (older than %dh, never alerted)",
                     len(stale_ids), lookback_hours)
        pending = fresh
        if not pending:
            log.info("no fresh critical news pending alert")
            return

        # Story collapse (DISPLAY ONLY - nothing is deleted, no hash changes).
        # One event carried by three outlets is three rows with three different
        # hashes, so this push used to spend three cards on one story. Rows
        # arrive ordered score DESC, so each group is led by its highest-scoring
        # telling; every other member is still named on the card (no-hiding
        # rule), and EVERY member id is marked alerted below.
        stories = cluster.group_stories(pending, matcher_obj.settings,
                                        label="realtime")

        # Push budget. LINE bills per REQUEST x recipients, and this loop used
        # to fire one request per news item - 8 alerts = 8 pushes, which on
        # 2026-08-27 ate a fifth of the monthly quota in a single cycle. Now the
        # whole batch is packed into ONE request (max_requests=1), and even that
        # one request is only spent while the daily/monthly budget allows.
        cap = matcher_obj.settings.get("alert_max_per_cycle", 8)
        override_levels = matcher_obj.settings.get("alert_override_levels", ["RED"])
        budget_left = quota.realtime_budget_left(con, matcher_obj.settings)
        is_override = False
        if budget_left <= 0:
            # Out of budget: only a genuinely top-level story may still buy an
            # emergency push, and only while the override reserve holds.
            top = [s for s in stories if s["row"].get("level") in override_levels]
            if not top or quota.override_left(con, matcher_obj.settings) <= 0:
                # Deliberately NOT marking anything alerted: these items stay
                # pending and are re-offered next cycle / in the daily digest.
                log.info("push budget spent: deferring %d critical items", len(pending))
                return
            stories = top
            is_override = True
            log.warning("push budget spent; spending override push on %d %s stories",
                        len(top), "/".join(override_levels))

        # Build the row ids each block accounts for, so only what actually went
        # out gets marked alerted. Built ONCE: build_alert_blocks guarantees the
        # same block layout for every audience.
        detailed, overflow = stories[:cap], stories[cap:]
        block_rows = [[]]
        for story in detailed:
            # ids, not [row["id"]]: a card speaks for its whole group, so every
            # row it covers must be marked alerted or the merged-away rows would
            # come back as "new" and be alerted again next cycle.
            block_rows.append(story["ids"])
        if overflow:
            block_rows.append([i for s in overflow for i in s["ids"]])

        targets = audience.realtime_targets(matcher_obj.settings, con)
        if not targets:  # cannot happen (team is the fallback) - but never guess
            log.error("no alert destination resolved: nothing was sent")
            return

        ok_all, used_total, covered = True, 0, None
        for target in targets:
            blocks = build_alert_blocks(detailed, overflow, len(stories),
                                        target["audience"])
            if target["audience"] == "public":
                blocks, leaks = audience.guard_public_blocks(blocks)
                if leaks:
                    log.error("realtime: %d block(s) redacted before broadcast",
                              len(leaks))
            ok, used, cov = notifier.send_blocks(blocks, max_requests=1,
                                                 to=target["to"])
            ok_all = ok and ok_all
            used_total += used
            # INTERSECTION, not union: a row is done only once EVERY destination
            # has carried it. Marking on the union would let a block that fit on
            # one channel but not another vanish from the channel that dropped
            # it, permanently.
            covered = cov if covered is None else (covered & cov)
            log.info("realtime -> %s (%s): %d request(s), %d block(s) delivered",
                     target["key"], target["audience"], used, len(cov))
        covered = covered or set()

        # mark_alerted follows `covered`, NEVER `ok`: on dry-run (no credentials)
        # ok is always False, and gating on it would re-send the same news every
        # cycle forever. `covered` says what was actually put on the wire.
        sent_ids = [i for idx in sorted(covered) for i in block_rows[idx]]
        storage.mark_alerted(con, sent_ids)
        # The unit stays the REQUEST, summed over destinations - LINE bills each
        # destination its own request.
        used = used_total
        quota.record(con, used, kind="realtime", override=is_override)
        if not ok_all and used and notifier.active_channel() == "line":
            log.warning("LINE reported a delivery failure for this alert batch")
        sent_set = set(sent_ids)
        n_stories = sum(1 for s in stories
                        if s["ids"] and all(i in sent_set for i in s["ids"]))
        log.info("alerted %d rows as %d stories in %d LINE request(s);"
                 " deferred %d rows",
                 len(sent_ids), n_stories, used, len(pending) - len(sent_ids))
    finally:
        con.close()


def _digest_private(matcher_obj, targets, items, n_rows, round_label, health):
    """Send the FULL digest to the private destination(s).

    Returns (used_requests, ok, state) where state is what the public copy uses
    to decide whether to add its "could not reach the operator" footnote.
    """
    used_total, any_ok = 0, False
    state = audience.private_user_id()[1]      # "ok" | "unset" | "invalid"
    for target in targets:
        try:
            msg = summarizer.build_daily_summary(
                items, matcher_obj.cfg["watchlist"], round_label,
                health=health, n_rows=n_rows,
            )
            ok, used = notifier.send_counted(msg, to=target["to"])
            used_total += used
            any_ok = any_ok or ok
            if ok and used:
                state = "ok"
            elif notifier.active_channel() == "dry-run":
                state = "dry-run"        # no credentials locally: not a failure
            else:
                state = "failed"
            if used > 1:
                log.warning("digest cost %d LINE requests - consider lowering "
                            "LEVEL_SHOW_CAP['RED']", used)
        except Exception as exc:  # noqa: BLE001 - one destination must not
            log.error("digest to %s failed: %s", target["key"], exc)  # take out
            state = "failed"                                          # the other
    return used_total, any_ok, state


def _digest_public(matcher_obj, targets, items, n_rows, round_label, health,
                   private_state):
    """Broadcast the PUBLIC digest. Returns (used_requests, ok).

    The rows are projected before rendering and the finished text is scanned
    once more (guard_public_text) - layers 1 and 3 of src/audience.py.
    """
    used_total, any_ok = 0, False
    if private_state in ("failed", "invalid"):
        # Say that the operator did NOT get their copy, without saying anything
        # about the destination or the contents. Re-broadcasting the full digest
        # instead would turn a delivery failure into a disclosure.
        health = health + "\n⚠️ ส่งรายงานฉบับเต็มถึงผู้ดูแลไม่สำเร็จ — โปรดตรวจการตั้งค่าปลายทาง"
    for target in targets:
        try:
            msg = summarizer.build_daily_summary(
                audience.public_rows(items), matcher_obj.cfg["watchlist"],
                round_label, health=health, n_rows=n_rows, audience="public",
            )
            msg = audience.guard_public_text(msg)
            ok, used = notifier.send_counted(msg, to=target["to"])
            used_total += used
            any_ok = any_ok or ok
            if used > 1:
                log.warning("public digest cost %d LINE requests", used)
        except Exception as exc:  # noqa: BLE001
            log.error("digest to %s failed: %s", target["key"], exc)
    return used_total, any_ok


def _send_system_alert(matcher_obj, round_hour, round_label, exc, con):
    """Dead-man's switch delivery: one message per destination, each isolated.

    Returns the number of requests spent. If the audience machinery itself is
    broken, a public fallback still goes out - the whole point of this switch is
    that SOMETHING arrives.
    """
    try:
        targets = audience.digest_targets(matcher_obj.settings, con, round_hour)
    except Exception as texc:  # noqa: BLE001
        log.error("cannot resolve destinations for the system alert: %s", texc)
        targets = [{"key": "team", "to": notifier.BROADCAST, "audience": "public"}]

    used_total = 0
    for target in targets:
        try:
            if target["audience"] == "public":
                msg = audience.guard_public_text(summarizer.build_system_alert(
                    round_label, exc, audience="public"))
            else:
                msg = summarizer.build_system_alert(round_label, exc)
        except Exception as mexc:  # noqa: BLE001
            log.error("cannot build the system alert for %s: %s", target["key"], mexc)
            msg = summarizer.SYSTEM_ALERT_PUBLIC_FALLBACK
        try:
            _ok, used = notifier.send_counted(msg, to=target["to"])
            used_total += used
        except Exception as sexc:  # noqa: BLE001
            log.error("cannot deliver the system alert to %s: %s", target["key"], sexc)
    return used_total


def daily_summary_job(matcher_obj, round_label, round_hour=None):
    """07:00/12:00/18:00 rounds: summarize items stored since last round.

    `round_hour` (the Bangkok hour of this round) decides whether the team also
    gets a copy. It defaults to None - "not a scheduled round" - so any caller
    that still passes two arguments keeps the private-only behaviour.

    This job doubles as the DEAD-MAN'S SWITCH. If collecting or the database
    fails, it says so on LINE instead of going quiet: from the reader's side
    silence is indistinguishable from "no news today", and that is precisely
    how a 16-day outage in Aug 2026 went unnoticed. Only these rounds report
    failures (<=3 pushes/day); the realtime loop stays silent because it runs
    ~36x/day and would exhaust the LINE quota within a week of any outage.
    """
    log.info("=== daily summary job (%s) ===", round_label)
    stats, collect_error = None, None
    try:
        stats = collect_cycle(matcher_obj)  # fresh data right before summarizing
    except Exception as exc:
        collect_error = exc
        log.error("collect before summary failed: %s", exc)

    con = None
    try:
        con = storage.connect()
        try:
            storage.ensure_story_keys(con)
        except Exception as exc:  # never let a backfill trip the dead-man switch
            log.warning("story_key backfill skipped: %s", exc)
        fallback = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        since = storage.get_meta(con, "last_summary_at", fallback)
        items = storage.get_since(con, since)
        # Collapse same-story rows for the digest too, keeping the raw row count
        # so the header can say "N เรื่อง (จาก M ชิ้น)" instead of looking like
        # the watcher simply found fewer articles.
        n_rows = len(items)
        items = [s["row"] for s in
                 cluster.group_stories(items, matcher_obj.settings, label="digest")]
        if collect_error:
            # Masked even on the private channel: an exception can carry the
            # database DSN, and a digest is forwarded far more casually than a
            # log file.
            health = ("เก็บข่าวรอบนี้ล้มเหลว: "
                      f"{audience.mask_error(str(collect_error))[:120]}")
            health_public = "เก็บข่าวรอบนี้ล้มเหลว — ผู้ดูแลระบบได้รับรายละเอียดแล้ว"
        else:
            health = f"ระบบปกติ · รอบนี้ตรวจข่าว {stats[0]:,} ชิ้น"
            health_public = health   # a healthy round reads the same for everyone
        # Rides along inside the digest that is already being sent - zero extra
        # LINE requests. Empty setting = the message stays exactly as it was.
        archive_url = (matcher_obj.settings.get("archive_url") or "").strip()
        if archive_url:
            line = "📚 คลังข่าวย้อนหลัง: " + archive_url
            health = health + "\n" + line
            health_public = health_public + "\n" + line
        # Quota warning rides ALONG WITH this digest (never as its own push -
        # that would burn the very quota it is warning about). The LINE GET
        # endpoints are authoritative and cost nothing.
        # Isolated: this is a nice-to-have, and a hiccup reading it must never
        # make a perfectly healthy digest trip the dead-man's switch.
        warn = None
        try:
            line_limit, line_used = notifier.line_quota_status()
            warn = quota.pending_month_warning(
                con, matcher_obj.settings, line_used=line_used, line_limit=line_limit)
            if warn:
                notice = summarizer.build_quota_warning(warn)
                health = health + "\n" + notice
                health_public = health_public + "\n" + notice
        except Exception as exc:
            log.warning("quota warning check skipped: %s", exc)

        targets = audience.digest_targets(matcher_obj.settings, con, round_hour)
        private = [t for t in targets if t["audience"] == "full"]
        public = [t for t in targets if t["audience"] == "public"]
        # Private first: whether it got through decides what the public copy says.
        used_private, ok_private, private_state = _digest_private(
            matcher_obj, private, items, n_rows, round_label, health)
        used_team, ok_team = _digest_public(
            matcher_obj, public, items, n_rows, round_label, health_public,
            private_state)
        used = used_private + used_team
        quota.record(con, used, kind="summary")
        if warn and (ok_private or ok_team):
            quota.mark_month_warned(con)
        # Tied to used_team, NOT to ok: on dry-run ok is always False, and
        # gating on it would re-broadcast to the team on every later round of
        # the same day (same reason mark_alerted follows `covered`).
        if used_team > 0:
            try:
                storage.set_meta(con, audience.TEAM_DIGEST_META,
                                 now_bkk().strftime("%Y-%m-%d"))
            except Exception as exc:  # noqa: BLE001 - bookkeeping only
                log.warning("cannot record the team digest date: %s", exc)
        try:
            storage.set_meta(con, audience.PRIVATE_STATE_META, private_state)
            storage.set_meta(con, audience.PRIVATE_FP_META,
                             audience.fingerprint(audience.private_user_id()[0]))
        except Exception as exc:  # noqa: BLE001 - bookkeeping only
            log.warning("cannot record the private destination state: %s", exc)
        storage.set_meta(
            con, "last_summary_at", datetime.now().isoformat(timespec="seconds")
        )
        log.info("daily summary sent (%d items, %d LINE request(s): "
                 "private=%d team=%d, private state=%s)",
                 len(items), used, used_private, used_team, private_state)
    except Exception as exc:
        # The watcher itself is down (DB paused/unreachable, schema gone...).
        # Report it rather than letting the outage look like a quiet news day.
        log.error("daily summary failed: %s", exc)
        used = _send_system_alert(matcher_obj, round_hour, round_label, exc, con)
        # Book it only if we still have a connection - and never let bookkeeping
        # throw over the top of the dead-man's switch.
        if con is not None:
            try:
                quota.record(con, used, kind="summary")
            except Exception as qexc:  # pragma: no cover - record() never raises
                log.warning("cannot record quota for system alert: %s", qexc)
    finally:
        if con is not None:
            con.close()


def quota_cli(matcher_obj, set_month=None):
    """--quota / --quota-set-month: read-only report (optionally after a manual
    backfill). Reading the LINE quota uses GET endpoints and sends nothing."""
    con = storage.connect()
    try:
        if set_month is not None:
            quota.set_month_requests(con, set_month)
            print(f"ตั้งตัวนับ request ของเดือน {quota.month_key()[-7:]} = {set_month}")
        line_limit, line_used = notifier.line_quota_status()
        if line_used is None:
            print("(อ่านโควต้าจาก LINE API ไม่ได้ — ใช้ตัวนับในฐานข้อมูลแทน)")
        print(quota.report(con, matcher_obj.settings,
                           line_used=line_used, line_limit=line_limit))
    finally:
        con.close()


def audience_cli(matcher_obj):
    """--audience: who receives what, and at which level of detail.

    READ-ONLY: sends nothing, writes nothing. Prints a FINGERPRINT of the
    configured id, never the id itself - this output lands in terminals, logs
    and screenshots.
    """
    con = None
    try:
        con = storage.connect()
    except Exception as exc:  # noqa: BLE001 - the report works without a DB
        log.warning("cannot open the database for the audience report: %s", exc)
    try:
        print(audience.mode_report(matcher_obj.settings, con))
    finally:
        if con is not None:
            con.close()


def verify_recipient_cli(matcher_obj):
    """--verify-recipient: push ONE test message to the private destination.

    A wrong id fails silently forever otherwise: LINE accepts the request and
    nobody ever receives anything. This is the only way to find that out on
    purpose rather than by noticing months of quiet.
    """
    uid, state = audience.private_user_id()
    if not uid:
        print("ยังไม่มีช่องส่วนตัวที่ใช้งานได้")
        print("  สาเหตุ: " + ("LINE_USER_ID ผิดรูปแบบ "
                              "(ต้องเป็น U/C/R ตามด้วยเลขฐาน 16 อีก 32 ตัว)"
                              if state == "invalid" else "ยังไม่ได้ตั้ง LINE_USER_ID"))
        print("  ตอนนี้ทุกข้อความออกทาง broadcast เป็นฉบับสาธารณะเท่านั้น")
        return
    msg = "\n".join([
        "✅ ทดสอบปลายทางส่วนตัว (steel-intel)",
        f"🗓 {summarizer.thai_date()}",
        "",
        "ถ้าอ่านข้อความนี้ได้ แปลว่าช่องส่วนตัวใช้งานได้จริง",
        "รายงานฉบับเต็ม (ผลกระทบต่อบริษัท / คะแนน / watchlist) จะส่งมาทางนี้",
        f"ลายนิ้วมือปลายทาง: {audience.fingerprint(uid)}",
    ])
    ok, used = notifier.send_counted(msg, to=uid)
    channel = notifier.active_channel()
    result = "ok" if (ok and used) else ("dry-run" if channel == "dry-run" else "failed")
    try:
        con = storage.connect()
        try:
            storage.set_meta(con, audience.PRIVATE_STATE_META, result)
            storage.set_meta(con, audience.PRIVATE_FP_META, audience.fingerprint(uid))
            quota.record(con, used, kind="other")
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - the send already happened
        log.warning("cannot record the verification result: %s", exc)
    print(f"ช่องทาง: {channel} · ผล: {result} · ใช้ไป {used} request")
    print(f"ลายนิ้วมือปลายทาง: {audience.fingerprint(uid)} (ไม่แสดง ID จริง)")
    if result == "failed":
        print("⚠️ ส่งไม่สำเร็จ — ตรวจว่า ID นี้แอด OA ไว้จริง และ token ยังใช้ได้")


# Text that must NEVER appear in a public message. Used ONLY as a self-check in
# the preview below - never to edit an outgoing message (see src/audience.py:
# the news itself may legitimately contain any word).
PUBLIC_FORBIDDEN_MARKERS = (
    "ผลกระทบต่อบริษัท", "คะแนน", "คำสำคัญที่พบ", "⏳ เกาะติด",
    "เรื่องที่เกาะติด (Watchlist)", "บทวิเคราะห์ AI",
    # The playbook headings (src/playbook.py). An action names this operator's
    # licences and obligations, so seeing either of these in a public message
    # means the audience split failed upstream.
    summarizer.ACTION_HEAD, "🎯 ทำต่อ:",
)


def preview_public_cli(matcher_obj, limit=None):
    """--preview-public: exactly what the team broadcast would look like.

    READ-ONLY: sends nothing, marks nothing alerted, writes no meta key and
    records no quota. Run it after touching anything in summarizer/audience.
    """
    con = storage.connect()
    try:
        rows = storage.get_since(con, "")          # every row, ORDER BY score
        sample = rows[:(limit or 5)]
        stories = cluster.group_stories(sample, matcher_obj.settings,
                                        label="preview")
        cap = matcher_obj.settings.get("alert_max_per_cycle", 8)
        detailed, overflow = stories[:cap], stories[cap:]
        blocks = build_alert_blocks(detailed, overflow, len(stories), "public")
        guarded, block_leaks = audience.guard_public_blocks(blocks)

        digest = summarizer.build_daily_summary(
            audience.public_rows([s["row"] for s in stories]),
            matcher_obj.cfg["watchlist"], "ตัวอย่าง",
            health="ตัวอย่าง — ไม่ได้ส่งจริง", n_rows=len(sample),
            audience="public",
        )
        digest_leaks = audience.find_leaks(digest)
        digest = audience.guard_public_text(digest)

        print("ตัวอย่างข้อความฉบับสาธารณะ (ที่ทีมงานจะได้รับ) — "
              "อ่านอย่างเดียว ไม่ส่ง LINE ไม่แก้ฐานข้อมูล")
        print("=" * 66)
        print(f"แถวที่ใช้ทำตัวอย่าง : {len(sample):,} แถว -> {len(stories):,} เรื่อง")
        print(f"ประโยคภายในที่ยามเฝ้าอยู่ : {len(audience.profile_secrets())} ประโยค")
        print("")
        print("--- แจ้งเตือนด่วน (ฉบับสาธารณะ) ---")
        for block in guarded:
            print(block)
            print("")
        print("--- สรุปรายวัน (ฉบับสาธารณะ) ---")
        print(digest)

        whole = "\n".join(guarded) + "\n" + digest
        hits = [m for m in PUBLIC_FORBIDDEN_MARKERS if m in whole]
        leaks = block_leaks + digest_leaks
        print("")
        print("=" * 66)
        print("ผลสแกน")
        print(f"  ประโยคภายในที่หลุดออกมา : {len(leaks)}"
              + ("  ✅ ไม่มี" if not leaks else "  ❌ มี (ยามตัดออกให้แล้ว)"))
        print(f"  หัวข้อภายในที่ไม่ควรมี   : "
              + ("✅ ไม่พบ" if not hits else "❌ พบ -> " + ", ".join(hits)))
        if leaks or hits:
            print("  ⚠️ ต้องแก้ก่อนใช้งานจริง — ดู src/audience.py")
    finally:
        con.close()


def playbook_cli(matcher_obj, limit=None):
    """--playbook: the FULL version - what to do about the current top news.

    READ-ONLY: sends nothing, marks nothing alerted, writes no meta key and
    records no quota. This is the mirror image of --preview-public: that one
    proves the team sees no internal reading, this one shows the reading and
    the instructions that go with it.

    It prints operator-internal text to the terminal on purpose, which is why
    it opens with a warning: the actions name which licences this plant runs
    under, and a screenshot of this output is a leak.
    """
    from src import playbook

    con = storage.connect()
    try:
        rows = storage.get_since(con, "")          # every row, ORDER BY score
        sample = rows[:(limit or 8)]
        pb = playbook.stats()
        _uid, state = audience.private_user_id()

        print("⚠️ ฉบับเต็ม (มีข้อมูลภายใน) — อย่าแคปหน้าจอนี้ส่งต่อ")
        print("=" * 66)
        print(f"  คู่มือสิ่งที่ต้องทำ       : {pb['with_action']}/{pb['groups']} "
              f"กลุ่มความเสี่ยงมี action (โปรไฟล์: {pb['source']})")
        print(f"  ช่องส่วนตัว (ฉบับเต็ม)   : "
              + {"ok": "ตั้งค่าแล้ว", "unset": "ยังไม่ได้ตั้ง LINE_USER_ID",
                 "invalid": "⚠️ LINE_USER_ID ผิดรูปแบบ"}[state])
        print(f"  แถวที่นำมาแสดง            : {len(sample):,} จาก {len(rows):,} แถว")
        if state != "ok":
            print("ℹ️ ยังไม่ได้ตั้ง LINE_USER_ID → ข้อความเหล่านี้ถูกสร้างแล้ว"
                  "แต่ยังไม่ถูกส่งไปไหน (ห้ามออก broadcast)")
        print("")

        if not sample:
            print("(ยังไม่มีข่าวในฐานข้อมูล)")
            return
        for n, row in enumerate(sample, 1):
            notes = row.get("impact_notes") or []
            acts = playbook.actions_for(notes)
            print(f"{n}. {(row.get('title') or '').strip()}")
            print(f"   {row.get('level', '-')} · คะแนน {row.get('score', 0)}"
                  f" · {row.get('source_name') or row.get('source') or '-'}")
            for note in notes:
                print(f"   ⚠️ {note}")
            if acts:
                for k, act in enumerate(acts, 1):
                    print(f"   🎯 {k}. {act}")
            else:
                print("   — (ไม่มีคู่มือ)")
            print("")
    finally:
        con.close()


def cluster_report_cli(matcher_obj, limit=None):
    """--cluster-report: READ-ONLY same-story audit.

    Sends nothing, marks nothing alerted, writes no meta counter. Run it before
    touching any cluster_* threshold: it prints EVERY group the current settings
    would merge together with all member headlines and the gate that matched, so
    a bad threshold is caught here instead of by a reader who never finds out a
    story went missing."""
    con = storage.connect()
    try:
        rows = storage.get_since(con, "")     # every row, ORDER BY score, id
        total = len(rows)
        keys = [cluster.story_key(r.get("title") or "") for r in rows]
        # An empty key means "no comparable headline"; each such row counts as
        # its own story rather than collapsing them all together.
        distinct = len({k for k in keys if k}) + sum(1 for k in keys if not k)
        dup = total - distinct
        pct = (dup / total * 100) if total else 0.0

        print("รายงานข่าวซ้ำ (cluster report) — อ่านอย่างเดียว "
              "ไม่ส่ง LINE ไม่แก้สถานะแจ้งเตือน")
        print("=" * 66)
        print("ฐานข้อมูลทั้งหมด")
        print(f"  แถวทั้งหมด               : {total:,}")
        print(f"  เรื่องไม่ซ้ำ (story_key)   : {distinct:,}")
        print(f"  แถวที่เป็นเรื่องซ้ำ        : {dup:,} ({pct:.1f}%)")

        # Same population get_unalerted_critical draws from, minus the alerted
        # filter (on a live DB almost everything is already alerted, and an audit
        # that saw nothing would be useless).
        priority = [k.lower() for k in
                    matcher_obj.settings.get("priority_alert_keywords", [])]
        eligible = []
        for row in rows:
            if not row.get("critical_hits"):
                continue
            if row.get("level") in ("RED", "ORANGE"):
                eligible.append(row)
            elif row.get("level") == "YELLOW" and any(
                    str(h).lower() in priority for h in row["critical_hits"]):
                eligible.append(row)
        if limit:
            eligible = eligible[:limit]

        settings = dict(matcher_obj.settings)
        settings["cluster_enabled"] = True
        # The audit must always actually run, even past the production guard.
        settings["cluster_max_rows"] = max(len(eligible), 1)
        cfg = cluster.build_cfg(settings)
        started = time.time()
        stories = cluster.group_stories(eligible, settings, label="report")
        elapsed = time.time() - started
        collapsed = len(eligible) - len(stories)
        cpct = (collapsed / len(eligible) * 100) if eligible else 0.0

        print("")
        print("จำลองการยุบบนแถวที่เข้าเกณฑ์ยิงเตือน (alert-eligible)")
        print(f"  แถวเข้าเกณฑ์              : {len(eligible):,}")
        print(f"  ยุบแล้วเหลือการ์ด          : {len(stories):,}")
        print(f"  ยุบไปได้                 : {collapsed:,} แถว ({cpct:.1f}%)")
        print(f"  เวลาที่ใช้                : {elapsed:.2f} วินาที")
        print(f"  เกณฑ์ที่ใช้               : jaccard>={cfg['cluster_jaccard_min']}"
              f" ratio>={cfg['cluster_ratio_min']}"
              f" len>={cfg['cluster_len_ratio_min']}"
              f" window={cfg['cluster_window_hours']}h"
              f" ngram={cfg['cluster_ngram']}")

        merged = [s for s in stories if len(s["members"]) > 1]
        print("")
        print(f"กลุ่มที่ถูกยุบ {len(merged)} กลุ่ม "
              "(กางครบทุกกลุ่ม พาดหัวครบทุกใบ — ตรวจว่ายุบผิดไหม)")
        print("-" * 66)
        for n, story in enumerate(merged, 1):
            members = story["members"]
            lead = members[0]
            print(f"[{n}] {len(members)} ใบ · {lead.get('level')} "
                  f"score {lead.get('score')}")
            print(f"    ★ id={lead.get('id')} [{lead.get('source_name') or '-'}]"
                  f" {lead.get('published_datetime') or '-'}")
            print(f"      {lead.get('title')}")
            for other in members[1:]:
                _, info = cluster.same_story(lead, other, cfg)
                print(f"    + id={other.get('id')}"
                      f" [{other.get('source_name') or '-'}]"
                      f" {other.get('published_datetime') or '-'}"
                      f"  ({info['reason']} j={info['jaccard']:.2f}"
                      f" r={info['ratio']:.2f})")
                print(f"      {other.get('title')}")
        if not merged:
            print("  (ไม่มีกลุ่มไหนถูกยุบด้วยเกณฑ์ปัจจุบัน)")
    finally:
        con.close()


def build_archive_cli(matcher_obj, outdir=None, require_guard=False, min_rows=1):
    """--build-archive: write the public back-catalogue site.

    READ-ONLY towards the system, exactly like --preview-public: it sends
    nothing, marks nothing alerted, writes no meta key, records no quota and
    never runs the story_key backfill. The ONLY thing it writes is the site
    directory.

    The site is built for a PUBLIC GitHub Pages host, so every page comes out of
    audience.public_rows() and is audited before a single file is written (see
    src/archive.py). A leak aborts the whole build rather than shipping a
    partly-clean site.
    """
    outdir = outdir or ARCHIVE_DIR
    con = storage.connect()
    try:
        stats = archive.build_site(
            con, matcher_obj.settings, outdir,
            require_guard=require_guard, min_rows=max(1, int(min_rows or 1)),
        )
    except (ValueError, RuntimeError) as exc:
        print("❌ " + str(exc))
        sys.exit(1)
    finally:
        con.close()

    print("สร้างคลังข่าวย้อนหลัง (อ่านอย่างเดียว ไม่ส่ง LINE ไม่แก้ฐานข้อมูล)")
    print("=" * 66)
    if stats.get("skipped"):
        print("  ปิดการสร้างคลังไว้ (archive_enabled=false) — ไม่ได้เขียนไฟล์ใดเลย")
        return
    quarters = " · ".join(f"{key} {n:,}" for key, n in stats["quarters"]) or "-"
    print(f"  แถวที่ขึ้นคลัง            : {stats['rows']:,} แถว"
          f" ({stats['groups']:,} เรื่อง)")
    print(f"  ช่วงวันที่               : {stats['first']} ถึง {stats['last']}")
    print(f"  จำนวนหน้า                : {stats['pages']:,} หน้า"
          f" (หน้าแรก + ไตรมาส {len(stats['quarters'])})")
    print(f"  ไตรมาส                  : {quarters}")
    print(f"  หน้าแรกบรรจุ             : {stats['index_rows']:,} แถว"
          f" · ข้อมูล {stats['index_bytes'] / 1024:.1f} KB")
    print(f"  ขนาดรวมทั้งคลัง           : {stats['bytes'] / 1024:.1f} KB"
          f" ({len(stats['files'])} ไฟล์)")
    print(f"  โฟลเดอร์ปลายทาง           : {stats['outdir']}")
    print("")
    print("ผลสแกนก่อนเขียนไฟล์")
    print(f"  ประโยคภายในที่ยามเฝ้าอยู่ : {stats['secrets']} ประโยค"
          + ("" if stats["secrets"] else
             "  ⚠️ ไม่มีโปรไฟล์ = ยามไม่ได้ตรวจอะไรเลย (ใช้ --require-guard)"))
    print(f"  ข้อมูลภายในที่หลุด : {stats['leaks']} ✅")
    print("  หมายเหตุ: ทุกหน้าถูกตรวจในหน่วยความจำก่อน ถ้าพบแม้จุดเดียว"
          "จะไม่มีไฟล์ใดถูกเขียน")


def backfill_cli():
    """--backfill-story-keys: force the story_key backfill to run again."""
    con = storage.connect()
    try:
        n = storage.backfill_story_keys(con)
        storage.set_meta(con, "story_key_backfilled", "1")
        print(f"เติม story_key ให้แถวเดิมแล้ว {n:,} แถว")
    finally:
        con.close()


def run_scheduler(matcher_obj):
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler()
    sched.add_job(
        realtime_job, "interval", minutes=15, args=[matcher_obj],
        id="realtime", max_instances=1, coalesce=True,
    )
    for hour, label in ROUND_LABELS.items():
        sched.add_job(
            daily_summary_job, "cron", hour=hour, minute=0,
            args=[matcher_obj, label, hour], id=f"summary_{hour}",
            misfire_grace_time=3600,
        )
    log.info("scheduler started: realtime/15min + summaries 07:00 12:00 18:00")
    realtime_job(matcher_obj)  # run once immediately on startup
    sched.start()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="steel-intel news monitor")
    parser.add_argument("--once", action="store_true", help="one fetch+alert cycle")
    parser.add_argument("--summary", action="store_true", help="force summary now")
    parser.add_argument("--quota", action="store_true",
                        help="print LINE push-quota status (sends nothing)")
    parser.add_argument("--quota-set-month", type=int, metavar="N",
                        help="backfill this month's request counter to N")
    parser.add_argument("--cluster-report", action="store_true",
                        help="read-only same-story duplicate audit (sends nothing)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="cluster-report/preview-public/playbook: only use "
                             "the first N rows")
    parser.add_argument("--backfill-story-keys", action="store_true",
                        help="fill story_key for rows stored before the column existed")
    parser.add_argument("--audience", action="store_true",
                        help="who receives what, at which detail (sends nothing)")
    parser.add_argument("--verify-recipient", action="store_true",
                        help="send one test message to the private destination")
    parser.add_argument("--preview-public", action="store_true",
                        help="print the team's version of the messages (sends nothing)")
    parser.add_argument("--playbook", action="store_true",
                        help="what to DO about the current top news "
                             "(full version, sends nothing)")
    parser.add_argument("--build-archive", action="store_true",
                        help="build the public back-catalogue site (sends nothing)")
    parser.add_argument("--out", metavar="DIR",
                        help="build-archive: output directory (default: ./site)")
    parser.add_argument("--require-guard", action="store_true",
                        help="build-archive: refuse to build when no operator "
                             "profile is loaded (the leak guard would be blind)")
    parser.add_argument("--min-rows", type=int, metavar="N", default=1,
                        help="build-archive: refuse to build with fewer rows than N")
    args = parser.parse_args()

    load_env()
    setup_logging()
    matcher_obj = Matcher()

    if args.cluster_report:
        cluster_report_cli(matcher_obj, args.limit)
    elif args.backfill_story_keys:
        backfill_cli()
    elif args.audience:
        audience_cli(matcher_obj)
    elif args.verify_recipient:
        verify_recipient_cli(matcher_obj)
    elif args.preview_public:
        preview_public_cli(matcher_obj, args.limit)
    elif args.playbook:
        playbook_cli(matcher_obj, args.limit)
    elif args.build_archive:
        build_archive_cli(matcher_obj, args.out, args.require_guard,
                          args.min_rows)
    elif args.quota or args.quota_set_month is not None:
        quota_cli(matcher_obj, args.quota_set_month)
    elif args.once:
        realtime_job(matcher_obj)
    elif args.summary:
        # Bangkok wall-clock, like every other round decision in this system:
        # the GitHub runner is UTC and would pick the wrong round.
        hour = now_bkk().hour
        label = ROUND_LABELS.get(hour, f"พิเศษ {now_bkk():%H:%M}")
        daily_summary_job(matcher_obj, label, hour)
    else:
        run_scheduler(matcher_obj)


if __name__ == "__main__":
    main()
