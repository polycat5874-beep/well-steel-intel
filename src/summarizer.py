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

AUDIENCE
--------
Every builder takes audience="full" (default, byte-for-byte the layout that
shipped) or audience="public". The public variant is what goes out on the OA
broadcast: same news, minus this operator's own reading of it - no impact notes,
no score, no matched keywords, no watchlist. It is the SECOND of the three
layers described in src/audience.py; the first is that a public builder is
handed a row already projected down to the publicly showable fields, so an
internal field it forgets to skip is not in the dict to begin with.
"""
import logging
import os
from datetime import date, datetime

from src.audience import classify_error, mask_error
from src.cluster import normalize_title
from src.matcher import load_profile_overlay
from src.playbook import actions_for
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

# The playbook block (src/playbook.py): what to DO about a story, printed only
# on the private/full version because an action names this operator's licences.
# It is APPENDED, never substituted - a story with no action renders exactly the
# bytes it rendered before this feature existed.
ACTION_HEAD = "🎯 สิ่งที่ต้องทำต่อ"
ACTION_MAX_CARD = 3        # an alert card can carry a short numbered list
ACTION_MAX_DIGEST = 1      # the digest has one line per story, so one action


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


def _also_reported_lines(item, indent="   ", max_title=70):
    """Thai block listing the other outlets that carried the SAME story.

    THE NO-HIDING RULE. Clustering (cluster.py) merges rows that look like one
    story, and its worst failure mode is a wrong merge that makes real news
    disappear without a trace. So this block always names EVERY outlet in the
    group, and additionally prints that outlet's own headline whenever the
    headline differs (after normalisation) from the leader's. A bad merge then
    shows up as two visibly different headlines inside one card instead of a
    silently missing story.

    Returns [] when the item carries no merged members, so a plain dict (e.g.
    from test_alert.py) renders exactly as it always did."""
    others = item.get("also_reported") or []
    if not others:
        return []
    lines = [f"📰 อีก {len(others)} สำนักรายงานเรื่องเดียวกัน"]
    leader_norm = normalize_title(item.get("title"))
    for other in others:
        name = _src(other)
        title = (other.get("title") or "").strip()
        if title and normalize_title(title) != leader_norm:
            shown = title if len(title) <= max_title else title[:max_title - 1] + "…"
            lines.append(f"{indent}• {name} — {shown}")
        else:
            lines.append(f"{indent}• {name}")
    return lines


# --- critical alert ------------------------------------------------------

def build_critical_alert(item, analysis, index=None, total=None, audience="full"):
    """Premium realtime alert card (Thai) for one news item. `item` and
    `analysis` may be the same dict (a DB row already carries both).

    `index`/`total` number the card inside a batch ("2/8") so a reader can tell
    at a glance that several cards arrived in ONE push. Both omitted -> the
    output is byte-for-byte the original single-card layout.

    audience="public" swaps the whole company-impact block for a plain
    importance line. Everything a reader needs to act on the NEWS - headline,
    date, source, the "also reported by" block, the summary and the link - is
    identical in both versions; only the operator's own reading is withheld."""
    emoji = LEVEL_EMOJI.get(analysis.get("level"), "⚪")
    header = ("🚨 [CRITICAL ALERT]" if index is None or total is None
              else f"🚨 [CRITICAL ALERT {index}/{total}]")
    lines = [
        header,
        item.get("title", "").strip(),
        "",
        HEAVY_RULE,
        "📅 วัน-เวลาที่ออกข่าว",
        f"   {fmt_datetime_full(item)}",
        "",
        "🌐 แหล่งที่มา",
        f"   {_src(item)}",
        "",
    ]
    also = _also_reported_lines(item)
    if also:                       # omitted entirely when nothing was merged,
        lines += also + [""]       # so an un-clustered card is byte-identical
    lines.append("📝 สรุปเนื้อหาข่าว")
    bullets = summary_bullets(item)
    if bullets:
        lines += [f"   • {b}" for b in bullets]
    else:
        lines.append("   • (ดูรายละเอียดที่ลิงก์ข่าว)")

    if audience == "public":
        # LEVEL_LABEL has no "GRAY" key - .get keeps an unexpected level from
        # taking the whole broadcast down with a KeyError.
        lines += ["", "🔺 ระดับความสำคัญ",
                  f"   {emoji} {LEVEL_LABEL.get(analysis.get('level'), '-')}"]
    else:
        lines += ["", "💥 ผลกระทบต่อบริษัท (Impact)",
                  f"   {emoji} {analysis.get('level', '-')} · คะแนน {analysis.get('score', 0)}"]
        if analysis.get("critical_hits"):
            lines.append("   คำสำคัญที่พบ: " + ", ".join(analysis["critical_hits"][:6]))
        for note in analysis.get("impact_notes", []):
            lines.append(f"   • {note}")
        for w in analysis.get("watchlist_hits", []):
            lines.append(f"   ⏳ เกาะติด: {w}")
        acts = actions_for(analysis.get("impact_notes"), ACTION_MAX_CARD)
        if acts:
            lines += ["", ACTION_HEAD]
            lines += [f"   {n}. {a}" for n, a in enumerate(acts, 1)]

    if item.get("url"):
        lines += ["", "🔗 ลิงก์ข่าว", f"   {item['url']}"]
    lines.append(HEAVY_RULE)
    return "\n".join(lines)


