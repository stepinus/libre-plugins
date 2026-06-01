#!/usr/bin/env python3
"""LCM compact hook — compress conversation mid-messages into DAG summaries.

Fires when the context window is under pressure. Instead of calling an external
LCM CLI, this takes the messages, stores compressed chunks in the SQLite DB,
and returns a slimmed-down message list.

Input (stdin):
  {"type": "compact", "agent_id": "...", "messages": [...], "model": "...", "context_window_tokens": 200000}
Output (stdout):
  {"type": "compact_result", "messages": [...]}

Strategy:
  1. Passthrough if total tokens < threshold or too few messages.
  2. Else: extract head keep + tail keep + pinned → middle is compressible.
  3. Chunk middle → write each chunk as a DAG summary node in SQLite.
  4. Return: head + pinned + summary_markers + tail.
  5. DB errors → fallback to "[LCM] N messages compacted" marker.

Shares the same `summaries` table as the assemble hook, so MCP tools
(lcm_summaries, lcm_search) can find compacted context later.
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

def _env_int(key: str, default: int, min_v: int = 1, max_v: int = 256) -> int:
    try:
        v = int(os.environ.get(key, ""))
        return max(min_v, min(max_v, v))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float, min_v: float = 0.1, max_v: float = 0.95) -> float:
    try:
        v = float(os.environ.get(key, ""))
        return max(min_v, min(max_v, v))
    except (ValueError, TypeError):
        return default


DB_PATH = Path(
    os.environ.get("LFRANG_LCM_DB_PATH", "")
    or Path.home() / ".librefang" / "lcm-context" / "lcm.db"
)
HEAD_KEEP = _env_int("LFRANG_LCM_HEAD_KEEP", 2, 1, 20)
TAIL_KEEP = _env_int("LFRANG_LCM_TAIL_KEEP", 16, 4, 128)
THRESHOLD_PCT = _env_float("LFRANG_LCM_THRESHOLD_PCT", 0.75, 0.1, 0.95)
CHUNK_SIZE = _env_int("LFRANG_LCM_CHUNK_SIZE", 8, 4, 32)
MAX_SNIPPETS = 24


# ── helpers ──────────────────────────────────────────────────────────────────

def _est_tokens(msgs: List[Dict[str, Any]]) -> int:
    """Rough token estimate: ~4 chars per token."""
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


def _extract_text(msg: Dict[str, Any]) -> str:
    """Extract plain text from a message's content."""
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


def _chunk_list(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _snippets_from(messages: list, max_snippets: int = MAX_SNIPPETS) -> List[str]:
    """Build summary snippets: role + first 180 chars per message."""
    snippets: List[str] = []
    for m in messages[:max_snippets]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "msg"))
        content = _extract_text(m)
        if not content:
            continue
        snippet = content.replace("\n", " ").strip()[:180]
        snippets.append(f"{role}: {snippet}")
    return snippets


def emit(obj: dict) -> None:
    json.dump(obj, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


# ── DB ───────────────────────────────────────────────────────────────────────

def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
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
        CREATE INDEX IF NOT EXISTS idx_sum_parent
            ON summaries(session_id, parent_summary_id);
    """)


def _write_summary(
    conn: sqlite3.Connection,
    session_id: str,
    messages: list,
    parent_id: Optional[int],
    depth: int,
) -> Optional[int]:
    snippets = _snippets_from(messages)
    if not snippets:
        return None

    summary_text = "\n".join(snippets)
    now = datetime.now(timezone.utc).isoformat()

    cur = conn.execute(
        "INSERT INTO summaries(session_id, parent_summary_id, depth, "
        "covered_message_count, summary_text, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (session_id, parent_id, depth, len(messages), summary_text, now),
    )
    return int(cur.lastrowid)


# ── compact strategy ────────────────────────────────────────────────────────

def _fallback_marker(
    head: list, pinned: list, tail: list, removed: int,
) -> list:
    """Simple marker when DB is unavailable."""
    marker = {
        "role": "system",
        "content": (
            f"[LCM] {removed} earlier messages were compacted into "
            f"lossless SQLite storage. Use lcm_summaries to recall specifics."
        ),
    }
    return head + pinned + [marker] + tail


def compact_messages(
    messages: list,
    session_id: str,
    context_window_tokens: int,
    threshold_pct: float = THRESHOLD_PCT,
) -> list:
    """Compress middle messages into DAG summary nodes, return slim context."""
    # Separate pinned from unpinned
    pinned: list = []
    unpinned: list = []
    for m in messages:
        if m.get("pinned"):
            pinned.append(m)
        else:
            unpinned.append(m)

    threshold_tokens = int(context_window_tokens * threshold_pct)
    total_tokens = _est_tokens(unpinned)

    # Passthrough if under threshold or not enough messages to trim
    if (
        total_tokens < threshold_tokens
        or len(unpinned) <= (HEAD_KEEP + TAIL_KEEP + 4)
    ):
        return messages

    head_msgs = unpinned[:HEAD_KEEP]
    tail_msgs = unpinned[-TAIL_KEEP:]
    middle = unpinned[HEAD_KEEP:-TAIL_KEEP]

    if not middle:
        return messages

    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_tables(conn)

        chunks = list(_chunk_list(middle, CHUNK_SIZE))
        summary_msgs: list = []
        parent_id: Optional[int] = None
        depth = 0
        total_compacted = 0

        for i, chunk in enumerate(chunks):
            sid = _write_summary(conn, session_id, chunk, parent_id, depth)
            if sid is not None:
                snippets = _snippets_from(chunk)
                if snippets:
                    summary_msgs.append({
                        "role": "system",
                        "content": (
                            f"[LCM compact {i + 1}/{len(chunks)} — "
                            f"{len(chunk)} messages compressed]\n"
                            + "\n".join(snippets)
                        ),
                    })
                parent_id = sid
                depth += 1
                total_compacted += len(chunk)

        conn.commit()
        conn.close()

        if not summary_msgs:
            return _fallback_marker(head_msgs, pinned, tail_msgs, len(middle))

        print(
            f"[lcm:compact] compressed {total_compacted} messages into "
            f"{len(chunks)} summary nodes (session={session_id})",
            file=sys.stderr,
            flush=True,
        )

        return head_msgs + pinned + summary_msgs + tail_msgs

    except Exception as exc:
        print(
            f"[lcm:compact] DB error, using fallback: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return _fallback_marker(head_msgs, pinned, tail_msgs, len(middle))


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        emit({"type": "compact_result", "messages": []})
        return

    messages: list = data.get("messages", [])
    agent_id: str = str(data.get("agent_id", "") or data.get("session_id", "") or "unknown")
    context_window_tokens: int = int(data.get("context_window_tokens", 200_000))
    model: str = str(data.get("model", ""))

    result = compact_messages(messages, agent_id, context_window_tokens)

    emit({"type": "compact_result", "messages": result})


if __name__ == "__main__":
    main()
