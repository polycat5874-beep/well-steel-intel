# -*- coding: utf-8 -*-
"""Story clustering: collapse rows that are THE SAME STORY, at display time only.

WHY THIS EXISTS
---------------
`storage.item_hash` is sha256(canonicalize_url(url) + "|" + title). Three outlets
carrying one story publish it at three URLs under three different headlines, so
BY DESIGN they hash differently and all three land in the DB. Measured on the
live table (824 rows): 636 distinct stories -> 188 duplicate rows (23%), and 52
alerts had already gone out twice for the same event.

This module NEVER touches item_hash, insert_many, or anything else on the write
path, and nothing is ever deleted. Rows are only GROUPED while a message is being
built, so the reader gets one card per story instead of three.

WHY CHARACTER 3-GRAMS
---------------------
Thai puts no spaces between words, and Thai desks insert spaces wherever they
please, so splitting a headline on whitespace is useless. Real word segmentation
needs a dictionary/ML dependency (pythainlp) that this project will not add
(stdlib only). Character n-grams need neither: they measure how much character
material two headlines share, which is exactly what two rewrites of one story
have in common.

WHERE THE THRESHOLDS COME FROM
------------------------------
Measured on real Thai steel headlines out of this database:
  * one story re-worded by another desk .................. Jaccard 0.62 - 1.00
  * the dangerous near-miss pair (different events, same actors, same
    vocabulary): "ผู้ผลิตเตา IF ค้านแนวคิด กมอ." versus
    "สมาคมเหล็ก IF โวย มติ กมอ. สั่งเลิกผลิตข้ออ้อย" ............ Jaccard 0.15
There is a wide empty band between those two populations, so 0.62 sits in the
middle of the gap instead of on the edge of either. difflib's ratio (>= 0.70) is
a second, ORDER-AWARE opinion: n-grams are a bag, so two headlines assembled from
the same material in a different order can score high on Jaccard alone.

THE NO-HIDING RULE (the most important paragraph in this file)
--------------------------------------------------------------
A rendered card MUST name every outlet in the group, and MUST also print a
member's own headline whenever that headline differs (after normalisation) from
the leader's. This turns the worst failure mode of the feature - "collapsed
wrongly, so a story vanished and nobody noticed" - into "collapsed wrongly, and
the other story is visible right there inside the card". It is what makes the
risk of clustering acceptable at all. See summarizer._also_reported_lines.
"""
import difflib
import hashlib
import logging
import re

from .sources.base import parse_bkk, _THAI_DIGITS

log = logging.getLogger("steel_intel.cluster")

DEFAULTS = {
    "cluster_enabled": True,
    "cluster_jaccard_min": 0.62,
    "cluster_ratio_min": 0.70,
    "cluster_len_ratio_min": 0.50,
    "cluster_window_hours": 36,
    "cluster_max_rows": 600,
    "cluster_ngram": 3,
}

# Google News appends " - <publisher>" to every headline, and some desks prepend
# their section the same way ("โลกธุรกิจ - <ข่าว> - แนวหน้า").
#
# DO NOT "simplify" this to title.split(" - ")[0]: that keeps the FIRST field,
# which for แนวหน้า is the section name, so every แนวหน้า headline normalises to
# "โลกธุรกิจ" and unrelated stories score Jaccard 1.00 against each other (3,650
# false pairs measured). Only the LAST field is dropped, and only when enough of
# the headline survives to still identify the story.
_TAIL_RE = re.compile(r"\s+[-|–—]\s+[^-|–—]{1,40}$")
_TAIL_MIN_HEAD = 15

# Whitespace plus every quote/dash/bracket a desk might sprinkle in. Thai
# headlines differ mostly by spacing and quoting ("ซินเคอหยวน" versus
# "ซิน เคอ หยวน"), so all of it is deleted before comparing.
_PUNCT_RE = re.compile(
    "[\\s​ ‘’“”\"'!?.,:;\\-–—_/\\\\()\\[\\]{}|·•<>]+"
)

# --- numeric guard --------------------------------------------------------
# Two headlines about the same actors but DIFFERENT figures are different news
# ("ภาษี 50%" versus "ภาษี 25%"). A disagreeing number under the same unit
# vetoes a collapse outright.
_UNITS = ("%", "ตัน", "ตู้", "เส้น", "ราย", "แห่ง", "โรง", "บริษัท", "กิโล")
_NUM_UNIT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(" + "|".join(re.escape(u) for u in _UNITS) + r")"
)
# TIS standard numbers behave like a unit of their own: มอก. 24 and มอก. 20 are
# two different standards, and confusing them is a business-critical mistake.
_TIS_RE = re.compile(r"มอก\.?\s*(\d+)")


