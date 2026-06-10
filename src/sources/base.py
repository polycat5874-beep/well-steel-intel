# -*- coding: utf-8 -*-
"""Shared HTTP fetch helper with retry/backoff. All sources go through here
so a flaky or down website never crashes the whole cycle."""
import logging
import time

import requests

log = logging.getLogger("steel_intel.sources")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 steel-intel/1.0"
    )
}


def fetch_url(url, timeout=20, retries=3, backoff=4):
    """GET url with retry. Returns response text, or None if all attempts fail."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            log.warning("fetch failed (%d/%d) %s : %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    return None
