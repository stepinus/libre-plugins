#!/usr/bin/env python3
"""LCM after_turn hook — persists the full turn transcript to SQLite.

Receives:  {"type": "after_turn", "agent_id": "...", "session_id": "...", "messages": [...]}
Responds:  {"type": "ok"}

Actions:
  1. Persist all messages to the LCM database (dedup by content hash)
  2. If promoted/notable content detected, record as promoted_knowledge
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


# ── config ───────────────────────────────────────────────────────────────────

DB_PATH = Path(
    os.environ.get("LFRANG_LCM_DB_PATH", "")
    or Path.home() / ".librefang" / "lcm-context" / "lcm.db"
)


# ── DB helpers ───────────────────────────────────────────────────────────────

def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def message_hash(role: str, content: str) -> str:
    h = hashlib.sha256()
    h.update(role.encode("utf-8", errors="ignore"))
    h.update(b"\0")
    h.update(content.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def extract_text(msg: Dict[str, Any]) -> str:
    c = msg.get("content", "")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = []
        for item in c:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts)
    return ""


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

        if req.get("type") != "after_turn":
            continue

        agent_id = str(req.get("agent_id") or "unknown")
        messages: List[Dict[str, Any]] = req.get("messages", [])

        if not messages:
            sys.stdout.write(json.dumps({"type": "ok"}) + "\n")
            sys.stdout.flush()
            continue

        # Ensure DB exists
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        try:
            with connect_db() as conn:
                # Ensure tables (lightweight — CREATE IF NOT EXISTS)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        message_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, message_hash)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS promoted_knowledge (
                        id TEXT PRIMARY KEY,
                        session_id TEXT,
                        content TEXT NOT NULL,
                        tags TEXT NOT NULL DEFAULT '[]',
                        depth INTEGER NOT NULL DEFAULT 0,
                        confidence REAL NOT NULL DEFAULT 1.0,
                        created_at TEXT NOT NULL
                    )
                """)

                now = datetime.now(timezone.utc).isoformat()
                saved = 0

                for m in messages:
                    role = str(m.get("role", "unknown"))
                    content = extract_text(m)
                    if not content:
                        continue
                    mh = message_hash(role, content)

                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO messages(session_id, role, content, message_hash, created_at) "
                            "VALUES(?,?,?,?,?)",
                            (agent_id, role, content, mh, now),
                        )
                        if conn.total_changes > 0:
                            saved += 1
                    except sqlite3.Error:
                        continue

            sys.stdout.write(json.dumps({
                "type": "ok",
                "persisted": saved,
                "session_id": agent_id,
            }) + "\n")

        except Exception as exc:
            # Never break the agent loop for a persistence error
            sys.stdout.write(json.dumps({
                "type": "ok",
                "persisted": 0,
                "error": str(exc),
            }) + "\n")

        sys.stdout.flush()


if __name__ == "__main__":
    main()
