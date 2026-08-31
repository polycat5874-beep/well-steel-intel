# -*- coding: utf-8 -*-
"""The public back-catalogue: every stored headline, as a static offline site.

WHY THIS FILE IS WRITTEN THE WAY IT IS
--------------------------------------
The generated site is meant to sit on GitHub Pages, in a PUBLIC repository, so
whatever this module writes can be read by anyone on the internet - forever, and
by machines. The database it reads from, on the other hand, carries this
operator's own reading of the news: impact notes, the score, the matched
keywords, the watchlist. None of that may ever reach a page here.

So the pipeline is deliberately narrow and one-directional:

    storage.get_since(con, "")        every row, READ ONLY
        -> audience.public_rows()     projection to the publicly showable fields
        -> ARCHIVE_FIELDS             a second, narrower allow-list
        -> encode()                   a compact JSON document
        -> render                     HTML held in memory
        -> audit                      find_leaks() + FORBIDDEN_TOKENS
        -> write                      only if the audit found nothing at all

Nothing in this module writes to the database. No set_meta, no mark_alerted, no
ensure_story_keys, no notifier: building the archive must never change what the
alerting system will do next, and must never cost a LINE push.

WHY TWO ALLOW-LISTS
-------------------
audience.PUBLIC_ROW_FIELDS is what may be BROADCAST to a chat. ARCHIVE_FIELDS is
what may be PUBLISHED TO THE WEB, which is a strictly smaller promise (a chat
message is read once by people who added the OA; a web page is indexed, mirrored
and archived). Keeping the second list here means a field added to the first one
in 2027 does not silently start appearing on a public web page.
"""
import html
import json
import logging
import os
import time

from . import audience, cluster, storage
from .sources.base import now_bkk

log = logging.getLogger("steel_intel.archive")


# =========================================================================
# A. the allow-list, and the guard that keeps it honest
# =========================================================================

ARCHIVE_FIELDS = frozenset({
    "id", "title", "url", "source", "source_name",
    "published_datetime", "fetched_at", "summary", "level", "topics",
})
# A field added to PUBLIC_ROW_FIELDS in the future must never flow in here by
# accident; and a typo here must never silently widen the projection.
assert ARCHIVE_FIELDS <= audience.PUBLIC_ROW_FIELDS

# Never used as a CSS class, JS identifier or Thai label anywhere in the output:
# their presence in a page can only mean a raw row was dumped.
FORBIDDEN_TOKENS = ("impact_notes", "critical_hits", "watchlist_hits",
                    "story_key", "alerted", '"score"', '"hash"')

# Longest first: a Google News link also starts with "https://".
URL_PREFIXES = ("https://news.google.com/rss/articles/", "https://", "http://")

LEVEL_CODE = {"RED": "R", "ORANGE": "O", "YELLOW": "Y", "GRAY": "G"}
# The coloured dots live in web/app.js as \uXXXX escapes, not here: a page built
# with archive_include_level=false must not contain those characters at all.

SUMMARY_MAX = 240
UNDATED = "undated"

SITE_TITLE = "คลังข่าวอุตสาหกรรมเหล็ก"
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

INDEX_MAX_KB_DEFAULT = 900

# The front end lives in real .html/.css/.js files (web/) so it can be edited
# and read like a web page instead of as Python string literals. They are read
# once and INLINED at build time - the published site is still one self-contained
# file per page, with no request to anything.
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web")

# How many rows the <noscript> fallback lists. Deliberately tiny: the point of
# this rewrite is that the news is shipped ONCE, as JSON, and drawn by the
# browser. This block only keeps the page from being blank for a reader with
# scripting switched off.
NOSCRIPT_ROWS = 20

_ASSETS = {}


def asset(name):
    """web/<name>, read once and cached. A missing file is a build error: a page
    without its stylesheet or its script is not a page."""
    if name not in _ASSETS:
        path = os.path.join(WEB_DIR, name)
        with open(path, encoding="utf-8") as fh:
            _ASSETS[name] = fh.read()
    return _ASSETS[name]