def _light(title):
    """Digits normalised + lowercased + whitespace collapsed, punctuation KEPT.
    Used by the numeric guard, which needs to see '24' sitting next to 'มอก.'."""
    if not title:
        return ""
    return re.sub(r"\s+", " ", str(title).translate(_THAI_DIGITS).lower()).strip()


def normalize_title(title):
    """Headline -> comparison form: publisher tail dropped, Thai digits mapped to
    Arabic, lowercased, all spacing and punctuation removed."""
    if not title:
        return ""
    t = str(title).strip()
    m = _TAIL_RE.search(t)
    if m:
        head = t[: m.start()].strip()
        if len(head) >= _TAIL_MIN_HEAD:  # never let a section label be the whole key
            t = head
    t = t.translate(_THAI_DIGITS).lower()
    return _PUNCT_RE.sub("", t)


def story_key(title):
    """Stable short key of the normalised headline. Empty string when nothing is
    left - an empty key must NEVER be read as "these two match"."""
    norm = normalize_title(title)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _grams(norm, n=3):
    """Character n-gram set of an already-normalised headline."""
    if not norm:
        return frozenset()
    if len(norm) <= n:
        return frozenset([norm])
    return frozenset(norm[i:i + n] for i in range(len(norm) - n + 1))


def _jaccard(a, b):
    """Intersection over union of two gram sets; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def _units(title):
    """{unit: {values}} for every number carrying one of the tracked units."""
    text = _light(title)
    out = {}
    for raw, unit in _NUM_UNIT_RE.findall(text):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            value = raw
        out.setdefault(unit, set()).add(value)
    for raw in _TIS_RE.findall(text):
        try:
            value = float(raw)
        except ValueError:
            value = raw
        out.setdefault("มอก.", set()).add(value)
    return out


def _maps_conflict(map_a, map_b):
    for unit, values_a in map_a.items():
        values_b = map_b.get(unit)
        if values_b and not (values_a & values_b):
            return True
    return False


def _unit_conflict(a, b):
    """True when both headlines quote the SAME unit with values that never
    overlap (50% vs 25%, มอก. 24 vs มอก. 20) - i.e. two different stories."""
    return _maps_conflict(_units(a), _units(b))


def _date_conflict(row_a, row_b, window_hours):
    """True when both rows carry a parseable publication time and those times are
    further apart than the window. The same headline republished days later is a
    NEW event (a measure re-run, a second seizure), not a duplicate."""
    dt_a = parse_bkk(row_a.get("published_datetime") or "")
    dt_b = parse_bkk(row_b.get("published_datetime") or "")
    if dt_a is None or dt_b is None:
        return False
    return abs((dt_a - dt_b).total_seconds()) > float(window_hours) * 3600.0


# --- config ---------------------------------------------------------------

def build_cfg(settings=None):
    """DEFAULTS overlaid with the cluster_* keys from the config settings, with
    coercion. A garbage value falls back to the default and never raises."""
    cfg = dict(DEFAULTS)
    if not settings:
        return cfg
    for key, default in DEFAULTS.items():
        if key not in settings:
            continue
        raw = settings[key]
        try:
            if isinstance(default, bool):
                cfg[key] = bool(raw)
            elif isinstance(default, int):
                cfg[key] = int(raw)
            else:
                cfg[key] = float(raw)
        except (TypeError, ValueError):
            log.warning("bad cluster setting %s=%r; using %r", key, raw, default)
    return cfg


def _prepare(row, cfg):
    """Everything the gates need, computed ONCE per row. Grouping is
    O(rows x groups), so re-normalising inside the inner loop would dominate."""
    title = row.get("title") or ""
    norm = normalize_title(title)
    key = row.get("story_key") or story_key(title)
    return {
        "row": row,
        "id": row.get("id"),
        "title": title,
        "norm": norm,
        "key": key,
        "grams": _grams(norm, cfg["cluster_ngram"]),
        "units": _units(title),
    }


def _same_prepared(pa, pb, cfg):
    """The six gates, each exiting as early as it can. Returns (is_same, info)."""
    info = {"reason": "", "jaccard": 0.0, "ratio": 0.0}

    # D1 - identical normalised headline. An empty key means "nothing left to
    # compare with", which must never be read as a match.
    if pa["key"] and pa["key"] == pb["key"]:
        info["reason"], info["jaccard"], info["ratio"] = "key", 1.0, 1.0
    else:
        # D2 - length ratio. A one-line brief and a long analysis piece are not
        # the same item even when they share vocabulary; this also stops a short
        # headline from being swallowed by a much longer one.
        la, lb = len(pa["norm"]), len(pb["norm"])
        if not la or not lb:
            info["reason"] = "blocked-empty"
            return False, info
        if min(la, lb) / max(la, lb) < cfg["cluster_len_ratio_min"]:
            info["reason"] = "blocked-len"
            return False, info

        # D3 - character 3-gram Jaccard (the main signal).
        jac = _jaccard(pa["grams"], pb["grams"])
        info["jaccard"] = jac
        if jac < cfg["cluster_jaccard_min"]:
            info["reason"] = "blocked-jaccard"
            return False, info

        # D4 - order-aware second opinion, only paid for once D3 has passed.
        ratio = difflib.SequenceMatcher(None, pa["norm"], pb["norm"],
                                        autojunk=False).ratio()
        info["ratio"] = ratio
        if ratio < cfg["cluster_ratio_min"]:
            info["reason"] = "blocked-ratio"
            return False, info
        info["reason"] = "fuzzy"

    # D5/D6 are VETOES, so they run last - on a candidate that already matched,
    # including one matched through D1. Deliberate difference from the plan's
    # numbering: had D1 short-circuited past them, an identical headline
    # republished three days later (the measure re-run) would collapse into the
    # original and the second event would disappear.
    if _maps_conflict(pa["units"], pb["units"]):
        info["reason"] = "blocked-unit"
        return False, info
    if _date_conflict(pa["row"], pb["row"], cfg["cluster_window_hours"]):
        info["reason"] = "blocked-date"
        return False, info
    return True, info


def same_story(row_a, row_b, cfg=None):
    """Do these two rows report the same story? Returns (bool, info) where info
    is {"reason": "key|fuzzy|blocked-<gate>", "jaccard": float, "ratio": float}."""
    cfg = cfg if (cfg and "cluster_ngram" in cfg) else build_cfg(cfg)
    return _same_prepared(_prepare(row_a, cfg), _prepare(row_b, cfg), cfg)


def _identity(rows):
    """One row = one story: the shape group_stories always returns."""
    out = []
    for row in rows:
        view = dict(row)
        view["also_reported"] = []
        out.append({"row": view, "ids": [row.get("id")], "members": [row]})
    return out


def group_stories(rows, settings=None, label=""):
    """Collapse rows reporting the same story. Returns a list of
    {"row": <leader copy, plus also_reported>, "ids": [...], "members": [...]}.

    `rows` MUST arrive in priority order (storage sorts ORDER BY score DESC,
    id DESC) and is NOT re-sorted here: the first row of a group is its leader -
    the highest-scoring telling of the story - and that is the one rendered.

    Grouping is LEADER clustering, never union-find/single-link: a row is only
    ever compared against the leader of each existing group. Single-link would
    chain A~B, B~C into one group even when A and C are plainly different
    stories, and on a news feed that chain runs away fast.

    This function NEVER raises. It sits on the same code path as the dead-man's
    switch, so any failure degrades to one-row-per-story rather than costing the
    alert.
    """
    rows = list(rows or [])
    if not rows:
        return []
    try:
        cfg = build_cfg(settings)
        if not cfg["cluster_enabled"]:
            return _identity(rows)
        if len(rows) > cfg["cluster_max_rows"]:
            log.warning("clustering skipped: %d rows exceeds cluster_max_rows=%d [%s]",
                        len(rows), cfg["cluster_max_rows"], label)
            return _identity(rows)

        groups = []          # [{"lead": <prepared>, "members": [row, ...]}]
        collapsed = 0
        for row in rows:
            prep = _prepare(row, cfg)
            placed = False
            for grp in groups:
                same, info = _same_prepared(grp["lead"], prep, cfg)
                if not same:
                    continue
                grp["members"].append(row)
                collapsed += 1
                placed = True
                log.info("story collapse [%s]: id=%s '%s' <- id=%s '%s'"
                         " (%s j=%.2f r=%.2f)",
                         label, grp["lead"]["id"], grp["lead"]["title"][:70],
                         prep["id"], prep["title"][:70],
                         info["reason"], info["jaccard"], info["ratio"])
                break
            if not placed:
                groups.append({"lead": prep, "members": [row]})

        stories = []
        for grp in groups:
            members = grp["members"]
            # Shallow copy: the dicts storage handed us must not be mutated,
            # they still belong to the caller.
            view = dict(members[0])
            view["also_reported"] = members[1:]
            stories.append({
                "row": view,
                "ids": [m.get("id") for m in members],
                "members": members,
            })
        log.info("clustered %d rows into %d stories (%d collapsed) [%s]",
                 len(rows), len(stories), collapsed, label)
        return stories
    except Exception as exc:  # noqa: BLE001 - must never break the alert path
        log.warning("clustering skipped: %s", exc)
        try:
            return _identity(rows)
        except Exception:  # pragma: no cover - dict(row) cannot realistically fail
            return [{"row": r, "ids": [], "members": [r]} for r in rows]