def build_alert_batch_header(n_total, n_detailed):
    """Opening block of a batched realtime alert.

    All the cards of one cycle travel in a SINGLE LINE request (see
    notifier.plan_requests), so the reader needs to know up front how many
    stories this push carries."""
    lines = [
        f"🚨 แจ้งเตือนด่วน — ข่าวสำคัญ {n_total} ชิ้น",
        f"🗓 {thai_date()}",
    ]
    if n_detailed < n_total:
        lines.append(f"(กางรายละเอียด {n_detailed} ชิ้นแรก · ที่เหลือแสดงเฉพาะพาดหัว)")
    lines.append(HEAVY_RULE)
    return "\n".join(lines)


def build_extra_headlines(rows, limit=20):
    """Headline-only tail block for items beyond alert_max_per_cycle."""
    if not rows:
        return ""
    lines = [f"🚨 ข่าวสำคัญเพิ่มเติมอีก {len(rows)} ชิ้นในรอบนี้:"]
    for r in rows[:limit]:
        extra = r.get("also_reported") or []
        # Even in the headline-only tail the reader is told the item stands for
        # several outlets, so a collapsed group never looks like a single report.
        suffix = f" (+{len(extra)} สำนัก)" if extra else ""
        lines.append(f"• {r['title']}{suffix}")
    return "\n".join(lines)


def build_quota_warning(status):
    """Short Thai notice appended to a digest when the monthly LINE quota is
    running out. Appended to an existing message - never pushed on its own,
    because the warning itself would consume the quota it is warning about."""
    return "\n".join([
        f"⚠️ โควต้า LINE เดือน {status['month']} ใช้ไปแล้ว "
        f"{status['used']}/{status['limit']} ข้อความ "
        f"({status['ratio'] * 100:.0f}%) เหลือ {status['left']} ข้อความ",
        "   ระบบจะจำกัดการแจ้งเตือนด่วนอัตโนมัติ (เก็บโควต้าไว้ให้สรุป 3 รอบ/วัน)",
        "   ถ้าต้องการเตือนครบทุกชิ้น ให้เพิ่มแพ็กเกจ LINE หรือลดจำนวนผู้รับ",
    ])


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


