# -*- coding: utf-8 -*-
"""Shared source helpers.

Three responsibilities live here so every source module behaves consistently:

  1. HTTP fetch with retry/backoff (`fetch_url`) - a flaky/down site never
     crashes the whole cycle.
  2. Publication date/time parsing (`parse_published`, `parse_article_meta`) -
     normalised to ISO-8601 in Asia/Bangkok so the original release time can be
     displayed.
  3. Article enrichment (`enrich_article`) - fetch one article page and pull a
     precise published_datetime + a short lead-paragraph summary. This is the
     EXPENSIVE path: callers gate it to high-impact (ORANGE/RED) items only.

`python-dateutil` and `beautifulsoup4` are preferred but OPTIONAL: if either is
missing the code degrades to pure-regex parsing rather than failing to import.
"""
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

log = logging.getLogger("steel_intel.sources")

# Optional deps: degrade gracefully to regex if unavailable.
try:
    from dateutil import parser as _dateutil_parser
except ImportError:  # pragma: no cover - environment dependent
    _dateutil_parser = None
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - environment dependent
    BeautifulSoup = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 steel-intel/1.0"
    )
}

# All times are normalised to Thailand local time for display.
BKK_TZ = timezone(timedelta(hours=7))


# --- URL canonicalisation (dedup hashing) --------------------------------

# Tracking / analytics query params that vary per share but point at the SAME
# article. Left in the URL they defeat dedup-by-hash: the same story arriving
# with a fresh ?utm_source / ?fbclid hashes differently and slips past the
# UNIQUE(hash) constraint, so an old article re-appears as "new". We strip these
# (and the #fragment) before hashing. Meaningful params (?id=, ?p=, ?newsid=)
# are KEPT because some CMSes use them as the article identifier.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_reader", "utm_name", "utm_social", "utm_brand", "utm_referrer",
    "fbclid", "gclid", "dclid", "gclsrc", "msclkid", "yclid", "twclid",
    "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "ref_url", "referrer",
    "_ga", "_gl", "spm", "scm", "cmpid", "ncid", "wt.mc_id", "cmp", "source",
}


def canonicalize_url(url):
    """Normalise a URL so the same article always maps to the same dedup key.

    Drops tracking query params (utm_*/fbclid/gclid/...) and the #fragment,
    lowercases scheme+host, and trims a trailing '/'. Returns '' for falsy input
    and the original (stripped) string if it can't be parsed."""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme and not parts.netloc:  # not a real URL (e.g. bare id)
        return url
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(kept)
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or parts.path
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


# --- Freshness / lookback window -----------------------------------------

def now_bkk():
    """Timezone-aware 'now' in Asia/Bangkok (NOT the server's local/UTC clock)."""
    return datetime.now(tz=BKK_TZ)


def _parse_bkk(iso_str):
    """Parse a stored published_datetime back into a tz-aware Asia/Bangkok
    datetime. Our stored strings are 'YYYY-MM-DDTHH:MM:SS' (BKK, naive); a naive
    value is tagged BKK, an offset-bearing one is converted. None if unparseable."""
    if not iso_str:
        return None
    s = str(iso_str).strip()
    dt = None
    if _dateutil_parser is not None:
        try:
            dt = _dateutil_parser.parse(s)
        except (ValueError, OverflowError, TypeError):
            return None
    else:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BKK_TZ)
    return dt.astimezone(BKK_TZ)


def is_fresh(published_iso, lookback_hours=24, keep_if_unknown=True, now=None):
    """True if the article was published within `lookback_hours`.

    Comparison is fully timezone-aware in Asia/Bangkok, so a UTC server clock
    can't make a Thai article look 7h younger. An empty/unparseable date is
    NEVER silently treated as 'now' (that is exactly what lets an undated old
    article masquerade as new); instead `keep_if_unknown` decides: keep it
    (True, conservative — e.g. gov listing links that expose no date) or drop it
    (False, strict). A future timestamp (clock skew) counts as fresh."""
    now = now or now_bkk()
    dt = _parse_bkk(published_iso)
    if dt is None:
        return keep_if_unknown
    if dt > now:
        return True
    return (now - dt) <= timedelta(hours=lookback_hours)


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


# --- Date / time parsing -------------------------------------------------

