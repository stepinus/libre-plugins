#!/usr/bin/env python3
"""LibreFang LCM sidecar context engine.

A context-engine sidecar that delegates memory persistence and recall to a
local SQLite database (LCM-compatible schema, no daemon dependency).

Speaks the newline-delimited JSON request/reply protocol:
  stdin:  {"id": <u64>, "method": "<name>", "params": {…}}
  stdout: {"id": <u64>, "ok": {…}}  |  {"id": <u64>, "error": "<msg>"}

Methods (map to LCM hooks per claude-plugin_example pattern):
  bootstrap  – SessionStart:   init DB, ensure tables + FTS5
  ingest     – UserPromptSubmit: recall related memories from past conversations
  assemble   – PreCompact:     window management (trim/reorder), no LLM compaction
  after_turn – SessionEnd:     persist turn transcript to messages table

Configuration (env vars):
  LFRANG_LCM_DB_PATH         – database path (default: ~/.librefang/lcm-context/lcm.db)
  LFRANG_LCM_HEAD_KEEP       – messages kept at window start (default: 2)
  LFRANG_LCM_TAIL_KEEP       – messages kept at window end   (default: 16)
  LFRANG_LCM_THRESHOLD_PCT   – compaction trigger threshold (default: 0.75)
  LFRANG_LCM_RECALL_LIMIT    – max memories returned by ingest (default: 5)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── config ───────────────────────────────────────────────────────────────────

def _env_int(key: str, default: int, min_v: int = 1, max_v: int = 256) -> int:
    try:
        v = int(os.environ.get(key, ""))
        return max(min_v, min(max_v, v))
    except ValueError:
        return default


def _env_float(key: str, default: float, min_v: float = 0.1, max_v: float = 0.95) -> float:
    try:
        v = float(os.environ.get(key, ""))
        return max(min_v, min(max_v, v))
    except ValueError:
        return default


DB_PATH = Path(
    os.environ.get("LFRANG_LCM_DB_PATH", "")
    or Path.home() / ".librefang" / "lcm-context" / "lcm.db"
)
HEAD_KEEP = _env_int("LFRANG_LCM_HEAD_KEEP", 2, 1, 20)
TAIL_KEEP = _env_int("LFRANG_LCM_TAIL_KEEP", 16, 4, 128)
THRESHOLD_PCT = _env_float("LFRANG_LCM_THRESHOLD_PCT", 0.75, 0.1, 0.95)
RECALL_LIMIT = _env_int("LFRANG_LCM_RECALL_LIMIT", 5, 1, 20)


# ── database ─────────────────────────────────────────────────────────────────

class Database:
    """SQLite wrapper with WAL mode, FTS5, and message/summary/promoted tables."""

    def __init__(self, path: Path):
        self.path = path
        self.fts_ok = False

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_tables(self) -> None:
        with self.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id    TEXT    NOT NULL,
                    role          TEXT    NOT NULL,
                    content       TEXT    NOT NULL,
                    message_hash  TEXT    NOT NULL,
                    created_at    TEXT    NOT NULL,
                    UNIQUE(session_id, message_hash)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_msg_session
                    ON messages(session_id, id)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id            TEXT    NOT NULL,
                    parent_summary_id     INTEGER,
                    depth                 INTEGER NOT NULL DEFAULT 0,
                    covered_message_count INTEGER NOT NULL DEFAULT 0,
                    summary_text          TEXT    NOT NULL,
                    created_at            TEXT    NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sum_session
                    ON summaries(session_id, id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sum_parent
                    ON summaries(session_id, parent_summary_id)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS promoted_knowledge (
                    id          TEXT PRIMARY KEY,
                    session_id  TEXT,
                    content     TEXT    NOT NULL,
                    tags        TEXT    NOT NULL DEFAULT '[]',
                    depth       INTEGER NOT NULL DEFAULT 0,
                    confidence  REAL    NOT NULL DEFAULT 1.0,
                    created_at  TEXT    NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_promoted_tags
                    ON promoted_knowledge(tags)
            """)

            self._setup_fts(conn)

    def _setup_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(content, role, session_id UNINDEXED, message_id UNINDEXED)
            """)
            conn.execute("""
                INSERT OR IGNORE INTO messages_fts(rowid, content, role, session_id, message_id)
                SELECT id, content, role, session_id, id FROM messages
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS msg_fts_ai AFTER INSERT ON messages BEGIN
                    INSERT OR REPLACE INTO messages_fts(rowid, content, role, session_id, message_id)
                    VALUES (new.id, new.content, new.role, new.session_id, new.id);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS msg_fts_ad AFTER DELETE ON messages BEGIN
                    DELETE FROM messages_fts WHERE rowid = old.id;
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS msg_fts_au AFTER UPDATE OF content, role, session_id ON messages BEGIN
                    INSERT OR REPLACE INTO messages_fts(rowid, content, role, session_id, message_id)
                    VALUES (new.id, new.content, new.role, new.session_id, new.id);
                END
            """)
            self.fts_ok = True
        except sqlite3.Error:
            self.fts_ok = False

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def message_hash(role: str, content: str) -> str:
        h = hashlib.sha256()
        h.update(role.encode("utf-8", errors="ignore"))
        h.update(b"\0")
        h.update(content.encode("utf-8", errors="ignore"))
        return h.hexdigest()

    @staticmethod
    def extract_text(msg: Dict[str, Any]) -> str:
        c = msg.get("content", "")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts: List[str] = []
            for item in c:
                if isinstance(item, dict):
                    t = item.get("text") or item.get("content")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
                elif isinstance(item, str) and item.strip():
                    parts.append(item.strip())
            return "\n".join(parts)
        return ""

    # ── write ops ────────────────────────────────────────────────────────────

    def persist_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> int:
        """Insert messages with dedup (by session_id + content hash)."""
        now = datetime.now(timezone.utc).isoformat()
        rows: List[Tuple[str, str, str, str, str]] = []
        for m in messages:
            role = str(m.get("role", "unknown"))
            content = self.extract_text(m)
            if not content:
                continue
            mh = self.message_hash(role, content)
            rows.append((session_id, role, content, mh, now))
        if not rows:
            return 0
        with self.connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            conn.executemany(
                "INSERT OR IGNORE INTO messages(session_id, role, content, message_hash, created_at) "
                "VALUES(?,?,?,?,?)",
                rows,
            )
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            return int(after) - int(before)

    def write_summary(self, session_id: str, source_messages: List[Dict[str, Any]]) -> Optional[int]:
        """Create a new summary node in the DAG, returning its id."""
        if not source_messages:
            return None

        snippets: List[str] = []
        for m in source_messages:
            role = str(m.get("role", "msg"))
            content = self.extract_text(m)
            if not content:
                continue
            snippet = content.replace("\n", " ").strip()[:180]
            snippets.append(f"{role}: {snippet}")
            if len(snippets) >= 24:
                break

        if not snippets:
            return None

        summary_text = "\n".join(snippets)
        now = datetime.now(timezone.utc).isoformat()

        with self.connect() as conn:
            parent = conn.execute(
                "SELECT id, depth FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            parent_id = int(parent["id"]) if parent else None
            depth = (int(parent["depth"]) + 1) if parent else 0

            cur = conn.execute(
                "INSERT INTO summaries(session_id, parent_summary_id, depth, "
                "covered_message_count, summary_text, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, parent_id, depth, len(source_messages), summary_text, now),
            )
            return int(cur.lastrowid)

    def promote_knowledge(
        self, session_id: str, content: str, tags: str = "[]", confidence: float = 1.0
    ) -> str:
        """Record a reusable insight across sessions."""
        kid = hashlib.sha256(content.encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO promoted_knowledge(id, session_id, content, tags, "
                "depth, confidence, created_at) VALUES(?,?,?,?,?,?,?)",
                (kid, session_id, content, tags, 0, confidence, now),
            )
        return kid

    # ── read ops ─────────────────────────────────────────────────────────────

    def search_messages(self, query: str, session_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
        """FTS5 search (with LIKE fallback) across messages."""
        with self.connect() as conn:
            if self.fts_ok and query.strip():
                safe_query = " ".join(f'"{w}"' for w in query.split() if w)
                if not safe_query:
                    safe_query = query
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
                        (safe_query, limit),
                    ).fetchall()
                except sqlite3.Error:
                    rows = self._search_like(conn, query, session_id, limit)
            else:
                rows = self._search_like(conn, query, session_id, limit)

        return [
            {
                "id": int(r["id"]),
                "session_id": r["session_id"],
                "role": r["role"],
                "snippet": (r["content"][:300] + "…") if len(r["content"]) > 300 else r["content"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def _search_like(
        self, conn: sqlite3.Connection, query: str, session_id: Optional[str], limit: int
    ) -> List[sqlite3.Row]:
        if session_id:
            return conn.execute(
                "SELECT id, session_id, role, content, created_at FROM messages "
                "WHERE session_id = ? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (session_id, f"%{query}%", limit),
            ).fetchall()
        else:
            return conn.execute(
                "SELECT id, session_id, role, content, created_at FROM messages "
                "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()

    def search_promoted(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Simple substring search in promoted knowledge."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, session_id, content, tags, confidence, created_at "
                "FROM promoted_knowledge "
                "WHERE content LIKE ? "
                "ORDER BY confidence DESC, created_at DESC "
                "LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "content": r["content"],
                "tags": r["tags"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_recent_summaries(self, session_id: str, limit: int = 3) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, summary_text, depth, created_at FROM summaries "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            {"id": int(r["id"]), "text": r["summary_text"], "depth": r["depth"], "created_at": r["created_at"]}
            for r in rows
        ]


# ── protocol handlers ───────────────────────────────────────────────────────

db: Optional[Database] = None


def _reply(rid: Any, result: Any) -> str:
    return json.dumps({"id": rid, "ok": result}, ensure_ascii=False)


def _error(rid: Any, msg: str) -> str:
    return json.dumps({"id": rid, "error": msg}, ensure_ascii=False)


def handle_bootstrap(params: Dict[str, Any]) -> Dict[str, Any]:
    global db
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = Database(DB_PATH)
    db.ensure_tables()
    return {
        "db_path": str(DB_PATH),
        "fts_enabled": db.fts_ok,
        "head_keep": HEAD_KEEP,
        "tail_keep": TAIL_KEEP,
        "threshold_pct": THRESHOLD_PCT,
    }


def handle_ingest(params: Dict[str, Any]) -> Dict[str, Any]:
    """Search past conversations + promoted knowledge for relevant memories."""
    user_msg = params.get("user_message") or params.get("message") or ""
    session_id = str(params.get("agent_id") or params.get("session_id") or "unknown")

    if not db:
        handle_bootstrap({})

    memories: List[Dict[str, Any]] = []

    if user_msg.strip():
        # FTS search current session
        hits = db.search_messages(user_msg, session_id, RECALL_LIMIT)
        for h in hits:
            memories.append({
                "source": "message",
                "session_id": h["session_id"],
                "snippet": h["snippet"],
                "role": h["role"],
            })

        # FTS search across ALL sessions (cross-session recall)
        cross_hits = db.search_messages(user_msg, None, max(2, RECALL_LIMIT // 2))
        for h in cross_hits:
            if h["session_id"] != session_id:
                memories.append({
                    "source": "cross_session_message",
                    "session_id": h["session_id"],
                    "snippet": h["snippet"],
                    "role": h["role"],
                })

        # Promoted knowledge
        promoted_hits = db.search_promoted(user_msg, max(2, RECALL_LIMIT // 3))
        for p in promoted_hits:
            memories.append({
                "source": "promoted_knowledge",
                "session_id": p["session_id"],
                "content": p["content"],
                "tags": p["tags"],
                "confidence": p["confidence"],
            })

    # Recent summaries for continuity
    summaries = db.get_recent_summaries(session_id, 2)
    for s in summaries:
        memories.append({
            "source": "summary",
            "session_id": session_id,
            "content": s["text"],
            "depth": s["depth"],
        })

    return {"recalled_memories": memories[:RECALL_LIMIT + 5]}


def handle_assemble(params: Dict[str, Any]) -> Dict[str, Any]:
    """Window management: head/tail keep.
    
    Per spec (docs/architecture/sidecar-context-engine.md):
      - Returns trimmed/reordered window + recovery stage.
      - compact runs in Rust (inner engine) — NOT bridged to sidecar.
      - No DB writes here; persistence is after_turn's job.
    """
    messages: List[Dict[str, Any]] = params.get("messages", [])
    context_window_tokens: int = int(params.get("context_window_tokens", 200_000))
    threshold_tokens = int(context_window_tokens * THRESHOLD_PCT)

    # Simple token estimate: ~4 chars per token
    def _est_tokens(msgs: List[Dict[str, Any]]) -> int:
        total = 0
        for m in msgs:
            c = m.get("content", "")
            if isinstance(c, str):
                total += max(1, len(c) // 4)
            elif isinstance(c, list):
                for item in c:
                    if isinstance(item, dict):
                        total += max(1, len(str(item.get("text", ""))) // 4)
                    elif isinstance(item, str):
                        total += max(1, len(item) // 4)
        return total

    total_tokens = _est_tokens(messages)

    if total_tokens < threshold_tokens or len(messages) <= (HEAD_KEEP + TAIL_KEEP + 4):
        return {"messages": messages, "recovery": "None"}

    # Head/tail keep — pure window management, no DB writes
    head = messages[:HEAD_KEEP]
    tail = messages[-TAIL_KEEP:]
    removed = len(messages) - HEAD_KEEP - TAIL_KEEP

    marker = {
        "role": "system",
        "content": (
            f"[LCM] {removed} earlier messages were compacted into lossless SQLite storage. "
            f"Use lcm_grep / lcm_expand to recall specifics."
        ),
    }

    return {
        "messages": head + [marker] + tail,
        "recovery": {"AutoCompaction": {"removed": removed}},
    }


def handle_after_turn(params: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the full turn transcript to the database."""
    agent_id = str(params.get("agent_id") or "unknown")
    messages: List[Dict[str, Any]] = params.get("messages", [])

    if not db:
        handle_bootstrap({})

    if not messages:
        return {}

    try:
        saved = db.persist_messages(agent_id, messages)
        return {"persisted": saved, "session_id": agent_id}
    except Exception as exc:
        return {"persisted": 0, "error": str(exc)}


HANDLERS = {
    "bootstrap":  handle_bootstrap,
    "ingest":     handle_ingest,
    "assemble":   handle_assemble,
    "after_turn": handle_after_turn,
}


# ── main loop ────────────────────────────────────────────────────────────────

def main() -> None:
    # Write startup mark to stderr so the daemon can log it
    print(f"[lcm-sidecar] started, db={DB_PATH}", file=sys.stderr, flush=True)

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

        rid = req.get("id")
        method = req.get("method", "")
        handler = HANDLERS.get(method)

        if handler is None:
            out = _error(rid, f"unknown method: {method}")
        else:
            try:
                result = handler(req.get("params", {}))
                out = _reply(rid, result)
            except Exception as exc:
                out = _error(rid, str(exc))

        sys.stdout.write(out + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
