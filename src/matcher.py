# -*- coding: utf-8 -*-
"""Keyword matching + topic tagging + company-impact scoring.

Scoring model (config: config/keywords.json):
  critical keyword hit  -> +3 each (capped at 3 hits)
  topic keyword hits    -> +1..2 per topic
  company profile boost -> +score per matched boost group (scores a story
                           against the operator's own risk profile)
  watchlist match       -> +4 (needs >= 2 keywords of that watchlist item)
Levels: RED >= 10, ORANGE >= 5, YELLOW >= 2, else GRAY.
"""
import json
import logging
import os
import re

log = logging.getLogger("steel_intel.matcher")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "keywords.json",
)


PROFILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "profile.json",
)
PROFILE_KEYS = ("company_profile", "watchlist")


def load_profile_overlay():
    """The operator's own risk profile, kept OUT of this repository.

    keywords.json ships a generic steel-industry profile because this repo is
    public - GitHub Actions only gives unlimited minutes to public repos, so
    anything committed here is world-readable. The real profile names the
    company, its plant location and which licences it operates under, which is
    exactly what must not sit in a public file.

    Source order: STEEL_INTEL_PROFILE_JSON (a JSON string, used on CI) then
    config/profile.json (gitignored, used locally). Returns (overlay, source).
    """
    raw = os.getenv("STEEL_INTEL_PROFILE_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw), "env"
        except ValueError as exc:
            log.warning("STEEL_INTEL_PROFILE_JSON is not valid JSON: %s", exc)
    if os.path.exists(PROFILE_PATH):
        try:
            with open(PROFILE_PATH, encoding="utf-8") as f:
                return json.load(f), "file"
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", PROFILE_PATH, exc)
    return {}, "default"


class Matcher:
    def __init__(self, config_path=CONFIG_PATH):
        with open(config_path, encoding="utf-8") as f:
            self.cfg = json.load(f)
        overlay, self.profile_source = load_profile_overlay()
        for key in PROFILE_KEYS:
            if key in overlay:
                self.cfg[key] = overlay[key]
        if self.profile_source == "default":
            # Loud, because a missing profile degrades scoring silently: every
            # story keeps its topic points but loses the company boosts, so
            # RED items quietly become ORANGE and stop firing alerts.
            log.warning("company profile not loaded - scoring with the generic "
                        "profile from keywords.json (set STEEL_INTEL_PROFILE_JSON)")
        else:
            log.info("company profile loaded from %s", self.profile_source)
        self.settings = self.cfg.get("settings", {})

    @staticmethod
    def _hit(keyword, text):
        """ASCII keywords match on word boundaries; short ALL-CAPS ones (AD,
        HRC) stay case-sensitive to avoid hits inside normal words. Thai
        keywords use plain substring (Thai has no word boundaries)."""
        if keyword.isascii():
            flags = 0 if (len(keyword) <= 3 and keyword.isupper()) else re.IGNORECASE
            pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
            return re.search(pattern, text, flags) is not None
        return keyword in text

    def analyze(self, item):
        text = f"{item.get('title', '')} {item.get('summary', '')}"

        critical = [k for k in self.cfg["critical_keywords"] if self._hit(k, text)]

        topics, topic_score = [], 0
        for topic in self.cfg["topics"].values():
            hits = [k for k in topic["keywords"] if self._hit(k, text)]
            if hits:
                topics.append(topic["name"])
                topic_score += min(len(hits), 2)

        impact_notes, boost = [], 0
        for grp in self.cfg["company_profile"]["boosts"]:
            if any(self._hit(k, text) for k in grp["keywords"]):
                boost += grp["score"]
                impact_notes.append(grp["note"])

        watchlist_hits = []
        for w in self.cfg["watchlist"]:
            n_hits = sum(1 for k in w["keywords"] if self._hit(k, text))
            if n_hits >= 2:  # require 2 keywords so a lone generic word can't trigger
                watchlist_hits.append(w["title"])
                boost += 4

        score = min(len(critical), 3) * 3 + topic_score + boost
        red = self.settings.get("score_red", 10)
        orange = self.settings.get("score_orange", 5)
        yellow = self.settings.get("score_yellow", 2)
        level = (
            "RED" if score >= red
            else "ORANGE" if score >= orange
            else "YELLOW" if score >= yellow
            else "GRAY"
        )

        return {
            "critical_hits": critical,
            "topics": topics,
            "score": score,
            "level": level,
            "impact_notes": impact_notes,
            "watchlist_hits": watchlist_hits,
            "is_relevant": bool(critical or topics or watchlist_hits),
        }