def _render_level_block(level, rows, audience="full"):
    """One level section: header + items grouped by topic tag.

    audience="public" drops the "⚠️ impact note" line (the operator's own
    reading); every headline, source, outlet list and link still shows."""
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
            # No-hiding rule (see _also_reported_lines): every outlet in the
            # merged group is named, and any member whose headline differs from
            # the leader's gets its headline printed too. Deliberately NOT
            # capped - a truncated member list is exactly the silent loss this
            # feature must not create.
            others = it.get("also_reported") or []
            if others:
                parts.append(f"   📰 อีก {len(others)} สำนัก: "
                             + ", ".join(_src(o) for o in others))
                leader_norm = normalize_title(it.get("title"))
                for other in others:
                    other_title = (other.get("title") or "").strip()
                    if other_title and normalize_title(other_title) != leader_norm:
                        parts.append(f"   ↳ [{_src(other)}] {other_title}")
            for b in summary_bullets(it, max_points=1):
                parts.append(f"   • {b}")
            if audience != "public":
                for note in it.get("impact_notes", [])[:1]:
                    parts.append(f"   ⚠️ {note}")
                for a in actions_for(it.get("impact_notes"), ACTION_MAX_DIGEST):
                    parts.append(f"   🎯 ทำต่อ: {a}")
            if it.get("url"):
                parts.append(f"   🔗 {it['url']}")
            idx += 1

    hidden = len(rows) - len(shown)
    if hidden > 0:
        parts.append(f"   ...และอีก {hidden} ชิ้น (ดูใน news.db)")
    return parts


# Emitted when even the audience machinery fails while the dead-man's switch is
# firing. It still carries the two lines that make the switch worth having, and
# nothing that could conceivably be sensitive.
SYSTEM_ALERT_PUBLIC_FALLBACK = "\n".join([
    "🚨 ระบบเฝ้าข่าวขัดข้อง — ยังไม่ได้เฝ้าข่าวให้",
    HEAVY_RULE,
    "",
    "ข้อความนี้แปลว่า ระบบพัง ไม่ใช่ ไม่มีข่าว",
    "",
    "โปรดแจ้งผู้ดูแลระบบให้ตรวจสอบโดยเร็ว",
    HEAVY_RULE,
])


def build_system_alert(round_label, error, audience="full"):
    """Dead-man's switch: tell the reader the WATCHER is broken.

    From the reader's side a broken watcher and a quiet news day look identical -
    that is exactly how this system stayed silent for 16 days in Aug 2026. Only
    the daily-summary rounds send this (3x/day worst case); the realtime loop
    runs ~36x/day and would burn the whole LINE quota in under a week.

    BOTH audiences keep the two lines the switch exists for - the alarm headline
    and "this means the system is broken, not that there is no news". The public
    version drops only the DIAGNOSIS: the raw error and the operator checklist
    name infrastructure, so the team gets a category instead.
    """
    head = [
        "🚨 ระบบเฝ้าข่าวขัดข้อง — ยังไม่ได้เฝ้าข่าวให้",
        f"🗓 {thai_date()}  |  รอบ{round_label}",
        HEAVY_RULE,
        "",
        "ข้อความนี้แปลว่า ระบบพัง ไม่ใช่ ไม่มีข่าว",
        "",
    ]
    if audience == "public":
        return "\n".join(head + [
            f"ประเภทปัญหา: {classify_error(error)}",
            "",
            "ผู้ดูแลระบบได้รับรายละเอียดฉบับเต็มแล้ว",
            HEAVY_RULE,
        ])
    return "\n".join(head + [
        # mask_error, never the raw string: a DSN or an access token inside an
        # exception would otherwise be pasted straight into a chat window.
        f"สาเหตุ: {mask_error(str(error))[:300]}",
        "",
        "ตรวจ 3 จุดตามลำดับ:",
        "1) Supabase ถูก pause หรือไม่ (free tier พักโปรเจคเมื่อไม่มี query 7 วัน)",
        "2) GitHub Actions ถูกปิดจาก inactivity 60 วันหรือไม่",
        "3) GitHub secret DATABASE_URL ยังตรงกับโปรเจคปัจจุบันหรือไม่",
        HEAVY_RULE,
    ])


