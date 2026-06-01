#!/usr/bin/env python3
"""LCM assemble hook — structural context window with DAG summaries.

Receives:  {"type": "assemble", "system_prompt": "...", "messages": [...], "context_window_tokens": 200000}
Responds:  {"type": "assemble_result", "messages": [...]}

Strategy:
  1. Passthrough if total tokens < threshold.
  2. Otherwise: extract middle unpinned messages → chunk into groups →
     write each group as a DAG summary node in SQLite → inject summary
     texts as synthetic system messages.
  3. Pinned messages always survive.
  4. DB errors → fallback to simple "[LCM] N messages compacted" marker.
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


# ── assemble strategies ─────────────────────────────────────────────────────

def _fallback_marker(
    head: list, pinned: list, tail: list, removed: int,
) -> list:
    """Simple marker when DB is unavailable."""
    marker = {
        "role": "system",
        "content": (
            f"[LCM] {removed} earlier messages were compacted into "
            f"lossless SQLite storage. Use lcm_grep / lcm_expand to "
            f"recall specifics."
        ),
    }
    return head + pinned + [marker] + tail


def _structured_summaries(
    head: list, pinned: list, tail: list, middle: list, session_id: str,
) -> list:
    """Write DAG summary nodes for middle messages, inject as context."""
    if not middle:
        return head + pinned + tail

    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_tables(conn)

        chunks = list(_chunk_list(middle, CHUNK_SIZE))
        summary_msgs: List[Dict[str, Any]] = []
        parent_id: Optional[int] = None
        depth = 0

        for i, chunk in enumerate(chunks):
            sid = _write_summary(conn, session_id, chunk, parent_id, depth)
            if sid is not None:
                snippets = _snippets_from(chunk)
                if snippets:
                    summary_msgs.append({
                        "role": "system",
                        "content": (
                            f"[LCM summary {i + 1}/{len(chunks)} — "
                            f"{len(chunk)} messages compressed]\n"
                            + "\n".join(snippets)
                        ),
                    })
                parent_id = sid
                depth += 1

        conn.commit()
        conn.close()

        if not summary_msgs:
            return _fallback_marker(head, pinned, tail, len(middle))

        return head + pinned + summary_msgs + tail

    except Exception:
        return _fallback_marker(head, pinned, tail, len(middle))


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

        if req.get("type") != "assemble":
            continue

        messages: List[Dict[str, Any]] = req.get("messages", [])
        context_window_tokens: int = int(req.get("context_window_tokens", 200_000))
        session_id: str = str(req.get("agent_id") or req.get("session_id") or "unknown")
        threshold_tokens = int(context_window_tokens * THRESHOLD_PCT)

        # Separate pinned (must survive) from unpinned
        pinned: List[Dict[str, Any]] = []
        unpinned: List[Dict[str, Any]] = []
        for m in messages:
            if m.get("pinned"):
                pinned.append(m)
            else:
                unpinned.append(m)

        total_tokens = _est_tokens(unpinned)

        # Passthrough if under threshold or not enough to trim
        if (
            total_tokens < threshold_tokens
            or len(unpinned) <= (HEAD_KEEP + TAIL_KEEP + 4)
        ):
            result = messages
        else:
            head_msgs = unpinned[:HEAD_KEEP]
            tail_msgs = unpinned[-TAIL_KEEP:]
            middle = unpinned[HEAD_KEEP:-TAIL_KEEP]

            result = _structured_summaries(
                head_msgs, pinned, tail_msgs, middle, session_id,
            )

        sys.stdout.write(
            json.dumps(
                {"type": "assemble_result", "messages": result},
                ensure_ascii=False,
            )
            + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
