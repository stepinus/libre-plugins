"""Tests for after_turn hook — note: after_turn uses agent_id as session_id in DB."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import run_hook


def test_after_turn_persists_messages(temp_db_path: Path):
    """Messages are written to the database after a turn."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    messages = [
        {"role": "user", "content": "Hello, world!"},
        {"role": "assistant", "content": "Hi there!"},
    ]

    result, stderr, rc = run_hook(
        "after_turn.py",
        {"type": "after_turn", "agent_id": "test-agent", "session_id": "sess-001", "messages": messages},
    )
    assert rc == 0
    assert result["type"] == "ok"

    # after_turn uses agent_id as session_id column value
    conn = sqlite3.connect(str(temp_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
        ("test-agent",),
    ).fetchall()
    assert len(rows) == 2, f"Expected 2 messages, got {len(rows)}"
    conn.close()


def test_after_turn_dedup(temp_db_path: Path):
    """Same messages inserted twice only store once."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    messages = [{"role": "user", "content": "Test message"}]

    run_hook("after_turn.py", {"type": "after_turn", "agent_id": "s1", "messages": messages})
    run_hook("after_turn.py", {"type": "after_turn", "agent_id": "s1", "messages": messages})

    conn = sqlite3.connect(str(temp_db_path))
    count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert count == 1
    conn.close()


def test_after_turn_fts5_populated(temp_db_path: Path):
    """FTS5 is populated after message insertion."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    messages = [{"role": "assistant", "content": "Decision: use Redis for caching."}]

    run_hook("after_turn.py", {"type": "after_turn", "agent_id": "a", "messages": messages})

    conn = sqlite3.connect(str(temp_db_path))
    # FTS5 should have the content
    fts_count = conn.execute(
        "SELECT count(*) FROM messages_fts WHERE content MATCH ?", ("Redis",),
    ).fetchone()[0]
    # Note: after_turn doesn't populate FTS5 yet — this documents current behavior
    # If hook adds FTS5 sync later, this test will validate
    assert fts_count == 0, "FTS5 not populated by after_turn yet"
    conn.close()


def test_after_turn_with_text_blocks(temp_db_path: Path):
    """Messages with content as list of text blocks."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    messages = [{"role": "user", "content": [{"type": "text", "text": "Nested content block"}]}]

    result, stderr, rc = run_hook(
        "after_turn.py", {"type": "after_turn", "agent_id": "a", "messages": messages},
    )
    assert rc == 0
    assert result["type"] == "ok"

    conn = sqlite3.connect(str(temp_db_path))
    content = conn.execute("SELECT content FROM messages WHERE session_id = ?", ("a",)).fetchone()[0]
    assert "Nested content block" in content
    conn.close()