THAI_MONTHS = {
    "มกราคม": 1, "ม.ค.": 1, "ม.ค": 1,
    "กุมภาพันธ์": 2, "ก.พ.": 2, "ก.พ": 2,
    "มีนาคม": 3, "มี.ค.": 3, "มี.ค": 3,
    "เมษายน": 4, "เม.ย.": 4, "เม.ย": 4,
    "พฤษภาคม": 5, "พ.ค.": 5, "พ.ค": 5,
    "มิถุนายน": 6, "มิ.ย.": 6, "มิ.ย": 6,
    "กรกฎาคม": 7, "ก.ค.": 7, "ก.ค": 7,
    "สิงหาคม": 8, "ส.ค.": 8, "ส.ค": 8,
    "กันยายน": 9, "ก.ย.": 9, "ก.ย": 9,
    "ตุลาคม": 10, "ต.ค.": 10, "ต.ค": 10,
    "พฤศจิกายน": 11, "พ.ย.": 11, "พ.ย": 11,
    "ธันวาคม": 12, "ธ.ค.": 12, "ธ.ค": 12,
}
# Thai digits -> Arabic, so "๒๕๖๙" parses like "2569".
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_THAI_DATE_RE = re.compile(
    r"(\d{1,2})\s*(" + "|".join(re.escape(m) for m in THAI_MONTHS) + r")\s*(\d{4})"
)
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")


