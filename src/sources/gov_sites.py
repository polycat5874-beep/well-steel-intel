# -*- coding: utf-8 -*-
"""Best-effort scraper for government news pages (DFT / TISI / Customs / DIW).
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
                "published": "",
                "summary": "",
            })
            count += 1
            if count >= MAX_LINKS_PER_PAGE:
                break
        log.info("gov page %s -> %d candidate links", name, count)
    return items
