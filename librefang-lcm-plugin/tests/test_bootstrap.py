"""Tests for bootstrap hook."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import run_hook


def test_bootstrap_creates_db(temp_db_path: Path):
    """Fresh bootstrap creates database with all tables."""
    result, stderr, rc = run_hook(
        "bootstrap.py",
        {"type": "bootstrap", "context_window_tokens": 200_000},
    )
    assert rc == 0
    assert result["type"] == "ok"
    assert result.get("db_path") == str(temp_db_path)
    assert result.get("fts_enabled") is True

    conn = sqlite3.connect(str(temp_db_path))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "messages" in tables
    assert "summaries" in tables
    assert "promoted_knowledge" in tables
    assert "messages_fts" in tables
    conn.close()


def test_bootstrap_idempotent(temp_db_path: Path):
    """Second bootstrap call is safe."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})
    result, stderr, rc = run_hook(
        "bootstrap.py",
        {"type": "bootstrap", "context_window_tokens": 200_000},
    )
    assert rc == 0
    assert result["type"] == "ok"


def test_bootstrap_invalid_input():
    """Graceful handling of invalid JSON."""
    import subprocess
    proc = subprocess.Popen(
        ["python3", "hooks/bootstrap.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout_data, _ = proc.communicate(input="not json", timeout=5)
    # Should not crash — either {"type":"ok"} with defaults or graceful empty
    assert proc.returncode == 0 or json.loads(stdout_data)["type"] == "error"
