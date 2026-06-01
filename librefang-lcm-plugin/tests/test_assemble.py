"""Tests for assemble hook — context window assembly with DAG summaries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import run_hook, make_messages


def test_assemble_passthrough_under_threshold(temp_db_path: Path):
    """Context under threshold returns unchanged."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(10)
    result, stderr, rc = run_hook(
        "assemble.py",
        {"type": "assemble", "agent_id": "a", "messages": msgs, "context_window_tokens": 200_000},
    )
    assert rc == 0
    assert result["type"] == "assemble_result"
    assert len(result["messages"]) == len(msgs)


def test_assemble_compresses_large_context(temp_db_path: Path):
    """Large context triggers structured summary compression."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(60, prefix="Asm")
    result, stderr, rc = run_hook(
        "assemble.py",
        {"type": "assemble", "agent_id": "a", "messages": msgs, "context_window_tokens": 2000},
    )
    assert rc == 0
    output = result["messages"]
    assert len(output) < len(msgs)

    markers = [m for m in output if "[LCM summary" in m.get("content", "")]
    assert len(markers) > 0

    conn = sqlite3.connect(str(temp_db_path))
    count = conn.execute("SELECT count(*) FROM summaries").fetchone()[0]
    assert count > 0
    conn.close()


def test_assemble_preserves_pinned(temp_db_path: Path):
    """Pinned messages survive assemble compression."""
    run_hook("bootstrap.py", {"type": "bootstrap", "context_window_tokens": 200_000})

    msgs = make_messages(60)
    msgs[10]["pinned"] = True
    pinned_content = msgs[10]["content"]

    result, stderr, rc = run_hook(
        "assemble.py",
        {"type": "assemble", "agent_id": "a", "messages": msgs, "context_window_tokens": 2000},
    )
    assert rc == 0
    output = result["messages"]
    pinned_outs = [m for m in output if m.get("content") == pinned_content]
    assert len(pinned_outs) == 1
    assert pinned_outs[0].get("pinned") is True


def test_assemble_fallback_on_db_error(temp_db_path: Path):
    """Fallback marker when DB path is unwritable (use invalid path)."""
    import os
    # Use a path that assemble's mkdir(parents=True) will fail on
    os.environ["LFRANG_LCM_DB_PATH"] = "/nonexistent-root-zzz/lcm.db"

    msgs = make_messages(60)
    result, stderr, rc = run_hook(
        "assemble.py",
        {"type": "assemble", "agent_id": "a", "messages": msgs, "context_window_tokens": 2000},
    )
    assert rc == 0
    output = result["messages"]
    markers = [m for m in output if "earlier messages were compacted" in m.get("content", "")]
    assert len(markers) == 1, f"Expected fallback marker, got roles: {[m['role'] for m in output]}"


def test_assemble_loop_input(temp_db_path: Path):
    """Assemble reads multiple JSON lines in loop mode."""
    import subprocess
    proc = subprocess.Popen(
        ["python3", str(Path(__file__).parent.parent / "hooks" / "assemble.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    msgs1 = make_messages(10, "A")
    msgs2 = make_messages(10, "B")
    stdin_data = (
        json.dumps({"type": "assemble", "agent_id": "a1", "messages": msgs1, "context_window_tokens": 200_000}) + "\n"
        + json.dumps({"type": "assemble", "agent_id": "a2", "messages": msgs2, "context_window_tokens": 200_000}) + "\n"
    )

    stdout_data, stderr = proc.communicate(input=stdin_data, timeout=10)

    lines = [l for l in stdout_data.strip().split("\n") if l.strip()]
    assert len(lines) == 2, f"Got {len(lines)} lines, stdout={stdout_data[:200]}, stderr={stderr[:200]}"
    for line in lines:
        obj = json.loads(line)
        assert obj["type"] == "assemble_result"
