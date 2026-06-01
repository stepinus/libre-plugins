#!/usr/bin/env python3
"""LCM bootstrap hook — initialises the LCM database at engine startup.

Receives:  {"type": "bootstrap", "context_window_tokens": 200000, ...}
Responds:  {"type": "ok"}

Actions:
  1. Ensure the LCM database directory and file exist.
  2. Create tables if missing (messages, summaries, promoted_knowledge, messages_fts).
  3. Report FTS5 availability.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict


# ── config ───────────────────────────────────────────────────────────────────

DB_PATH = Path(
    os.environ.get("LFRANG_LCM_DB_PATH", "")
    or Path.home() / ".librefang" / "lcm-context" / "lcm.db"
)


# ── DB helpers ───────────────────────────────────────────────────────────────

def ensure_db() -> Dict[str, Any]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT    NOT NULL,
            role          TEXT    NOT NULL,
            content       TEXT    NOT NULL,
            message_hash  TEXT    NOT NULL,
            created_at    TEXT    NOT NULL,
            UNIQUE(session_id, message_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session
            ON messages(session_id, id);

        CREATE TABLE IF NOT EXISTS summaries (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id            TEXT    NOT NULL,
            parent_summary_id     INTEGER,
            depth                 INTEGER NOT NULL DEFAULT 0,
            covered_message_count INTEGER NOT NULL DEFAULT 0,
            summary_text          TEXT    NOT NULL,
            created_at            TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sum_session
            ON summaries(session_id, id);

        CREATE TABLE IF NOT EXISTS promoted_knowledge (
            id          TEXT PRIMARY KEY,
            session_id  TEXT,
            content     TEXT    NOT NULL,
            tags        TEXT    NOT NULL DEFAULT '[]',
            depth       INTEGER NOT NULL DEFAULT 0,
            confidence  REAL    NOT NULL DEFAULT 1.0,
            created_at  TEXT    NOT NULL
        );
    """)

    fts_ok = False
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, role, session_id UNINDEXED, message_id UNINDEXED);
            INSERT OR IGNORE INTO messages_fts(rowid, content, role, session_id, message_id)
            SELECT id, content, role, session_id, id FROM messages;
        """)
        fts_ok = True
    except sqlite3.Error:
        pass

    conn.close()
    return {"db_path": str(DB_PATH), "fts_enabled": fts_ok}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        if req.get("type") != "bootstrap":
            continue

        try:
            status = ensure_db()
            sys.stdout.write(
                json.dumps({"type": "ok", **status}, ensure_ascii=False) + "\n"
            )
        except Exception as exc:
            sys.stdout.write(
                json.dumps({"type": "error", "message": str(exc)}) + "\n"
            )

        sys.stdout.flush()


if __name__ == "__main__":
    main()
