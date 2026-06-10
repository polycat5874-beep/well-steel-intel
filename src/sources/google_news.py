# -*- coding: utf-8 -*-
"""Google News RSS search - the backbone source (Thai + English + site: queries
that cover gov domains as a reliable fallback for direct scrapes)."""
import logging
import time
from urllib.parse import quote_plus

import feedparser

from .base import fetch_url

log = logging.getLogger("steel_intel.sources.google_news")

RSS_FMT = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def _parse_feed(url, source_label):
    text = fetch_url(url)
    if not text:
        return []
    feed = feedparser.parse(text)
    items = []
    for entry in feed.entries:
        # Google News puts the real publisher in entry.source.title
        publisher = ""
        src = entry.get("source")
        if src and isinstance(src, dict):
            publisher = src.get("title", "")
        items.append({
            "title": (entry.get("title") or "").strip(),
            "url": entry.get("link", ""),
            "source": publisher or source_label,
            "published": entry.get("published", ""),
            "summary": (entry.get("summary") or "")[:500],
        })
    return items


def fetch_all(cfg):
    """Run every configured query (Thai, English, site:) and merge results."""
    items = []
    plans = (
        [(q, "th", "TH", "TH:th") for q in cfg.get("queries_th", [])]
        + [(q, "th", "TH", "TH:th") for q in cfg.get("site_queries", [])]
        + [(q, "th", "TH", "TH:th") for q in cfg.get("outlet_queries", [])]
        + [(q, "en-US", "US", "US:en") for q in cfg.get("queries_en", [])]
    )
    for q, hl, gl, ceid in plans:
        url = RSS_FMT.format(q=quote_plus(q), hl=hl, gl=gl, ceid=ceid)
        got = _parse_feed(url, "Google News")
        log.info("google_news query=%r -> %d items", q, len(got))
        items.extend(got)
        time.sleep(0.5)  # be polite between queries
    return items
