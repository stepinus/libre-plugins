"""Tests for compact hook — DAG summary compression."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import run_hook, make_messages


def test_compact_passthrough_under_threshold(temp_db_path: Path):
    """Small context passes through unchanged."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(10)  # few messages, under threshold
    result, stderr, rc = run_hook(
        "compact.py",
        {"type": "compact", "agent_id": "a", "messages": msgs, "context_window_tokens": 200_000},
    )
    assert rc == 0
    assert result["type"] == "compact_result"
    # Passthrough — same count
    assert len(result["messages"]) == len(msgs)


def test_compact_compresses_large_context(temp_db_path: Path):
    """Large context gets compressed with DAG summaries."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(60, prefix="Big")
    result, stderr, rc = run_hook(
        "compact.py",
        {"type": "compact", "agent_id": "a", "messages": msgs, "context_window_tokens": 2000},
    )
    assert rc == 0
    assert result["type"] == "compact_result"
    output = result["messages"]
    # Output should be smaller: 2 head + N summaries + 16 tail
    head_count = len([m for m in output if m["role"] != "system"])
    assert len(output) < len(msgs)
    assert any("[LCM compact" in m.get("content", "") for m in output)

    # Check DB has summary nodes
    conn = sqlite3.connect(str(temp_db_path))
    count = conn.execute("SELECT count(*) FROM summaries").fetchone()[0]
    assert count > 0
    conn.close()


def test_compact_preserves_head_tail(temp_db_path: Path):
    """Head and tail messages survive compression."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(60)
    result, stderr, rc = run_hook(
        "compact.py",
        {"type": "compact", "agent_id": "a", "messages": msgs, "context_window_tokens": 2000},
    )
    assert rc == 0
    output = result["messages"]

    # Head messages (first non-system)
    non_system = [m for m in output if m["role"] != "system"]
    assert non_system[0]["content"] == msgs[0]["content"]
    assert non_system[-1]["content"] == msgs[-1]["content"]


def test_compact_preserves_pinned(temp_db_path: Path):
    """Pinned messages survive regardless of position."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(60)
    msgs[25]["pinned"] = True  # middle pinned message
    pinned_content = msgs[25]["content"]

    result, stderr, rc = run_hook(
        "compact.py",
        {"type": "compact", "agent_id": "a", "messages": msgs, "context_window_tokens": 2000},
    )
    assert rc == 0
    output = result["messages"]
    # Pinned message should be present
    pinned_outs = [m for m in output if m.get("content") == pinned_content]
    assert len(pinned_outs) == 1


def test_compact_creates_dag_with_parent_chain(temp_db_path: Path):
    """Summary nodes form a parent chain (depth increasing)."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(60, prefix="Chain")
    run_hook(
        "compact.py",
        {"type": "compact", "agent_id": "a", "messages": msgs, "context_window_tokens": 2000},
    )

    conn = sqlite3.connect(str(temp_db_path))
    conn.row_factory = sqlite3.Row
    nodes = conn.execute(
        "SELECT id, depth, parent_summary_id FROM summaries WHERE session_id = ? ORDER BY id",
        ("a",),
    ).fetchall()
    conn.close()

    assert len(nodes) > 1
    for i, node in enumerate(nodes):
        if i == 0:
            assert node["parent_summary_id"] is None
        else:
            # Parent should reference previous node
            assert node["parent_summary_id"] == nodes[i - 1]["id"]
        assert node["depth"] == i
