"""Shared fixtures for LCM hook tests.

Provides a temporary database path and helper utilities
so every test starts with a clean state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Add hooks directory to path
HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


@pytest.fixture
def temp_db_path(monkeypatch, tmp_path: Path) -> Path:
    """Isolated database per test."""
    db_path = tmp_path / "lcm.db"
    monkeypatch.setenv("LFRANG_LCM_DB_PATH", str(db_path))
    return db_path


@pytest.fixture
def seeded_db(temp_db_path: Path) -> sqlite3.Connection:
    """Database pre-populated with messages, summaries, and promoted knowledge."""
    from bootstrap import ensure_db  # type: ignore[import-untyped]

    temp_db_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Manually create tables instead of calling ensure_db() (which uses
    # bootstrap's module-level DB_PATH that may not match our temp path)
    conn = sqlite3.connect(str(temp_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, message_hash TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(session_id, message_hash)
        );
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            parent_summary_id INTEGER, depth INTEGER NOT NULL DEFAULT 0,
            covered_message_count INTEGER NOT NULL DEFAULT 0,
            summary_text TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promoted_knowledge (
            id TEXT PRIMARY KEY, session_id TEXT, content TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]', depth INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 1.0, created_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
        USING fts5(content, role, session_id UNINDEXED, message_id UNINDEXED);
    """)

    # Insert messages
    msgs = [
        ("s1", "user", "What is Python?", "hash01", "2026-01-01T00:00:00"),
        ("s1", "assistant", "Python is a programming language.", "hash02", "2026-01-01T00:01:00"),
        ("s1", "user", "Show me async patterns.", "hash03", "2026-01-01T00:02:00"),
        ("s1", "assistant", "Use asyncio for async Python.", "hash04", "2026-01-01T00:03:00"),
        ("s2", "user", "How to write tests?", "hash05", "2026-01-02T00:00:00"),
        ("s2", "assistant", "Use pytest for testing.", "hash06", "2026-01-02T00:01:00"),
    ]
    conn.executemany(
        "INSERT INTO messages(session_id, role, content, message_hash, created_at) "
        "VALUES(?,?,?,?,?)",
        msgs,
    )

    # Insert summaries
    summaries = [
        ("s1", None, 0, 2, "user: old convo start\nassistant: old convo reply", "2026-01-01T00:00:00"),
        ("s1", 1, 1, 2, "user: continue\nassistant: more reply", "2026-01-01T00:01:00"),
    ]
    conn.executemany(
        "INSERT INTO summaries(session_id, parent_summary_id, depth, "
        "covered_message_count, summary_text, created_at) "
        "VALUES(?,?,?,?,?,?)",
        summaries,
    )

    # Insert promoted knowledge
    conn.execute(
        "INSERT INTO promoted_knowledge(id, session_id, content, tags, depth, "
        "confidence, created_at) VALUES(?,?,?,?,?,?,?)",
        ("pk1", "s0", "Always use virtualenv for Python projects.", '["python","best-practice"]', 0, 1.0, "2026-01-01T00:00:00"),
    )

    # Populate FTS5
    conn.execute(
        "INSERT INTO messages_fts(rowid, content, role, session_id, message_id) "
        "SELECT id, content, role, session_id, id FROM messages"
    )

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return conn


def make_messages(count: int, prefix: str = "Msg") -> List[Dict[str, Any]]:
    """Generate synthetic messages for testing."""
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"{prefix}{i}: " + "lorem ipsum " * 10,
        }
        for i in range(count)
    ]


def run_hook(script: str, stdin_data: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a hook subprocess and return parsed stdout response."""
    import subprocess
    proc = subprocess.Popen(
        ["python3", script],
        cwd=str(HOOKS_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_data, stderr_data = proc.communicate(
        input=json.dumps(stdin_data), timeout=10,
    )
    result = json.loads(stdout_data)
    return result, stderr_data, proc.returncode
