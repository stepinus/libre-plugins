"""Tests for ingest hook — memory recall."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from conftest import run_hook


def test_ingest_empty_db(temp_db_path: Path):
    """Ingest with empty database returns no errors."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    result, stderr, rc = run_hook(
        "ingest.py",
        {"type": "ingest", "agent_id": "test-agent", "session_id": "s-unknown",
         "message": "completely new topic never seen before"},
    )
    assert rc == 0
    assert result["type"] == "ingest_result"
    assert "memories" in result


def test_ingest_with_populated_db(temp_db_path: Path):
    """Ingest finds matching past messages from seeded DB."""
    # Create and seed: ingest uses agent_id as session_id
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    agent = "s1"  # agent_id used as session_id by ingest
    conn = sqlite3.connect(str(temp_db_path))
    conn.executemany(
        "INSERT OR IGNORE INTO messages(session_id, role, content, message_hash, created_at) VALUES(?,?,?,?,?)",
        [
            (agent, "user", "What is Python?", "h01", "2026-01-01T00:00:00"),
            (agent, "assistant", "Python is a language.", "h02", "2026-01-01T00:01:00"),
            (agent, "user", "Show me async patterns.", "h03", "2026-01-01T00:02:00"),
            (agent, "assistant", "Use asyncio for async.", "h04", "2026-01-01T00:03:00"),
        ],
    )
    conn.execute(
        "INSERT OR IGNORE INTO messages_fts(rowid, content, role, session_id, message_id) "
        "SELECT id, content, role, session_id, id FROM messages"
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    result, _, rc = run_hook(
        "ingest.py",
        {"type": "ingest", "agent_id": agent,
         "message": "async patterns"},
    )
    assert rc == 0
    assert result["type"] == "ingest_result"
    memories = result.get("memories", [])
    contents = " ".join(m.get("content", "") for m in memories).lower()
    assert "async" in contents, f"Expected 'async' in memories: {memories}"
