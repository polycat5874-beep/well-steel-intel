# -*- coding: utf-8 -*-
"""What to DO about a story, not just what happened.

matcher.analyze() already answers "why does this news matter to us" - it
returns impact_notes, one per company-profile boost group that matched. This
module answers the next question: given that note, what is the operator
supposed to do today. The two live side by side in the profile: every boost
group carries a `note` (the reading) and an `action` (the instruction).

WHERE AN ACTION MAY LIVE (this is a security property, not an accident)
-----------------------------------------------------------------------
Actions come from load_profile_overlay() and NOTHING ELSE. This module never
reads config/keywords.json, not even as a fallback, and must never learn how:
keywords.json is committed to a PUBLIC repository (GitHub only gives unlimited
Actions minutes to public repos), while an action names which licences this
plant operates under and what has to be cleared before a furnace is started.
A `note` is the company's reading of the news; an `action` is the company's
soft underbelly written down. Both belong in the gitignored overlay.

An action is therefore FULL-AUDIENCE ONLY. src/audience.py counts every action
as a protected sentence (profile_secrets), so all three leak layers already
cover it, and src/summarizer.py renders it in the audience="full" branch only.

WHY NOTES ARE THE KEY, NOT THE POSITION
---------------------------------------
A stored row keeps the note TEXT (impact_notes), never the index of the boost
group that produced it. Keying on text means rows written months ago still
resolve, and - more importantly - a note that no longer matches any group in
the current profile resolves to NOTHING. An unmatched note getting no action is
the correct outcome; an unmatched note getting the neighbouring group's action
would be a wrong instruction attached to real news, which is worse than silence.

NEVER RAISES
------------
This module sits on the same call path as the dead-man's switch, which exists
precisely because a silent watcher looks exactly like a quiet news day. A
malformed profile yields {} / [] and a log warning; it never takes a send down.
"""
import logging

from .matcher import load_profile_overlay

log = logging.getLogger("steel_intel.playbook")

_cache = None


def _build():
    """(action_map, stats) from the profile overlay. Never raises.

    The map holds both boost notes and watchlist titles; the watchlist half is
    unused by any renderer today and is here so that adding an `action` to a
    watchlist entry starts working without a code change (audience.py already
    guards it - the watchman is deliberately wider than the renderer).
    """
    mapping, groups, with_action, source = {}, 0, 0, "default"
    try:
        overlay, source = load_profile_overlay()
        order = 0
        for group in ((overlay.get("company_profile") or {}).get("boosts") or []):
            if not isinstance(group, dict):
                continue
            groups += 1
            note = (group.get("note") or "").strip()
            action = (group.get("action") or "").strip()
            if note and action:
                with_action += 1
                # score high -> low, ties keep profile order (see actions_for)
                try:
                    score = float(group.get("score") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                mapping.setdefault(note, (-score, order, action))
            order += 1
        for item in (overlay.get("watchlist") or []):
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            action = (item.get("action") or "").strip()
            if title and action:
                # No score of its own: a watchlist action sorts after every
                # boost action, in profile order.
                mapping.setdefault(title, (1.0, order, action))
            order += 1
    except Exception as exc:  # noqa: BLE001 - a playbook may never break a send
        log.warning("cannot build the action playbook from the profile: %s", exc)
        return {}, {"groups": 0, "with_action": 0, "source": "default"}
    return mapping, {"groups": groups, "with_action": with_action,
                     "source": source}


def action_map():
    """{note_or_title: (sort_score, sort_order, action)}. Cached in-process."""
    global _cache
    if _cache is None:
        _cache = _build()
    return _cache[0]


def stats():
    """{"groups", "with_action", "source"} for the CLI and the audience report."""
    global _cache
    if _cache is None:
        _cache = _build()
    return dict(_cache[1])


def reset_cache():
    """Forget the cached map (tests that swap the profile MUST call this).

    Pair it with audience.reset_cache(): a stale map here would attach the
    previous profile's instructions to the next profile's news.
    """
    global _cache
    _cache = None


def actions_for(notes, limit=None):
    """Actions for a row's impact_notes, most important first.

    Ordered by the boost score of the group that owns the note (high to low),
    ties broken by the order the groups appear in the profile - so the same row
    always renders the same list, whatever order storage handed the notes back.
    Duplicates are dropped. limit=None means no cap.
    """
    if not notes:
        return []
    if isinstance(notes, str):
        notes = [notes]
    try:
        mapping = action_map()
        if not mapping:
            return []
        found = []
        for note in notes:
            key = (note or "").strip() if isinstance(note, str) else ""
            hit = mapping.get(key)
            if hit and hit[2] not in [a for _s, _o, a in found]:
                found.append(hit)
        found.sort(key=lambda hit: (hit[0], hit[1]))
        out = [action for _score, _order, action in found]
    except Exception as exc:  # noqa: BLE001 - never break a send
        log.warning("cannot resolve actions for a story: %s", exc)
        return []
    if limit is not None:
        try:
            out = out[:max(0, int(limit))]
        except (TypeError, ValueError):
            pass
    return out


def has_action(notes):
    """True when at least one of `notes` resolves to an action."""
    return bool(actions_for(notes, 1))
