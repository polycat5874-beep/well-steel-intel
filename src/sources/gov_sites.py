# -*- coding: utf-8 -*-
"""Best-effort scraper for government news pages (DFT / TISI / Customs / DIW).

!!! NOT CALLED BY ANYTHING - RETIRED 2026-09-05 (Phase 7a) !!!
--------------------------------------------------------------
main.collect_cycle no longer calls fetch_all() and config/sources.json now has
`"gov_pages": []`. The file is kept ON PURPOSE (link extraction, the Thai
headline heuristic and the never-crash contract are all still correct) so that
the day one of these sites exposes a JSON feed, only the fetch half has to be
written.

WHY IT WAS RETIRED - measured, not assumed:
  * TISI and DIW returned `0 candidate links` on EVERY cycle (~96/day).
  * DFT stopped responding entirely: its TLS chain fails with
    `SSL UNEXPECTED_EOF` even with the GeoTrust intermediate in certs/.
  * Customs returned its full 30-link cap, but every one of them was a SITE
    MENU entry, not news, and 5 of those menu links scored as RELEVANT and were
    being written into news.db as if they were articles.
All four render their news client-side with JavaScript, so no static scrape can
ever see it. The group cost ~5.5s of every cycle for that.

BEFORE SWITCHING IT BACK ON: find a JSON/API endpoint. Re-pointing this at
another HTML listing page will fail the same way. Coverage in the meantime comes
from google_news.site_queries (site:dft.go.th / tisi.go.th / customs.go.th /
diw.go.th / industry.go.th), which reads the same announcements off an index
Google has already rendered.

Extracts <a> links whose text looks like a Thai news headline. Layout changes
or downtime never crash the cycle - Google News site: queries are the backstop."""
import logging
import re
from urllib.parse import urljoin

from .base import fetch_url

log = logging.getLogger("steel_intel.sources.gov_sites")

LINK_RE = re.compile(r'<a\s[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
THAI_RE = re.compile(r"[ก-๛]")

MAX_LINKS_PER_PAGE = 30


def _clean(text):
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_all(page_cfgs):
    items = []
    for page in page_cfgs:
        name, url, base = page["name"], page["url"], page["base"]
        html = fetch_url(url, retries=2)
        if not html:
            log.warning("gov page unavailable: %s (%s)", name, url)
            continue
        count = 0
        for href, raw_text in LINK_RE.findall(html):
            title = _clean(raw_text)
            # keep only headline-looking Thai links
            if not (12 <= len(title) <= 200 and THAI_RE.search(title)):
                continue
            items.append({
                "title": title,
                "url": urljoin(base, href.strip()),
                "source": name,
                "source_name": name,
                "published": "",
                # gov listing pages rarely expose a date; enrich_article() fills
                # this in for high-impact items by fetching the article page.
                "published_datetime": "",
                "summary": "",
            })
            count += 1
            if count >= MAX_LINKS_PER_PAGE:
                break
        log.info("gov page %s -> %d candidate links", name, count)
    return items
