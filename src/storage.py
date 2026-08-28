# -*- coding: utf-8 -*-
"""Storage layer for steel-intel.

Two interchangeable backends, selected at connect() time:

  * SQLite (default)   -> local file news.db. Used for local dev / always-on host.
  * PostgreSQL         -> when DATABASE_URL is set (e.g. Supabase). Used by the
                          GitHub Actions cron deployment where the runner disk is
                          ephemeral, so state (news + dedup + meta) must live in a
                          remote DB that survives between cron runs.

The SQL used by the rest of the module is intentionally written so it runs on
BOTH backends unchanged (``?`` placeholders, ``ON CONFLICT ... DO UPDATE``). The
Postgres wrapper below translates ``?`` -> ``%s`` and emulates the small slice of
the sqlite3 connection API this module relies on, so callers never branch.

Dedup key = sha256(url + title) per spec.

`story_key` is a SECOND, weaker key: a hash of the normalised headline (see
cluster.py). It never gates an insert - two outlets carrying one story are still
two rows - it only lets a reader/report find same-story rows cheaply. Like every
other late addition it is appended LAST everywhere (CREATE TABLE, _ADDED_COLUMNS,
ROW_COLS, INSERT_COLS, _row_values) so a freshly created table and a migrated one
zip against ROW_COLS identically.
"""
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime

from .cluster import story_key
from .sources.base import canonicalize_url

log = logging.getLogger("steel_intel.storage")

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news.db"
)

