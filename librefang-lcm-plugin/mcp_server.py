#!/usr/bin/env python3
"""LCM MCP Server — JSON-RPC 2.0 over stdio.

Native Python MCP server that operates directly on the LCM SQLite database.
No Node.js / daemon dependency. Speaks standard MCP JSON-RPC protocol.

Tools:
  lcm_search    — FTS5 full-text search across messages + promoted knowledge
  lcm_promote   — record a reusable insight in promoted_knowledge
  lcm_summaries — read DAG summary nodes for a session
  lcm_stats     — database statistics (message count, sessions, etc.)
"""

from __future__ import annotations

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
DEFAULT_LIMIT = 10


# ── DB ───────────────────────────────────────────────────────────────────────

def ensure_tables() -> None:
    """Create all LCM tables if they don't exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
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

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, role, session_id UNINDEXED, message_id UNINDEXED);
        """)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def has_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT count(*) FROM messages_fts").fetchone()
        return True
    except sqlite3.Error:
        return False


# ── tools ────────────────────────────────────────────────────────────────────

def tool_search(query: str, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """FTS5 search across messages, with cross-session scope."""
    if not DB_PATH.exists():
        return []

    with connect() as conn:
        fts = has_fts(conn)

        if fts and query.strip():
            safe_query = " ".join(f'"{w}"' for w in query.split() if w)
            if not safe_query:
                safe_query = query
            try:
                rows = conn.execute(
                    """
                    SELECT m.session_id, m.role, m.content, m.created_at
                    FROM messages_fts f
                    JOIN messages m ON m.id = f.rowid
                    WHERE f.content MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (safe_query, limit),
                ).fetchall()
            except sqlite3.Error:
                fts = False
        else:
            fts = False

        if not fts:
            rows = conn.execute(
                "SELECT session_id, role, content, created_at FROM messages "
                "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()

        promoted = conn.execute(
            "SELECT session_id, content, created_at FROM promoted_knowledge "
            "WHERE content LIKE ? ORDER BY confidence DESC, created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()

    return [
        {
            "source": "message",
            "session_id": r["session_id"],
            "role": r["role"],
            "snippet": r["content"][:300],
            "created_at": r["created_at"],
        }
        for r in rows
    ] + [
        {
            "source": "promoted",
            "session_id": pr["session_id"] or "",
            "role": "knowledge",
            "snippet": pr["content"][:300],
            "created_at": pr["created_at"],
        }
        for pr in promoted
    ]


def tool_promote(content: str, tags: list | None = None, confidence: float = 1.0) -> Dict[str, Any]:
    """Store a reusable insight across sessions."""
    import hashlib

    kid = hashlib.sha256(content.encode()).hexdigest()[:16]
    tags_json = json.dumps(tags or [])
    session_id = "mcp-promoted"
    now = datetime.now(timezone.utc).isoformat()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS promoted_knowledge ("
            "id TEXT PRIMARY KEY, session_id TEXT, content TEXT NOT NULL, "
            "tags TEXT NOT NULL DEFAULT '[]', depth INTEGER NOT NULL DEFAULT 0, "
            "confidence REAL NOT NULL DEFAULT 1.0, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO promoted_knowledge(id, session_id, content, "
            "tags, depth, confidence, created_at) VALUES(?,?,?,?,?,?,?)",
            (kid, session_id, content, tags_json, 0, confidence, now),
        )
        conn.commit()

    return {"id": kid, "content": content[:200], "confidence": confidence}


def tool_summaries(
    session_id: str | None = None, limit: int = 5,
) -> List[Dict[str, Any]]:
    """Read DAG summary nodes."""
    if not DB_PATH.exists():
        return []

    with connect() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT id, session_id, parent_summary_id, depth, "
                "covered_message_count, summary_text, created_at "
                "FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, session_id, parent_summary_id, depth, "
                "covered_message_count, summary_text, created_at "
                "FROM summaries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    return [
        {
            "id": r["id"],
            "session_id": r["session_id"],
            "parent_id": r["parent_summary_id"],
            "depth": r["depth"],
            "messages": r["covered_message_count"],
            "text": r["summary_text"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def tool_stats() -> Dict[str, Any]:
    """Database overview."""
    if not DB_PATH.exists():
        return {"db_exists": False, "db_path": str(DB_PATH)}

    with connect() as conn:
        msg_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        sessions = conn.execute(
            "SELECT count(DISTINCT session_id) FROM messages"
        ).fetchone()[0]
        sum_count = 0
        prom_count = 0
        try:
            sum_count = conn.execute("SELECT count(*) FROM summaries").fetchone()[0]
        except sqlite3.Error:
            pass
        try:
            prom_count = conn.execute(
                "SELECT count(*) FROM promoted_knowledge"
            ).fetchone()[0]
        except sqlite3.Error:
            pass
        fts = has_fts(conn)

    return {
        "db_path": str(DB_PATH),
        "db_exists": True,
        "messages": msg_count,
        "sessions": sessions,
        "summaries": sum_count,
        "promoted": prom_count,
        "fts_enabled": fts,
    }


# ── MCP protocol ─────────────────────────────────────────────────────────────

def handle_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """Process a single JSON-RPC 2.0 request, return a response dict."""
    rid = req.get("id")
    method = req.get("method", "")
    params: Dict[str, Any] = req.get("params", {})

    # ── initialize ──
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "lcm",
                    "version": "0.2.0",
                },
            },
        }

    # ── notifications (no id) ──
    if rid is None:
        if method == "notifications/initialized":
            return {}  # no response for notifications
        return {}

    # ── tools/list ──
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "tools": [
                    {
                        "name": "lcm_search",
                        "description": (
                            "Search LCM memory for past conversations matching a query. "
                            "Finds messages, decisions, patterns, and errors from past "
                            "sessions using full-text search. Use this before starting "
                            "any task to recover context."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Search terms (supports FTS5 syntax)",
                                },
                                "limit": {
                                    "type": "integer",
                                    "description": f"Max results (default {DEFAULT_LIMIT}, max 50)",
                                    "default": DEFAULT_LIMIT,
                                },
                            },
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "lcm_promote",
                        "description": (
                            "Promote a reusable insight to cross-session knowledge. "
                            "Use this after making a decision, discovering a pattern, "
                            "or fixing a tricky bug — so future sessions can recover it."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "The insight to preserve (decision, pattern, fix)",
                                },
                                "tags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Tags for categorization (e.g. ['architecture', 'bug'])",
                                },
                                "confidence": {
                                    "type": "number",
                                    "description": "Confidence 0.0-1.0 (default 1.0)",
                                    "default": 1.0,
                                },
                            },
                            "required": ["content"],
                        },
                    },
                    {
                        "name": "lcm_summaries",
                        "description": (
                            "Read DAG summary nodes for a session or across all sessions. "
                            "Summaries compress older conversation chunks while preserving "
                            "their content. Use to recover context after compaction."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "session_id": {
                                    "type": "string",
                                    "description": "Session to query (omit for most recent across all)",
                                },
                                "limit": {
                                    "type": "integer",
                                    "description": "Max summaries to return (default 5)",
                                    "default": 5,
                                },
                            },
                        },
                    },
                    {
                        "name": "lcm_stats",
                        "description": (
                            "Get LCM database statistics — message count, session count, "
                            "summaries, promoted knowledge, FTS status."
                        ),
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            },
        }

    # ── tools/call ──
    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args: Dict[str, Any] = params.get("arguments", {})

        try:
            if tool_name == "lcm_search":
                q = str(tool_args.get("query", ""))
                lim = min(int(tool_args.get("limit", DEFAULT_LIMIT)), 50)
                result = tool_search(q, lim)
            elif tool_name == "lcm_promote":
                content = str(tool_args.get("content", ""))
                tags = tool_args.get("tags") or []
                conf = float(tool_args.get("confidence", 1.0))
                result = tool_promote(content, tags, conf)
            elif tool_name == "lcm_summaries":
                sid = tool_args.get("session_id")
                lim = int(tool_args.get("limit", 5))
                result = tool_summaries(sid, lim)
            elif tool_name == "lcm_stats":
                result = tool_stats()
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }

            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32000, "message": str(exc)},
            }

    # ── unknown ──
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ensure_tables()
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

        resp = handle_request(req)
        if resp:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
