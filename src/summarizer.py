# -*- coding: utf-8 -*-
"""Builds Thai alert/summary messages with a premium, executive-friendly layout.

Rule-based is the primary path; if ANTHROPIC_API_KEY is set, a deep AI analysis
paragraph is appended to the daily summary (optional per team convention -
absence never breaks anything).

Visual hierarchy (per approved spec):
  * Critical alert -> single news card with clear header / date / source /
    bullet summary / company-impact / link, wrapped in heavy dividers.
  * Daily summary  -> items grouped by level (RED/ORANGE/YELLOW), then by topic
    tag, separated with light dividers + watchlist countdown.
"""
import logging
import os
from datetime import date, datetime

from src.sources.base import split_sentences

log = logging.getLogger("steel_intel.summarizer")

THAI_MONTHS = [
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]
LEVEL_EMOJI = {"RED": "🔴", "ORANGE": "🟠", "YELLOW": "🟡", "GRAY": "⚪"}
LEVEL_LABEL = {"RED": "ต้องรู้วันนี้", "ORANGE": "เฝ้าระวัง", "YELLOW": "ทั่วไปที่เกี่ยวข้อง"}
# show at most this many items per level in the daily summary
LEVEL_SHOW_CAP = {"RED": 99, "ORANGE": 8, "YELLOW": 5}

HEAVY_RULE = "━━━━━━━━━━━━━━━━━━"
LIGHT_RULE = "-------------------"


def thai_date(d=None):
    d = d or date.today()
    return f"{d.day} {THAI_MONTHS[d.month - 1]} {d.year + 543}"


# --- date/time display helpers -------------------------------------------