def _int_setting(settings, key, default):
    try:
        return int((settings or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _enabled(settings):
    return bool((settings or {}).get("archive_enabled", True))


# =========================================================================
# B. rows
# =========================================================================

def rows_for_archive(con):
    """Every stored row, projected down to what may be published.

    READ ONLY: a single SELECT. It must stay that way - this runs on a public
    build step and must never be able to change alert state.
    """
    rows = storage.get_since(con, "")          # every row; no writes, no backfill
    rows = audience.public_rows(rows)          # FIRST thing done to them
    out = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue                            # nothing to show, nothing to link
        item = {k: v for k, v in row.items() if k in ARCHIVE_FIELDS}
        item["title"] = title
        published = str(item.get("published_datetime") or "").strip()
        fetched = str(item.get("fetched_at") or "").strip()
        stamp = published or fetched
        item["disp"] = stamp[:16]
        # df=1 means "this is when WE saw it, not when it was published" - the
        # reader is told, instead of being shown a fetch time dressed up as a
        # publication time.
        item["df"] = 0 if published else (1 if fetched else 0)
        out.append(item)
    # Newest first. sort() is stable, so rows sharing a timestamp keep the order
    # storage handed us (score DESC, id DESC) - i.e. the strongest telling of a
    # story stays in front, which is the order cluster.group_stories expects.
    out.sort(key=lambda r: r["disp"], reverse=True)
    return out


def quarter_key(disp):
    """'2026-08-27T10:05' -> '2026-Q3'. Anything undatable -> 'undated', which
    still gets a page of its own: no row may exist only in the index."""
    text = disp or ""
    if len(text) < 7 or text[4] != "-":
        return UNDATED
    try:
        year, month = int(text[:4]), int(text[5:7])
    except ValueError:
        return UNDATED
    if not 1 <= month <= 12:
        return UNDATED
    return "%04d-Q%d" % (year, (month - 1) // 3 + 1)


def quarter_label(key):
    if key == UNDATED:
        return "ไม่ทราบวันที่"
    return "ไตรมาส %s ปี %s" % (key[-1], key[:4])


def _group_by_day(rows, settings):
    """Tag every row with `g`, an integer story group, computed ONE DAY AT A TIME.

    The whole table must never be handed to cluster.group_stories: grouping is
    O(rows x groups) and anything past cluster_max_rows (600) is skipped
    outright, so a single call over a year of news would be both slow and a
    no-op. A day is also the honest unit - two outlets carrying one story carry
    it on the same day.

    `g` is a plain running integer, never cluster.story_key: a story key is
    internal bookkeeping and does not belong in a published file.

    Every row keeps its own entry. Grouping here only says "these are the same
    story"; it never removes a row (the no-hiding rule, see cluster.py).
    """
    by_day, order = {}, []
    for row in rows:
        day = (row.get("disp") or "")[:10]
        if day not in by_day:
            by_day[day] = []
            order.append(day)
        by_day[day].append(row)

    group_no = 0
    for day in order:
        for story in cluster.group_stories(by_day[day], settings, label="archive"):
            for member in story["members"]:
                member["g"] = group_no
            group_no += 1
    for row in rows:
        row.setdefault("g", -1)     # a row clustering somehow skipped is still shown
    log.info("archive: %d rows over %d day(s) -> %d story group(s)",
             len(rows), len(order), group_no)
    return rows


# =========================================================================
# C. the compact document
# =========================================================================

def encode(rows, settings=None):
    """Rows -> the compact JSON document embedded in a page.

    Shape (indexes point into the shared "pre"/"src"/"top" tables, which is what
    keeps a year of news small enough to ship inside an HTML file):

      rows[i] = [id, title, preIdx, urlRest, srcIdx, disp, df, summary,
                 level, [topicIdx...], g]
    """
    settings = settings or {}
    include_level = bool(settings.get("archive_include_level", True))
    src, src_idx = [], {}
    top, top_idx = [], {}
    out = []
    for row in rows:
        name = str(row.get("source_name") or row.get("source") or "")
        if name not in src_idx:
            src_idx[name] = len(src)
            src.append(name)

        topics = []
        for topic in (row.get("topics") or []):
            text = str(topic)
            if text not in top_idx:
                top_idx[text] = len(top)
                top.append(text)
            topics.append(top_idx[text])

        # The URL is stored VERBATIM (minus the shared prefix): this document is
        # data, not markup, and a stored link is evidence of what was published.
        # ANY renderer - this file's HTML, or the browser-side one that comes
        # next - MUST put it through safe_url() before it becomes an href, or a
        # "javascript:" link out of a scraped feed becomes a live one.
        url = str(row.get("url") or "")
        prefix_idx, rest = -1, url
        for i, prefix in enumerate(URL_PREFIXES):
            if url.startswith(prefix):
                prefix_idx, rest = i, url[len(prefix):]
                break

        code = LEVEL_CODE.get(row.get("level"), "") if include_level else ""
        out.append([
            row.get("id") or 0,
            str(row.get("title") or ""),
            prefix_idx,
            rest,
            src_idx[name],
            str(row.get("disp") or ""),
            int(row.get("df") or 0),
            str(row.get("summary") or "")[:SUMMARY_MAX],
            code,
            topics,
            int(row.get("g", -1)),
        ])
    return {
        "v": 1,
        "gen": now_bkk().strftime("%Y-%m-%dT%H:%M"),
        "genms": int(time.time() * 1000),
        "lv": 1 if include_level else 0,
        "pre": list(URL_PREFIXES),
        "src": src,
        "top": top,
        "rows": out,
    }


def payload_json(doc):
    """The document as it is embedded in <script type="application/json">.

    A headline containing "</script>" would otherwise close the tag and turn
    news text into executable markup, so <, > and & leave as \\uXXXX escapes.
    JSON parsers decode them back; an HTML parser never sees a tag.
    """
    payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    payload = (payload.replace("&", "\\u0026")
                      .replace("<", "\\u003c").replace(">", "\\u003e"))
    return payload


def _payload_bytes(doc):
    return len(payload_json(doc).encode("utf-8"))


# =========================================================================
# D. pages
# =========================================================================

def _index_slice(rows, settings):
    """The newest rows that fit inside archive_index_max_kb.

    The cap is on the EMBEDDED PAYLOAD, which is what actually has to travel to
    a reader's browser. Trimming is measured rather than estimated: a headline
    table is not a fixed number of bytes per row.
    """
    cap = max(1, _int_setting(settings, "archive_index_max_kb",
                              INDEX_MAX_KB_DEFAULT)) * 1024
    kept = list(rows)
    if not kept or _payload_bytes(encode(kept, settings)) <= cap:
        return kept
    n = len(kept)
    while n > 1:
        n = int(n * 0.8) if n > 5 else n - 1
        if _payload_bytes(encode(kept[:n], settings)) <= cap:
            break
    log.info("archive index capped at %d of %d rows (<= %d KB)",
             n, len(kept), cap // 1024)
    return kept[:n]


def pages(rows, settings=None):
    """The whole site as page descriptors, index first, quarters newest first.

    EVERY row appears on its quarter page, always. The index is a shortcut for
    the reader, never the only copy of anything - so lowering
    archive_index_max_kb can never make news disappear from the archive.
    """
    settings = settings or {}
    buckets, order = {}, []
    for row in rows:
        key = quarter_key(row.get("disp"))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)
    # Newest quarter first; the undated bucket always sits at the end.
    order.sort(key=lambda k: "" if k == UNDATED else k, reverse=True)

    index_rows = _index_slice(rows, settings)
    out = [{
        "path": "index.html",
        "kind": "index",
        "key": "index",
        "label": "หน้าแรก",
        "rows": index_rows,
        "total": len(rows),
    }]
    for key in order:
        out.append({
            "path": "q/%s.html" % key,
            "kind": "quarter",
            "key": key,
            "label": quarter_label(key),
            "rows": buckets[key],
            "total": len(buckets[key]),
        })
    for page in out:
        page["doc"] = encode(page["rows"], settings)
        # What the browser needs to caption the page. Kept in the payload rather
        # than in template tokens so the header, the counters and the CSV all
        # read the same numbers from one place.
        page["doc"]["pg"] = {
            "k": page["kind"],
            "lb": page["label"],
            "n": len(page["rows"]),
            "t": page["total"],
        }
    return out


# =========================================================================
# E. rendering: the shell only - the news itself is drawn by the browser
# =========================================================================
#
# THE RULE THIS SECTION EXISTS TO ENFORCE
# ---------------------------------------
# The news is shipped ONCE per page, as the JSON document above. Nothing here
# may write a second, HTML-shaped copy of it. The first version of this file
# did: it rendered all 989 headlines into <li> elements next to the very same
# 989 headlines inside the embedded JSON, which made index.html 1.14 MB, 62% of
# it duplicated text that no reader ever saw twice.
#
# So render_page() only assembles a shell (head, header, filters, footer) around
# the payload, and web/app.js draws the list from it. The single exception is the
# <noscript> block, capped at NOSCRIPT_ROWS rows: a reader with scripting off
# gets the newest few headlines instead of a blank page.
#
# Values are substituted with str.replace() on comment tokens - never with
# str.format() or an f-string. The template, the stylesheet and the script are
# full of { and }, and a format call would either explode or silently mangle
# them.


def _esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(url):
    """Only real web links become links. Anything else (javascript:, data:, a
    relative path) is rendered as plain text instead.

    web/app.js repeats this check before it builds an href: the payload carries
    the URL verbatim (it is evidence of what was published), so every renderer
    on either side of the wire has to filter it for itself."""
    text = str(url or "").strip()
    low = text.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return text
    return ""


def _nav_html(page, all_pages):
    """Links between the pages of the archive.

    Relative only: GitHub Pages serves a project site from /<repo>/, so an
    absolute "/q/..." would 404.
    """
    up = "../" if page["kind"] == "quarter" else ""
    links = []
    if page["kind"] == "quarter":
        links.append('<a href="%sindex.html">&larr; หน้าแรก</a>' % up)
    for other in all_pages:
        if other["kind"] != "quarter":
            continue
        links.append('<a href="%s%s">%s (%d)</a>'
                     % (up, _esc(other["path"]), _esc(other["label"]),
                        other["total"]))
    return "".join(links) or "—"


def _fallback_html(rows):
    """The <noscript> block: the newest NOSCRIPT_ROWS headlines, nothing else.

    This is the only place on the page where a headline is turned into markup,
    which is exactly why it goes through _esc() and safe_url(): a title
    containing "</script>" must arrive as text, and a "javascript:" link out of
    a scraped feed must never become an href.
    """
    out = []
    for row in rows[:NOSCRIPT_ROWS]:
        when = _esc(str(row.get("disp") or "-").replace("T", " "))
        if row.get("df"):
            when += " (เวลาที่ระบบพบข่าว)"
        who = _esc(row.get("source_name") or row.get("source") or "")
        title = _esc(row.get("title"))
        url = safe_url(row.get("url"))
        head = ('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                % (_esc(url), title)) if url else title
        out.append('<div class="nsitem"><span class="when">%s%s</span>%s</div>'
                   % (when, (" · " + who) if who else "", head))
    return "".join(out)


def render_page(page, all_pages, settings=None):
    """One page of the archive as a complete, self-contained HTML document."""
    settings = settings or {}
    doc = page.get("doc") or encode(page["rows"], settings)
    payload = ('<script id="d" type="application/json">%s</script>'
               % payload_json(doc))
    # str.replace() with comment tokens ONLY - see the note at the top of this
    # section. The values are inserted in this order so that nothing inserted
    # earlier can be re-scanned as a token: none of the assets contains one.
    return (asset("template.html")
            .replace("<!--__CSS__-->", asset("app.css"))
            .replace("<!--__JS__-->", asset("app.js"))
            .replace("<!--__DATA__-->", payload)
            .replace("<!--__NAV__-->", _nav_html(page, all_pages))
            .replace("<!--__TITLE__-->", _esc(SITE_TITLE))
            .replace("<!--__FALLBACK__-->", _fallback_html(page["rows"])))


def render_site(page_list, settings=None):
    """{path: html} for every page. Nothing is written here on purpose - the
    audit runs over this dict first."""
    return {page["path"]: render_page(page, page_list, settings)
            for page in page_list}


# =========================================================================
# F. the audit, and the build
# =========================================================================

def audit(files):
    """(leaks, tokens) found across a rendered site. Both must be empty.

    The offending sentence is never returned or logged in full: a log line that
    quotes the secret IS the leak.
    """
    leaks, tokens = [], []
    for path in sorted(files):
        text = files[path]
        for secret in audience.find_leaks(text):
            leaks.append((path, len(secret)))
        for token in FORBIDDEN_TOKENS:
            if token in text:
                tokens.append((path, token))
    return leaks, tokens


def build_site(con, settings=None, outdir=None, require_guard=False, min_rows=1):
    """Build the whole archive under `outdir`. Returns a stats dict.

    The order of the checks below is the whole point of this function:

      1. switched off        -> do nothing at all (never write an empty site)
      2. too few rows        -> raise (an empty archive must not overwrite a
                                good one, e.g. when the DB is unreachable)
      3. guard unarmed       -> raise when the caller asked for the guard: with
                                no profile loaded, find_leaks() returns [] for
                                every input, which reads as "clean" while
                                nothing was actually checked
      4. render, then audit  -> in memory
      5. write               -> only if the audit is completely silent
    """
    settings = settings or {}
    outdir = outdir or "site"
    if not _enabled(settings):
        log.info("archive build skipped: archive_enabled is off")
        return {"skipped": True, "reason": "archive_enabled=false",
                "outdir": outdir, "rows": 0, "files": []}

    rows = rows_for_archive(con)
    if len(rows) < min_rows:
        raise ValueError(
            "ยกเลิกการสร้างคลัง: มีข่าวเพียง %d แถว (ต้องมีอย่างน้อย %d) — "
            "คลังเปล่าต้องไม่ถูกเผยแพร่ทับของเดิม" % (len(rows), min_rows))

    secrets = len(audience.profile_secrets())
    if require_guard and not secrets:
        raise RuntimeError(
            "ยกเลิกการสร้างคลัง: ยามยังไม่ติดอาวุธ (guard is unarmed) — "
            "ไม่ได้โหลดโปรไฟล์ จึงไม่มีประโยคให้ตรวจ ผลตรวจ 'ผ่าน' จะไม่มี"
            "ความหมาย ตั้ง STEEL_INTEL_PROFILE_JSON หรือ config/profile.json ก่อน")

    rows = _group_by_day(rows, settings)
    page_list = pages(rows, settings)
    files = render_site(page_list, settings)
    try:
        files["robots.txt"] = asset("robots.txt")
    except OSError:
        # The rule matters more than the file: a missing web/robots.txt must
        # never publish a crawlable archive.
        files["robots.txt"] = ROBOTS_TXT
    files[".nojekyll"] = ""

    leaks, tokens = audit(files)
    if leaks or tokens:
        raise RuntimeError(
            "ยกเลิกการสร้างคลัง: พบข้อมูลภายในในหน้าเว็บ — ประโยคภายใน %d จุด "
            "· คำต้องห้าม %d จุด (%s) ไม่มีไฟล์ใดถูกเขียน"
            % (len(leaks), len(tokens),
               ", ".join(sorted({"%s:%s" % (p, t) for p, t in tokens})) or "-"))

    written, total = [], 0
    for path in sorted(files):
        full = os.path.join(outdir, *path.split("/"))
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(files[path])
        total += len(files[path].encode("utf-8"))
        written.append(path)

    index_page = page_list[0]
    stamps = [r["disp"] for r in rows if r.get("disp")]
    stats = {
        "skipped": False,
        "outdir": outdir,
        "rows": len(rows),
        "groups": len({r.get("g") for r in rows}),
        "pages": len(page_list),
        "quarters": [(p["key"], p["total"]) for p in page_list[1:]],
        "index_rows": len(index_page["rows"]),
        "index_bytes": _payload_bytes(index_page["doc"]),
        "bytes": total,
        "first": min(stamps) if stamps else "-",
        "last": max(stamps) if stamps else "-",
        "secrets": secrets,
        "leaks": 0,
        "files": written,
    }
    log.info("archive built: %d rows -> %d pages, %d KB in %s",
             stats["rows"], stats["pages"], stats["bytes"] // 1024, outdir)
    return stats
