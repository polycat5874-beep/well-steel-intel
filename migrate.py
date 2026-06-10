# -*- coding: utf-8 -*-
"""One-shot helper to verify the cloud DB and create the schema.

Usage (after DATABASE_URL is set in .env or the environment):
    python migrate.py

It connects, ensures the tables exist (storage.connect creates them), prints a
short status, and exits non-zero on failure so it is safe to use in CI smoke
tests. Run it once after wiring up Supabase to confirm connectivity before the
first cron run.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from main import load_env  # noqa: E402  (reuse the same .env loader)
from src import storage  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_env()
    url = os.environ.get("DATABASE_URL", "")
    backend = "PostgreSQL (cloud)" if storage._is_pg_url(url) else "SQLite (local file)"
    print(f"backend  : {backend}")
    if storage._is_pg_url(url):
        # show host only, never the password
        try:
            host = url.split("@", 1)[1].split("/", 1)[0]
        except IndexError:
            host = "(unparsed)"
        print(f"host     : {host}")

    try:
        con = storage.connect()
    except Exception as exc:  # noqa: BLE001 - report any connection/setup error
        print(f"FAILED to connect / create schema: {exc}")
        return 1

    try:
        n_news = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        n_meta = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
        seeded = storage.get_meta(con, "baseline_seeded", "0")
        last_sum = storage.get_meta(con, "last_summary_at", "(never)")
    finally:
        con.close()

    print("schema   : OK (news + meta tables present)")
    print(f"news rows: {n_news}")
    print(f"meta rows: {n_meta}")
    print(f"baseline_seeded : {seeded}")
    print(f"last_summary_at : {last_sum}")
    print("\nReady. Next: `python main.py --once` to seed, then let cron take over.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