def _parse_iso(value):
    """Parse a stored ISO timestamp into a datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def fmt_datetime_full(item):
    """'DD/MM/YYYY - HH:MM น.' from published_datetime, falling back to the time
    the system fetched the item (clearly labelled, never fabricated)."""
    dt = _parse_iso(item.get("published_datetime"))
    if dt:
        return f"{dt:%d/%m/%Y - %H:%M} น."
    dt = _parse_iso(item.get("fetched_at"))
    if dt:
        return f"{dt:%d/%m/%Y - %H:%M} น. (เวลาที่ระบบพบข่าว)"
    return "ไม่ระบุเวลา"


def fmt_datetime_short(item):
    """'DD/MM HH:MM' compact form for the daily summary. '' if unknown."""
    dt = _parse_iso(item.get("published_datetime")) or _parse_iso(item.get("fetched_at"))
    return f"{dt:%d/%m %H:%M}" if dt else ""


def summary_bullets(item, max_points=2):
    """Split the stored lead summary into <= max_points short bullet strings."""
    text = (item.get("summary") or "").strip()
    if not text:
        return []
    sentences = split_sentences(text) or [text]
    return sentences[:max_points]


def _primary_topic(item):
    topics = item.get("topics") or []
    return topics[0] if topics else "อื่นๆ ที่เกี่ยวข้อง"


def _src(item):
    return item.get("source_name") or item.get("source") or "-"


# --- critical alert ------------------------------------------------------

def build_critical_alert(item, analysis):
    """Premium realtime alert card (Thai) for one news item. `item` and
    `analysis` may be the same dict (a DB row already carries both)."""
    emoji = LEVEL_EMOJI.get(analysis.get("level"), "⚪")
    lines = [
        "🚨 [CRITICAL ALERT]",
        item.get("title", "").strip(),
        "",
        HEAVY_RULE,
        "📅 วัน-เวลาที่ออกข่าว",
        f"   {fmt_datetime_full(item)}",
        "",
        "🌐 แหล่งที่มา",
        f"   {_src(item)}",
        "",
        "📝 สรุปเนื้อหาข่าว",
    ]
    bullets = summary_bullets(item)
    if bullets:
        lines += [f"   • {b}" for b in bullets]
    else:
        lines.append("   • (ดูรายละเอียดที่ลิงก์ข่าว)")

    lines += ["", "💥 ผลกระทบต่อบริษัท (Impact)",
              f"   {emoji} {analysis.get('level', '-')} · คะแนน {analysis.get('score', 0)}"]
    if analysis.get("critical_hits"):
        lines.append("   คำสำคัญที่พบ: " + ", ".join(analysis["critical_hits"][:6]))
    for note in analysis.get("impact_notes", []):
        lines.append(f"   • {note}")
    for w in analysis.get("watchlist_hits", []):
        lines.append(f"   ⏳ เกาะติด: {w}")

    if item.get("url"):
        lines += ["", "🔗 ลิงก์ข่าว", f"   {item['url']}"]
    lines.append(HEAVY_RULE)
    return "\n".join(lines)


# --- daily summary -------------------------------------------------------

def build_watchlist_block(watchlist):
    """Countdown block appended to every daily summary."""
    today = date.today()
    lines = ["⏳ เรื่องที่เกาะติด (Watchlist)"]
    for w in watchlist:
        if w.get("deadline"):
            dl = date.fromisoformat(w["deadline"])
            days = (dl - today).days
            when = thai_date(dl)
            if days >= 0:
                lines.append(f"• {w['title']}")
                lines.append(f"  — เหลือ {days} วัน (ครบกำหนด {when})")
            else:
                lines.append(f"• {w['title']}")
                lines.append(f"  — เลยกำหนดแล้ว {-days} วัน ({when}) ตรวจผลด่วน")
        else:
            lines.append(f"• {w['title']} — เฝ้าความเคลื่อนไหว")
        if w.get("note"):
            lines.append(f"   ({w['note']})")
    return "\n".join(lines)


def _render_level_block(level, rows):
    """One level section: header + items grouped by topic tag."""
    parts = ["", f"{LEVEL_EMOJI[level]} {LEVEL_LABEL[level]} ({len(rows)})"]
    shown = rows[: LEVEL_SHOW_CAP[level]]

    # group by primary topic, preserving first-seen order
    groups, order = {}, []
    for it in shown:
        topic = _primary_topic(it)
        if topic not in groups:
            groups[topic] = []
            order.append(topic)
        groups[topic].append(it)

    idx = 1
    for topic in order:
        parts.append(f"【 {topic} 】")
        for it in groups[topic]:
            parts.append(f"{idx}. {it.get('title', '').strip()}")
            meta = fmt_datetime_short(it)
            src = _src(it)
            meta_line = "   "
            if meta:
                meta_line += f"📅 {meta} · "
            meta_line += f"🌐 {src}"
            parts.append(meta_line)
            for b in summary_bullets(it, max_points=1):
                parts.append(f"   • {b}")
            for note in it.get("impact_notes", [])[:1]:
                parts.append(f"   ⚠️ {note}")
            if it.get("url"):
                parts.append(f"   🔗 {it['url']}")
            idx += 1

    hidden = len(rows) - len(shown)
    if hidden > 0:
        parts.append(f"   ...และอีก {hidden} ชิ้น (ดูใน news.db)")
    return parts


def build_system_alert(round_label, error):
    """Dead-man's switch: tell the reader the WATCHER is broken.

    From the reader's side a broken watcher and a quiet news day look identical -
    that is exactly how this system stayed silent for 16 days in Aug 2026. Only
    the daily-summary rounds send this (3x/day worst case); the realtime loop
    runs ~36x/day and would burn the whole LINE quota in under a week.
    """
    return "\n".join([
        "🚨 ระบบเฝ้าข่าวขัดข้อง — ยังไม่ได้เฝ้าข่าวให้",
        f"🗓 {thai_date()}  |  รอบ{round_label}",
        HEAVY_RULE,
        "",
        "ข้อความนี้แปลว่า ระบบพัง ไม่ใช่ ไม่มีข่าว",
        "",
        f"สาเหตุ: {str(error)[:300]}",
        "",
        "ตรวจ 3 จุดตามลำดับ:",
        "1) Supabase ถูก pause หรือไม่ (free tier พักโปรเจคเมื่อไม่มี query 7 วัน)",
        "2) GitHub Actions ถูกปิดจาก inactivity 60 วันหรือไม่",
        "3) GitHub secret DATABASE_URL ยังตรงกับโปรเจคปัจจุบันหรือไม่",
        HEAVY_RULE,
    ])


def build_daily_summary(items, watchlist, round_label, health=None):
    """Daily summary message (Thai), grouped RED/ORANGE/YELLOW -> topic + watchlist."""
    header = [
        f"📰 สรุปข่าวเหล็ก — รอบ{round_label}",
        f"🗓 {thai_date()}  |  ข่าวใหม่ {len(items)} ชิ้น",
        HEAVY_RULE,
    ]
    # Proof-of-life appended to the footer: a digest saying "no news" then
    # also proves the watcher ran, at no extra push cost.
    footer = [f"🩺 {health}"] if health else []
    if not items:
        body = ["", "ไม่มีข่าวใหม่ที่เกี่ยวข้องในรอบนี้", "", LIGHT_RULE, ""]
        return "\n".join(header + body + [build_watchlist_block(watchlist)]
                         + footer + [HEAVY_RULE])

    groups = {"RED": [], "ORANGE": [], "YELLOW": []}
    for it in items:
        if it.get("level") in groups:
            groups[it["level"]].append(it)

    parts = list(header)
    first = True
    for level in ("RED", "ORANGE", "YELLOW"):
        rows = groups[level]
        if not rows:
            continue
        if not first:
            parts.append(LIGHT_RULE)
        first = False
        parts += _render_level_block(level, rows)

    parts += ["", LIGHT_RULE, build_watchlist_block(watchlist)]

    ai_text = ai_deep_summary(groups["RED"] + groups["ORANGE"])
    if ai_text:
        parts += ["", LIGHT_RULE, "🤖 บทวิเคราะห์ AI:", ai_text]

    parts += footer
    parts.append(HEAVY_RULE)
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
