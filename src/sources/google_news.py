# -*- coding: utf-8 -*-
"""Google News RSS search - the backbone source (Thai + English + site: queries
that cover gov domains as a reliable fallback for direct scrapes).

ANTI-FAKE-NEWS: Google News surfaces any publisher. Every item carries the real
publisher in entry.source.title; we KEEP ONLY items whose publisher is on the
trusted whitelist (config: trusted_sources.publisher_aliases) and DROP the rest.
"""
import logging
import time
from urllib.parse import quote_plus

import feedparser

from .base import fetch_url, parse_published, is_trusted_publisher

log = logging.getLogger("steel_intel.sources.google_news")

RSS_FMT = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def _parse_feed(url, source_label, trusted_cfg):
    text = fetch_url(url)
    if not text:
        return [], 0
    feed = feedparser.parse(text)
    items, dropped = [], 0
    for entry in feed.entries:
        # Google News puts the real publisher in entry.source.title
        publisher = ""
        src = entry.get("source")
        if src and isinstance(src, dict):
            publisher = src.get("title", "")
        # Trust gate: drop anything from a non-whitelisted publisher.
        if not is_trusted_publisher(publisher, trusted_cfg):
            dropped += 1
            continue
        # Google News RSS summary is always boilerplate ("<a>title</a> publisher"),
        # never a real lead paragraph -> drop it so we don't show a bullet that
        # merely repeats the headline. High-impact items get a real summary via
        # article enrichment (when they have a direct, non-redirect URL).
        items.append({
            "title": (entry.get("title") or "").strip(),
            "url": entry.get("link", ""),
            "source": publisher or source_label,
            "source_name": publisher or source_label,
            "published": entry.get("published", ""),
            "published_datetime": parse_published(
                entry.get("published", ""), entry.get("published_parsed")
            ),
            "summary": "",
        })
    return items, dropped


def fetch_all(cfg, trusted_cfg=None):
    """Run every configured query (Thai, English, site:) and merge results.
    `trusted_cfg` is the trusted_sources block; items from untrusted publishers
    are dropped."""
    trusted_cfg = trusted_cfg or {}
    items, total_dropped = [], 0
    plans = (
        [(q, "th", "TH", "TH:th") for q in cfg.get("queries_th", [])]
        + [(q, "th", "TH", "TH:th") for q in cfg.get("site_queries", [])]
        + [(q, "th", "TH", "TH:th") for q in cfg.get("outlet_queries", [])]
        + [(q, "en-US", "US", "US:en") for q in cfg.get("queries_en", [])]
    )
    for q, hl, gl, ceid in plans:
        url = RSS_FMT.format(q=quote_plus(q), hl=hl, gl=gl, ceid=ceid)
        got, dropped = _parse_feed(url, "Google News", trusted_cfg)
        total_dropped += dropped
        log.info("google_news query=%r -> %d kept, %d dropped(untrusted)",
                 q, len(got), dropped)
        items.extend(got)
        time.sleep(0.5)  # be polite between queries
    log.info("google_news total: %d kept, %d dropped by trust gate",
             len(items), total_dropped)
    return items
