#!/usr/bin/env python3
"""LCM ingest hook — recalls relevant memories before each agent turn.

Receives:  {"type": "ingest", "agent_id": "...", "session_id": "...", "message": "..."}
Responds:  {"type": "ingest_result", "memories": [{"content": "..."}, ...]}

Searches:
  1. Current session messages (FTS5) — related past turns
  2. Cross-session messages — relevant content from other conversations
  3. Promoted knowledge — curated reusable insights
  4. Recent summary DAG nodes — conversation continuity markers
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── config ───────────────────────────────────────────────────────────────────

DB_PATH = Path(
    os.environ.get("LFRANG_LCM_DB_PATH", "")
    or Path.home() / ".librefang" / "lcm-context" / "lcm.db"
)
RECALL_LIMIT = int(os.environ.get("LFRANG_LCM_RECALL_LIMIT", "5"))


# ── minimal DB (read-only queries, shared with sidecar) ──────────────────────

class ReadDB:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def search(self, query: str, session_id: str, limit: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT m.id, m.session_id, m.role, m.content, m.created_at
                    FROM messages_fts f
                    JOIN messages m ON m.id = f.rowid
                    WHERE f.content MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (" ".join(f'"{w}"' for w in query.split() if w) or query, limit),
                ).fetchall()
            except sqlite3.Error:
                rows = conn.execute(
                    "SELECT id, session_id, role, content, created_at FROM messages "
                    "WHERE session_id = ? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                    (session_id, f"%{query}%", limit),
                ).fetchall()

        return [
            {"session_id": r["session_id"], "role": r["role"],
             "snippet": r["content"][:300], "created_at": r["created_at"]}
            for r in rows
        ]

    def search_cross(self, query: str, exclude_session: str, limit: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, session_id, role, content, created_at FROM messages "
                "WHERE session_id != ? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (exclude_session, f"%{query}%", limit),
            ).fetchall()
        return [
            {"session_id": r["session_id"], "role": r["role"],
             "snippet": r["content"][:300]} for r in rows
        ]

    def search_promoted(self, query: str, limit: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT content, tags, session_id FROM promoted_knowledge "
                "WHERE content LIKE ? ORDER BY confidence DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [{"content": r["content"], "tags": r["tags"]} for r in rows]

    def recent_summaries(self, session_id: str, limit: int = 2) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT summary_text FROM summaries WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [r["summary_text"] for r in rows]


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

        if req.get("type") != "ingest":
            continue

        message = str(req.get("message", "")).strip()
        session_id = str(req.get("agent_id") or req.get("session_id") or "unknown")

        memories: List[Dict[str, Any]] = []

        if message and DB_PATH.exists():
            try:
                db = ReadDB(DB_PATH)

                # 1. Current session FTS search
                hits = db.search(message, session_id, RECALL_LIMIT)
                for h in hits:
                    memories.append({"content": f"[LCM memory] {h['role']}: {h['snippet']}"})

                # 2. Cross-session search
                cross = db.search_cross(message, session_id, max(2, RECALL_LIMIT // 2))
                for h in cross:
                    memories.append({"content": f"[LCM cross-session] {h['role']}: {h['snippet']}"})

                # 3. Promoted knowledge
                promoted = db.search_promoted(message, max(2, RECALL_LIMIT // 3))
                for p in promoted:
                    memories.append({"content": f"[LCM knowledge] {p['content']}"})

                # 4. Recent summaries
                summaries = db.recent_summaries(session_id, 2)
                for s in summaries:
                    memories.append({"content": f"[LCM summary] {s}"})

            except Exception:
                pass  # best-effort; never break a turn on DB errors

        sys.stdout.write(json.dumps({"type": "ingest_result", "memories": memories}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