def _to_bkk_iso(dt):
    """Normalise a datetime to Asia/Bangkok and return 'YYYY-MM-DDTHH:MM:SS'."""
    if dt.tzinfo is None:
        # Feeds without an explicit offset are assumed already-local-ish; tag UTC
        # only when we KNOW it's UTC (struct_time path does that explicitly).
        dt = dt.replace(tzinfo=BKK_TZ)
    return dt.astimezone(BKK_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _normalise_be_year(year):
    """Buddhist-era year (>2400) -> Gregorian."""
    return year - 543 if year > 2400 else year


def parse_thai_date(text):
    """Find a Thai-formatted date in free text. Returns ISO string or ''.
    Handles '10 มิถุนายน 2569', '10 มิ.ย. 2569', '๑๐ มิ.ย. ๒๕๖๙', '10/06/2569'."""
    if not text:
        return ""
    text = text.translate(_THAI_DIGITS)
    m = _THAI_DATE_RE.search(text)
    if m:
        day, mon_name, year = int(m.group(1)), m.group(2), int(m.group(3))
        month = THAI_MONTHS.get(mon_name)
        if month:
            try:
                return datetime(_normalise_be_year(year), month, day,
                                tzinfo=BKK_TZ).strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass
    m = _NUM_DATE_RE.search(text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(_normalise_be_year(year), month, day,
                            tzinfo=BKK_TZ).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    return ""


def parse_published(raw="", struct_time=None):
    """Best-effort publication time -> ISO (Asia/Bangkok), or '' if unknown.

    Order: feedparser struct_time (UTC, most reliable) -> dateutil on the raw
    string (handles RFC822/ISO) -> Thai-date regex. Empty string when nothing
    usable is found, so callers can decide whether to enrich further."""
    if struct_time is not None:
        try:
            # feedparser's *_parsed structs are UTC.
            dt = datetime(*struct_time[:6], tzinfo=timezone.utc)
            return _to_bkk_iso(dt)
        except (TypeError, ValueError):
            pass
    raw = (raw or "").strip()
    if raw:
        if _dateutil_parser is not None:
            try:
                return _to_bkk_iso(_dateutil_parser.parse(raw))
            except (ValueError, OverflowError, TypeError):
                pass
        thai = parse_thai_date(raw)
        if thai:
            return thai
    return ""


# --- Article enrichment (date + lead summary) ----------------------------

_META_DATE_PATTERNS = [
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+itemprop=["\']datePublished["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'<time[^>]+datetime=["\']([^"\']+)["\']',
]
_META_DESC_PATTERNS = [
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    r'"description"\s*:\s*"([^"]{40,})"',
]
_TAG_RE = re.compile(r"<[^>]+>")
_SENT_SPLIT_RE = re.compile(r"(?<=[\.\!\?。])\s+")
# Thai abbreviations end in a period but are NOT sentence boundaries; a fragment
# ending in one of these is re-merged with the following fragment.
_THAI_ABBREV = (
    "มอก.", "สมอ.", "ร.ง.", "พ.ร.บ.", "พ.ศ.", "ค.ศ.", "บจ.", "บมจ.",
    "น.ส.", "ดร.", "รศ.", "ผศ.", "พล.", "พ.ต.", "ร.ต.", "จ.", "อ.", "ต.",
)


def split_sentences(text):
    """Split text into sentences, but DON'T break after a Thai abbreviation
    (e.g. 'สมอ. เตรียมแก้ มอก. 24-2559' stays one sentence, not three)."""
    raw = [f for f in _SENT_SPLIT_RE.split(text or "") if f.strip()]
    out = []
    for frag in raw:
        frag = frag.strip()
        if out and any(out[-1].endswith(a) for a in _THAI_ABBREV):
            out[-1] = f"{out[-1]} {frag}"
        else:
            out.append(frag)
    return out


def _first_match(html, patterns):
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def _extract_with_bs4(html):
    """Return (date_str, desc) using BeautifulSoup. ('','') if nothing found."""
    soup = BeautifulSoup(html, "html.parser")
    date_str = ""
    for attrs in (
        {"property": "article:published_time"},
        {"itemprop": "datePublished"},
        {"name": "pubdate"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            date_str = tag["content"].strip()
            break
    if not date_str:
        t = soup.find("time")
        if t and t.get("datetime"):
            date_str = t["datetime"].strip()

    desc = ""
    for attrs in ({"property": "og:description"}, {"name": "description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            desc = tag["content"].strip()
            break
    if not desc:
        for p in soup.find_all("p"):
            txt = p.get_text(" ", strip=True)
            if len(txt) >= 60:  # skip menus/captions
                desc = txt
                break
    return date_str, desc


def summarise_text(text, max_sentences=2, max_chars=240):
    """Trim body text to a concise 2-3 sentence lead. Returns a clean string."""
    text = _TAG_RE.sub(" ", text or "")
    text = html.unescape(text)  # decode &nbsp; &amp; &#39; etc.
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    sentences = split_sentences(text)
    out, total = [], 0
    for s in sentences[: max_sentences + 1]:
        if out and total + len(s) > max_chars:
            break
        out.append(s)
        total += len(s)
        if len(out) >= max_sentences:
            break
    summary = " ".join(out) if out else text[:max_chars]
    return summary[:max_chars].rstrip()


def enrich_article(url):
    """Fetch one article page and extract (published_datetime, summary).

    Returns ('', '') on any failure - callers keep whatever the feed provided.
    EXPENSIVE (one extra HTTP round-trip): only call for high-impact items."""
    if not url:
        return "", ""
    html = fetch_url(url, timeout=15, retries=1)
    if not html:
        return "", ""
    if BeautifulSoup is not None:
        try:
            date_raw, desc = _extract_with_bs4(html)
        except Exception as exc:  # malformed HTML -> fall back to regex
            log.debug("bs4 parse failed, regex fallback: %s", exc)
            date_raw, desc = _first_match(html, _META_DATE_PATTERNS), _first_match(html, _META_DESC_PATTERNS)
    else:
        date_raw = _first_match(html, _META_DATE_PATTERNS)
        desc = _first_match(html, _META_DESC_PATTERNS)

    published_dt = parse_published(date_raw) or parse_thai_date(_TAG_RE.sub(" ", html)[:4000])
    summary = summarise_text(desc)
    return published_dt, summary


# --- Quality gate (drop nav / page-title junk indexed from gov sites) ----

# Google indexes gov-site nav/landing pages; their titles ("Untitled",
# "Integrated Tariff Database", "หน้าหลัก") look like news to a keyword matcher
# but carry no information. Drop them before analysis.
_JUNK_TITLE_PATTERNS = (
    "untitled", "integrated tariff database", "tariff database",
    "หน้าหลัก", "หน้าแรก", "homepage", "home page", "sitemap",
    "ระบบฐานข้อมูล", "e-service", "eservice", "เข้าสู่ระบบ", "login",
)


def is_junk_title(title):
    """True if a title looks like a website nav/landing page, not a headline."""
    if not title:
        return True
    t = title.strip()
    if any(p in t.lower() for p in _JUNK_TITLE_PATTERNS):
        return True
    # Strip the trailing ' - <publisher>' Google appends; a real headline rarely
    # has fewer than ~12 chars of actual content.
    head = t.split(" - ")[0].strip()
    return len(head) < 12


# --- Trust gate (anti-fake-news) -----------------------------------------

def is_trusted_publisher(name, trusted_cfg):
    """True if `name` matches a publisher alias in the whitelist (case-insensitive
    substring). Empty/None name -> not trusted (drop)."""
    if not name:
        return False
    low = name.lower()
    for alias in trusted_cfg.get("publisher_aliases", []):
        if alias.lower() in low:
            return True
    return False