# --- SQLite schema (single script; sqlite executes multiple statements at once) ---
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hash            TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT,
    source          TEXT,
    published       TEXT,
    fetched_at      TEXT NOT NULL,
    topics          TEXT,
    critical_hits   TEXT,
    score           INTEGER DEFAULT 0,
    level           TEXT,
    impact_notes    TEXT,
    watchlist_hits  TEXT,
    alerted         INTEGER DEFAULT 0,
    published_datetime TEXT,
    source_name        TEXT,
    summary            TEXT,
    story_key          TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_fetched ON news(fetched_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# --- Postgres schema (same shape; SERIAL replaces AUTOINCREMENT; one stmt each) ---
PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS news (
        id              SERIAL PRIMARY KEY,
        hash            TEXT UNIQUE NOT NULL,
        title           TEXT NOT NULL,
        url             TEXT,
        source          TEXT,
        published       TEXT,
        fetched_at      TEXT NOT NULL,
        topics          TEXT,
        critical_hits   TEXT,
        score           INTEGER DEFAULT 0,
        level           TEXT,
        impact_notes    TEXT,
        watchlist_hits  TEXT,
        alerted         INTEGER DEFAULT 0,
        published_datetime TEXT,
        source_name        TEXT,
        summary            TEXT,
        story_key          TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_news_fetched ON news(fetched_at)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
]

# Columns added after the original schema shipped. They are appended (via ALTER
# ADD COLUMN) to any pre-existing table so deployed Supabase data is preserved.
# They are listed LAST in CREATE TABLE too, so a freshly-created table and a
# migrated one have the SAME column order -> `SELECT *` zips against ROW_COLS
# identically on both paths.
_ADDED_COLUMNS = ("published_datetime", "source_name", "summary", "story_key")

# Column order MUST match the table definition so `SELECT *` rows zip correctly
# on both backends.
ROW_COLS = (
    "id", "hash", "title", "url", "source", "published", "fetched_at",
    "topics", "critical_hits", "score", "level", "impact_notes",
    "watchlist_hits", "alerted", "published_datetime", "source_name", "summary",
    "story_key",
)


def _is_pg_url(url):
    return bool(url) and url.split(":", 1)[0] in ("postgres", "postgresql")


class _PgConn:
    """Minimal sqlite3-connection-compatible wrapper around a psycopg connection.

    Implements only what this module uses: execute / executemany / commit /
    rollback / close, plus ``?`` -> ``%s`` placeholder translation. execute()
    returns the underlying cursor, which already exposes fetchall()/fetchone().
    """

    def __init__(self, url):
        import psycopg  # lazy: only needed when a Postgres backend is selected

        self._psycopg = psycopg
        # prepare_threshold=None disables server-side prepared statements, which
        # keeps us compatible with pgbouncer transaction-mode poolers (Supabase).
        self.con = psycopg.connect(url, prepare_threshold=None)
        for stmt in PG_SCHEMA:
            with self.con.cursor() as cur:
                cur.execute(stmt)
        # Migrate pre-existing tables: add new columns if missing (idempotent).
        for col in _ADDED_COLUMNS:
            with self.con.cursor() as cur:
                cur.execute(f"ALTER TABLE news ADD COLUMN IF NOT EXISTS {col} TEXT")
        self.con.commit()

    @staticmethod
    def _q(sql):
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        cur = self.con.cursor()
        cur.execute(self._q(sql), params)
        return cur

    def executemany(self, sql, seq):
        cur = self.con.cursor()
        cur.executemany(self._q(sql), list(seq))
        return cur

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()

    def close(self):
        self.con.close()


def _dup_errors():
    """Errors that mean 'this row already exists' (dedup hit), per backend."""
    errs = [sqlite3.IntegrityError]
    try:
        import psycopg
        errs.append(psycopg.errors.UniqueViolation)
    except ImportError:
        pass
    return tuple(errs)


_DUP_ERRORS = _dup_errors()


def connect(db_path=DB_PATH):
    """Open a connection. Uses Postgres when DATABASE_URL is a postgres URL,
    otherwise SQLite at db_path. Schema is ensured on connect for both."""
    url = os.environ.get("DATABASE_URL")
    if _is_pg_url(url):
        return _PgConn(url)
    con = sqlite3.connect(db_path)
    con.executescript(SQLITE_SCHEMA)
    # Migrate pre-existing tables: SQLite has no ADD COLUMN IF NOT EXISTS, so
    # check PRAGMA first. New columns append at the end -> matches ROW_COLS.
    existing = {row[1] for row in con.execute("PRAGMA table_info(news)").fetchall()}
    for col in _ADDED_COLUMNS:
        if col not in existing:
            con.execute(f"ALTER TABLE news ADD COLUMN {col} TEXT")
    con.commit()
    return con


def item_hash(item):
    # Canonicalise the URL first: the same article shared with different
    # tracking tokens (?utm_source, ?fbclid, ...) must hash identically, or it
    # bypasses the UNIQUE(hash) dedup and re-appears as "new".
    url = canonicalize_url(item.get("url") or "")
    raw = url + "|" + (item.get("title") or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


INSERT_COLS = (
    "hash", "title", "url", "source", "published", "fetched_at",
    "topics", "critical_hits", "score", "level", "impact_notes", "watchlist_hits",
    "published_datetime", "source_name", "summary", "story_key",
)


def _row_values(item, analysis, now):
    return (
        item_hash(item),
        item.get("title", ""),
        item.get("url", ""),
        item.get("source", ""),
        item.get("published", ""),
        now,
        json.dumps(analysis["topics"], ensure_ascii=False),
        json.dumps(analysis["critical_hits"], ensure_ascii=False),
        analysis["score"],
        analysis["level"],
        json.dumps(analysis["impact_notes"], ensure_ascii=False),
        json.dumps(analysis["watchlist_hits"], ensure_ascii=False),
        item.get("published_datetime", ""),
        item.get("source_name") or item.get("source", ""),
        item.get("summary", ""),
        story_key(item.get("title", "")),
    )


def insert_many(con, pairs, chunk=100):
    """Bulk-insert analyzed items, skipping duplicates by hash.

    `pairs` is an iterable of (item, analysis). Returns the count of NEWLY
    inserted rows. This batches the work into a handful of multi-row INSERTs
    (instead of one round-trip per item), which is essential when the DB is
    remote: a full re-fetch is mostly duplicates, and per-row INSERT+rollback
    over a high-latency link (GitHub runner -> Supabase) is ~2000 round-trips
    and times out. ON CONFLICT(hash) DO NOTHING skips dups server-side; the
    RETURNING clause counts only the rows actually inserted. Works on both
    SQLite (>=3.35) and Postgres. chunk caps placeholders per statement to stay
    under driver/SQLite variable limits."""
    now = datetime.now().isoformat(timespec="seconds")
    seen = set()
    rows = []
    for item, analysis in pairs:
        vals = _row_values(item, analysis, now)
        h = vals[0]
        if h in seen:  # same headline fetched from two sources in one cycle
            continue
        seen.add(h)
        rows.append(vals)
    if not rows:
        return 0

    cols = ", ".join(INSERT_COLS)
    one = "(" + ",".join(["?"] * len(INSERT_COLS)) + ")"
    new_count = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        placeholders = ",".join([one] * len(batch))
        flat = [v for r in batch for v in r]
        cur = con.execute(
            f"INSERT INTO news ({cols}) VALUES {placeholders}"
            " ON CONFLICT(hash) DO NOTHING RETURNING id",
            flat,
        )
        new_count += len(cur.fetchall())
    con.commit()
    return new_count


def insert_if_new(con, item, analysis):
    """Insert one analyzed item. Returns True if new, False if duplicate.
    (Kept for single-item callers; collect cycles use insert_many for speed.)"""
    try:
        now = datetime.now().isoformat(timespec="seconds")
        cols = ", ".join(INSERT_COLS)
        ph = ",".join(["?"] * len(INSERT_COLS))
        con.execute(
            f"INSERT INTO news ({cols}) VALUES ({ph})",
            _row_values(item, analysis, now),
        )
        con.commit()
        return True
    except _DUP_ERRORS:
        # Postgres aborts the transaction on a constraint violation, so it must
        # be rolled back before the connection can be reused. (no-op for sqlite.)
        con.rollback()
        return False


def _row_to_dict(row):
    d = dict(zip(ROW_COLS, row))
    for key in ("topics", "critical_hits", "impact_notes", "watchlist_hits"):
        try:
            d[key] = json.loads(d[key] or "[]")
        except (TypeError, ValueError):
            d[key] = []
    return d


def get_unalerted_critical(con, priority_keywords=None):
    """Rows never alerted that warrant an instant '🚨 CRITICAL ALERT'.

    Two ways in:
      1. RED/ORANGE with any critical keyword - high measured impact.
      2. YELLOW whose critical keywords include a *priority* one (TIS standards,
         IF furnaces, anti-dumping). These decide whether this company can keep
         producing at all, so a middling score must not bury them in the digest.

    Every other YELLOW stays in the daily summary, which keeps realtime alerts
    genuinely critical and conserves the LINE push quota.
    """
    rows = con.execute(
        "SELECT * FROM news WHERE alerted = 0 AND critical_hits != '[]'"
        " AND level IN ('RED', 'ORANGE', 'YELLOW')"
        " ORDER BY score DESC, id DESC"
    ).fetchall()
    priority = [k.lower() for k in (priority_keywords or [])]
    out = []
    for r in rows:
        row = _row_to_dict(r)
        if row.get("level") in ("RED", "ORANGE"):
            out.append(row)
            continue
        # _row_to_dict has already decoded critical_hits into a list.
        hits = row.get("critical_hits") or []
        if any(str(h).lower() in priority for h in hits):
            out.append(row)
    return out


def mark_alerted(con, ids):
    if not ids:
        return
    con.executemany("UPDATE news SET alerted = 1 WHERE id = ?", [(i,) for i in ids])
    con.commit()


def backfill_story_keys(con, chunk=200):
    """Fill story_key for rows inserted before the column existed.

    Returns the number of rows updated. Chunked so one statement never carries
    more parameters than SQLite/psycopg will accept, and so a large table does
    not travel to a remote DB in a single enormous round-trip."""
    rows = con.execute(
        "SELECT id, title FROM news WHERE story_key IS NULL OR story_key = ''"
    ).fetchall()
    if not rows:
        return 0
    updated = 0
    for i in range(0, len(rows), chunk):
        batch = [(story_key(title or ""), row_id) for row_id, title in rows[i:i + chunk]]
        con.executemany("UPDATE news SET story_key = ? WHERE id = ?", batch)
        updated += len(batch)
    con.commit()
    return updated


def ensure_story_keys(con):
    """One-time backfill, guarded by meta['story_key_backfilled'].

    Deliberately NOT called from connect(): connect() runs every 15 minutes and
    a full-table scan across the network on every cycle would spend the very
    latency budget the bulk-insert work bought back. NEVER raises - it runs on
    the same path as the dead-man's switch."""
    try:
        if get_meta(con, "story_key_backfilled") == "1":
            return 0
        n = backfill_story_keys(con)
        set_meta(con, "story_key_backfilled", "1")
        if n:
            log.info("story_key backfilled for %d pre-existing rows", n)
        return n
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not break alerts
        log.warning("story_key backfill skipped: %s", exc)
        # Postgres aborts the transaction on a failed statement; roll back so the
        # connection stays usable for the alert that follows.
        try:
            con.rollback()
        except Exception:
            pass
        return 0


def get_since(con, iso_ts):
    rows = con.execute(
        "SELECT * FROM news WHERE fetched_at > ? ORDER BY score DESC, id DESC",
        (iso_ts,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    con.commit()
