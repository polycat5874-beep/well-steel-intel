# -*- coding: utf-8 -*-
"""Builds Thai alert/summary messages. Rule-based is the primary path;
if ANTHROPIC_API_KEY is set, a deep AI analysis paragraph is appended
(optional per team convention - absence never breaks anything)."""
import logging
import os
from datetime import date, datetime

log = logging.getLogger("steel_intel.summarizer")

THAI_MONTHS = [
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]
LEVEL_EMOJI = {"RED": "🔴", "ORANGE": "🟠", "YELLOW": "🟡", "GRAY": "⚪"}
LEVEL_LABEL = {"RED": "ต้องรู้วันนี้", "ORANGE": "เฝ้าระวัง", "YELLOW": "ทั่วไปที่เกี่ยวข้อง"}
# show at most this many items per level in the daily summary
LEVEL_SHOW_CAP = {"RED": 99, "ORANGE": 8, "YELLOW": 5}


def thai_date(d=None):
    d = d or date.today()
    return f"{d.day} {THAI_MONTHS[d.month - 1]} {d.year + 543}"


def build_critical_alert(item, analysis):
    """Urgent realtime alert message (Thai) for one news item."""
    lines = ["🚨 แจ้งเตือนด่วน — ข่าวสำคัญอุตสาหกรรมเหล็ก", ""]
    lines.append(f"📌 {item['title']}")
    src = item.get("source") or "-"
    lines.append(f"แหล่งข่าว: {src}")
    if analysis["critical_hits"]:
        lines.append("คำสำคัญที่พบ: " + ", ".join(analysis["critical_hits"][:6]))
    emoji = LEVEL_EMOJI.get(analysis["level"], "⚪")
    lines.append(f"ระดับผลกระทบ: {emoji} (คะแนน {analysis['score']})")
    for note in analysis["impact_notes"]:
        lines.append(f"⚠️ {note}")
    for w in analysis["watchlist_hits"]:
        lines.append(f"⏳ เกี่ยวข้องเรื่องที่เกาะติด: {w}")
    if item.get("url"):
        lines.append(item["url"])
    return "\n".join(lines)


def build_watchlist_block(watchlist):
    """Countdown block appended to every daily summary."""
    today = date.today()
    lines = ["⏳ เรื่องที่เกาะติด (Watchlist):"]
    for w in watchlist:
        if w.get("deadline"):
            dl = date.fromisoformat(w["deadline"])
            days = (dl - today).days
            when = thai_date(dl)
            if days >= 0:
                lines.append(f"• {w['title']} — เหลือ {days} วัน (ครบกำหนด {when})")
            else:
                lines.append(f"• {w['title']} — เลยกำหนดแล้ว {-days} วัน ({when}) ตรวจผลด่วน")
        else:
            lines.append(f"• {w['title']} — เฝ้าความเคลื่อนไหว")
        if w.get("note"):
            lines.append(f"   ({w['note']})")
    return "\n".join(lines)


def build_daily_summary(items, watchlist, round_label):
    """Daily summary message (Thai), grouped RED/ORANGE/YELLOW + watchlist."""
    header = f"📰 สรุปข่าวเหล็ก — รอบ{round_label} | {thai_date()}"
    if not items:
        body = "ไม่มีข่าวใหม่ที่เกี่ยวข้องในรอบนี้"
        return f"{header}\n\n{body}\n\n{build_watchlist_block(watchlist)}"

    groups = {"RED": [], "ORANGE": [], "YELLOW": []}
    for it in items:
        if it["level"] in groups:
            groups[it["level"]].append(it)

    parts = [header, f"ข่าวใหม่ที่เกี่ยวข้อง {len(items)} ชิ้น"]
    for level in ("RED", "ORANGE", "YELLOW"):
        rows = groups[level]
        if not rows:
            continue
        parts.append("")
        parts.append(f"{LEVEL_EMOJI[level]} {LEVEL_LABEL[level]} ({len(rows)})")
        for i, it in enumerate(rows[: LEVEL_SHOW_CAP[level]], 1):
            line = f"{i}. {it['title']}"
            if it.get("source"):
                line += f" [{it['source']}]"
            parts.append(line)
            for note in it.get("impact_notes", [])[:2]:
                parts.append(f"   ⚠️ {note}")
            if it.get("url"):
                parts.append(f"   {it['url']}")
        hidden = len(rows) - LEVEL_SHOW_CAP[level]
        if hidden > 0:
            parts.append(f"   ...และอีก {hidden} ชิ้น (ดูใน news.db)")

    parts.append("")
    parts.append(build_watchlist_block(watchlist))

    ai_text = ai_deep_summary(groups["RED"] + groups["ORANGE"])
    if ai_text:
        parts.append("")
        parts.append("🤖 บทวิเคราะห์ AI:")
        parts.append(ai_text)

    return "\n".join(parts)


def ai_deep_summary(items, max_items=10):
    """Optional Claude analysis of today's top items. Returns None when the
    key is missing or any error occurs (rule-based output stands alone)."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or not items:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        headlines = "\n".join(
            f"- {it['title']} (หัวข้อ: {', '.join(it.get('topics', []))})"
            for it in items[:max_items]
        )
        prompt = (
            "คุณเป็นนักวิเคราะห์อุตสาหกรรมเหล็กเส้นในประเทศไทย "
            " "
            "สรุปนัยสำคัญของพาดหัวข่าววันนี้ต่อบริษัท ใน 3-5 บรรทัด ภาษาไทย "
            "เน้นสิ่งที่ผู้บริหารควรทำ:\n" + headlines
        )
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:  # any failure -> silently fall back to rule-based
        log.warning("AI summary skipped: %s", exc)
        return None