def build_daily_summary(items, watchlist, round_label, health=None, n_rows=None,
                        audience="full", due_block=None):
    """Daily summary message (Thai), grouped RED/ORANGE/YELLOW -> topic + watchlist.

    `n_rows` is how many DATABASE ROWS the `items` stand for once same-story rows
    have been merged (see cluster.group_stories). Left as None - or equal to
    len(items) - the header keeps its original wording exactly; otherwise it
    states both numbers, so a shrinking headline count reads as "merged", never
    as "the watcher found less news".

    audience="public" omits the watchlist block entirely (a list of what this
    company is afraid of, and dated - it would be the single most revealing
    thing in the message) and the AI analysis paragraph, which is written from
    the company's point of view. The header, the news itself and the "🩺" proof
    of life stay, so a broadcast still proves the watcher ran.

    `due_block` is the watchlist deadline nudge (src/reminder.py), already
    rendered by the caller because it needs a database connection for its
    once-a-day bookkeeping. It is appended right after the watchlist countdown
    it refers to, and ONLY on the full version - it names watchlist titles, the
    same reason the countdown itself is private. An empty/None value appends
    NOTHING, so a digest with no due deadline is byte-for-byte the digest that
    shipped before this feature existed."""
    count = f"ข่าวใหม่ {len(items)} ชิ้น"
    if n_rows is not None and n_rows != len(items):
        count = f"ข่าวใหม่ {len(items)} เรื่อง (จาก {n_rows} ชิ้น)"
    header = [
        f"📰 สรุปข่าวเหล็ก — รอบ{round_label}",
        f"🗓 {thai_date()}  |  {count}",
        HEAVY_RULE,
    ]
    # Proof-of-life appended to the footer: a digest saying "no news" then
    # also proves the watcher ran, at no extra push cost.
    footer = [f"🩺 {health}"] if health else []
    is_public = audience == "public"
    # Layer 2 of the audience split (src/audience.py): the renderer simply does
    # not emit the block for a public reader, whatever the caller passed.
    due = "" if is_public else (due_block or "").strip()
    if not items:
        body = ["", "ไม่มีข่าวใหม่ที่เกี่ยวข้องในรอบนี้", "", LIGHT_RULE, ""]
        # The quiet-day digest hides the watchlist too: "no news" is exactly the
        # message where the watchlist would be the only content worth reading.
        tail = [] if is_public else [build_watchlist_block(watchlist)]
        if due:
            tail = tail + ["", LIGHT_RULE, due]
        return "\n".join(header + body + tail + footer + [HEAVY_RULE])

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
        parts += _render_level_block(level, rows, audience=audience)

    if not is_public:
        parts += ["", LIGHT_RULE, build_watchlist_block(watchlist)]
        # Directly after the countdown it belongs to, and before the optional AI
        # paragraph: an overdue deadline is the actionable half of the watchlist
        # and must not end up underneath an essay.
        if due:
            parts += ["", LIGHT_RULE, due]
        ai_text = ai_deep_summary(groups["RED"] + groups["ORANGE"])
        if ai_text:
            parts += ["", LIGHT_RULE, "🤖 บทวิเคราะห์ AI:", ai_text]

    parts += footer
    parts.append(HEAVY_RULE)
    return "\n".join(parts)


# Neutral by design. The persona that names the company, its furnace type and
# its raw-material mix is operator data and lives in the profile overlay
# (config/profile.json / STEEL_INTEL_PROFILE_JSON), never in this public repo.
DEFAULT_AI_PERSONA = "คุณเป็นนักวิเคราะห์อุตสาหกรรมเหล็กเส้นในประเทศไทย"


def _ai_persona():
    """The analyst persona for the AI paragraph, from the profile overlay.

    Read straight off load_profile_overlay() rather than through
    matcher.PROFILE_KEYS on purpose: adding a key there would feed it into
    scoring, and this string must not move a single score."""
    try:
        overlay, _source = load_profile_overlay()
        persona = (overlay.get("ai_persona") or "").strip()
        if persona:
            return persona
    except Exception as exc:  # noqa: BLE001 - the AI paragraph is optional
        log.warning("cannot read ai_persona from the profile: %s", exc)
    return DEFAULT_AI_PERSONA


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
            _ai_persona() + " "
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
