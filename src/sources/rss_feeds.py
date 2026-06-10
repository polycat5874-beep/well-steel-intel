# -*- coding: utf-8 -*-
"""Direct RSS feeds (business news outlets + ISIT). Items are filtered later by
the matcher, so broad feeds are fine here."""
import logging

import feedparser

from .base import fetch_url, parse_published

log = logging.getLogger("steel_intel.sources.rss_feeds")


def fetch_all(feed_cfgs):
    items = []
    for feed_cfg in feed_cfgs:
        name, url = feed_cfg["name"], feed_cfg["url"]
        text = fetch_url(url, retries=2)
        if not text:
            log.warning("rss feed unavailable: %s (%s)", name, url)
            continue
        feed = feedparser.parse(text)
        count = 0
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "title": title,
                "url": entry.get("link", ""),
                "source": name,
                "source_name": name,
                "published": entry.get("published", ""),
                "published_datetime": parse_published(
                    entry.get("published", ""), entry.get("published_parsed")
                ),
                "summary": (entry.get("summary") or "")[:500],
            })
            count += 1
        log.info("rss feed %s -> %d items", name, count)
    return items
