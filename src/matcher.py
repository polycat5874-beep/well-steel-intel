# -*- coding: utf-8 -*-
"""Keyword matching + topic tagging + company-impact scoring.

Scoring model (config: config/keywords.json):
  critical keyword hit  -> +3 each (capped at 3 hits)
  topic keyword hits    -> +1..2 per topic
  company profile boost -> +score per matched boost group (this is the
                           "[redacted]" innovation)
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


class Matcher:
    def __init__(self, config_path=CONFIG_PATH):
        with open(config_path, encoding="utf-8") as f:
            self.cfg = json.load(f)
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
