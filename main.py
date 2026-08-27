# -*- coding: utf-8 -*-
"""steel-intel - steel industry news monitor + executive alerts.

Schedules (per approved spec):
  * realtime check every 15 minutes -> instant Telegram alert on critical news
  * daily summary at 07:00 / 12:00 / 18:00 (morning / noon / evening rounds)

CLI:
  python main.py            run the scheduler (production mode)
  python main.py --once     run one fetch+alert cycle then exit (smoke test)
  python main.py --summary  force a daily summary now then exit
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from src import storage, notifier, summarizer  # noqa: E402
from src.matcher import Matcher  # noqa: E402
from src.sources import google_news, rss_feeds, gov_sites  # noqa: E402
from src.sources.base import (  # noqa: E402
    enrich_article, summarise_text, is_junk_title, is_fresh,
)

log = logging.getLogger("steel_intel")

SOURCES_PATH = os.path.join(BASE_DIR, "config", "sources.json")
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


def realtime_job(matcher_obj):
    """15-minute loop: collect then alert every unalerted critical item."""
    log.info("=== realtime job start ===")
    try:
        collect_cycle(matcher_obj)
    except Exception as exc:
        log.error("collect cycle failed: %s", exc)

    con = storage.connect()
    try:
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

        cap = matcher_obj.settings.get("alert_max_per_cycle", 8)
        sent_ids = []
        for row in pending[:cap]:
            msg = summarizer.build_critical_alert(row, row)
            notifier.send(msg)
            sent_ids.append(row["id"])
        overflow = pending[cap:]
        if overflow:
            lines = [f"🚨 ข่าวสำคัญเพิ่มเติมอีก {len(overflow)} ชิ้นในรอบนี้:"]
            lines += [f"• {r['title']}" for r in overflow[:20]]
            notifier.send("\n".join(lines))
            sent_ids += [r["id"] for r in overflow]
        storage.mark_alerted(con, sent_ids)
        log.info("alerted %d critical items", len(sent_ids))
    finally:
        con.close()


def daily_summary_job(matcher_obj, round_label):
    """07:00/12:00/18:00 rounds: summarize items stored since last round.

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
        fallback = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        since = storage.get_meta(con, "last_summary_at", fallback)
        items = storage.get_since(con, since)
        if collect_error:
            health = f"เก็บข่าวรอบนี้ล้มเหลว: {str(collect_error)[:120]}"
        else:
            health = f"ระบบปกติ · รอบนี้ตรวจข่าว {stats[0]:,} ชิ้น"
        msg = summarizer.build_daily_summary(
            items, matcher_obj.cfg["watchlist"], round_label, health=health
        )
        notifier.send(msg)
        storage.set_meta(
            con, "last_summary_at", datetime.now().isoformat(timespec="seconds")
        )
        log.info("daily summary sent (%d items)", len(items))
    except Exception as exc:
        # The watcher itself is down (DB paused/unreachable, schema gone...).
        # Report it rather than letting the outage look like a quiet news day.
        log.error("daily summary failed: %s", exc)
        notifier.send(summarizer.build_system_alert(round_label, exc))
    finally:
        if con is not None:
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
            args=[matcher_obj, label], id=f"summary_{hour}",
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
    args = parser.parse_args()

    load_env()
    setup_logging()
    matcher_obj = Matcher()

    if args.once:
        realtime_job(matcher_obj)
    elif args.summary:
        hour = datetime.now().hour
        label = ROUND_LABELS.get(hour, f"พิเศษ {datetime.now():%H:%M}")
        daily_summary_job(matcher_obj, label)
    else:
        run_scheduler(matcher_obj)


if __name__ == "__main__":
    main()
