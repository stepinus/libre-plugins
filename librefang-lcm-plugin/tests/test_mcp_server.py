"""Tests for MCP server — JSON-RPC 2.0 over stdio."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

MCP_SERVER = Path(__file__).resolve().parent.parent / "mcp_server.py"


def mcp_call(method: str, params: dict | None = None, rid: int = 1) -> dict:
    """Send a single JSON-RPC request to the MCP server."""
    params = params or {}
    req = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

    proc = subprocess.Popen(
        ["python3", str(MCP_SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout_data, stderr = proc.communicate(input=req + "\n", timeout=5)

    return json.loads(stdout_data)


def test_mcp_initialize(temp_db_path: Path):
    """Server responds with capabilities."""
    resp = mcp_call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    assert "result" in resp
    assert resp["result"]["serverInfo"]["name"] == "lcm"
    assert "tools" in resp["result"]["capabilities"]


def test_mcp_tools_list(temp_db_path: Path):
    """Server advertises all 4 tools."""
    resp = mcp_call("tools/list")
    tools = resp["result"]["tools"]
    tool_names = {t["name"] for t in tools}
    assert tool_names == {"lcm_search", "lcm_promote", "lcm_summaries", "lcm_stats"}

    # Each tool has inputSchema
    for t in tools:
        assert "inputSchema" in t
        assert "description" in t


def test_mcp_stats_empty(temp_db_path: Path):
    """Stats on fresh DB."""
    resp = mcp_call("tools/call", {"name": "lcm_stats", "arguments": {}})
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["db_exists"] is True
    assert content["messages"] == 0
    assert content["fts_enabled"] is True


def test_mcp_promote_and_search(temp_db_path: Path):
    """Promote an insight, then search for it."""
    # Promote
    resp = mcp_call("tools/call", {
        "name": "lcm_promote",
        "arguments": {"content": "PostgreSQL uses MVCC for concurrency control.", "tags": ["database", "architecture"]},
    })
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["confidence"] == 1.0
    assert "PostgreSQL" in content["content"]

    # Search
    resp = mcp_call("tools/call", {
        "name": "lcm_search",
        "arguments": {"query": "MVCC"},
    })
    results = json.loads(resp["result"]["content"][0]["text"])
    assert len(results) == 1
    assert results[0]["source"] == "promoted"
    assert "MVCC" in results[0]["snippet"]


def test_mcp_search_with_messages(seeded_db):
    """Search finds messages through FTS5."""
    db_path = seeded_db.execute("PRAGMA database_list").fetchone()["file"]
    import os
    os.environ["LFRANG_LCM_DB_PATH"] = db_path

    resp = mcp_call("tools/call", {
        "name": "lcm_search",
        "arguments": {"query": "asyncio"},
    })
    results = json.loads(resp["result"]["content"][0]["text"])
    assert len(results) > 0
    assert any("asyncio" in r["snippet"] for r in results)


def test_mcp_summaries_with_data(seeded_db):
    """Summaries tool returns DAG nodes."""
    db_path = seeded_db.execute("PRAGMA database_list").fetchone()["file"]
    import os
    os.environ["LFRANG_LCM_DB_PATH"] = db_path

    resp = mcp_call("tools/call", {
        "name": "lcm_summaries",
        "arguments": {"session_id": "s1"},
    })
    results = json.loads(resp["result"]["content"][0]["text"])
    assert len(results) >= 2
    assert results[0]["session_id"] == "s1"
    assert "depth" in results[0]


def test_mcp_unknown_tool(temp_db_path: Path):
    """Unknown tool returns error."""
    resp = mcp_call("tools/call", {"name": "nonexistent", "arguments": {}})
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_mcp_stats_with_populated_db(seeded_db):
    """Stats reflect actual data."""
    db_path = seeded_db.execute("PRAGMA database_list").fetchone()["file"]
    import os
    os.environ["LFRANG_LCM_DB_PATH"] = db_path

    resp = mcp_call("tools/call", {"name": "lcm_stats", "arguments": {}})
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["messages"] == 6
    assert content["sessions"] == 2
    assert content["promoted"] == 1
    assert content["summaries"] == 2
