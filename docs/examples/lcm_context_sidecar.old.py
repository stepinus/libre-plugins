#!/usr/bin/env python3
"""LCM-backed context engine sidecar for LibreFang.

Bridges the LibreFang sidecar context-engine NDJSON protocol
(sidecar-context-engine.md) to the LCM (@lossless-claude/lcm) CLI
daemon.  LCM stores and retrieves long-term conversation memory —
durable insights (decisions, patterns, gotchas, solutions) — and this
sidecar makes that memory available to LibreFang agents.

The sidecar protocol delegates exactly four methods:
    bootstrap, ingest, assemble, after_turn.

Everything else (compact, subagent lifecycle, tool-result truncation)
stays in the built-in Rust engine (see sidecar-context-engine.md §
"What crosses the boundary").

Requirements:
    - LCM CLI installed and available in PATH: ``which lcm``
    - LCM daemon running on port 3737: ``lcm daemon start --detach``

Wire it up in your LibreFang config::

    [context_engine]
    engine = "sidecar"

    [context_engine.sidecar]
    command = "python3"
    args = ["/path/to/lcm_context_sidecar.py"]

Environment variables (all optional):
    LCM_BIN             – path to the ``lcm`` binary (default: ``lcm``)
    LCM_CWD             – project root passed as ``cwd`` to LCM
                           (default: os.getcwd() — the subprocess cwd)
    LCM_SNAPSHOT_SEC    – min seconds between session-snapshot calls
                           (default: 60)

Mapping: LibreFang sidecar method → LCM CLI command
    bootstrap   → lcm restore          cross-session context injection
    ingest      → lcm user-prompt      search memory for current message
    after_turn  → lcm session-snapshot rolling transcript ingest (throttled)
    assemble    → (passthrough)        context injected via ingest, not here
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import hashlib
from pathlib import Path


# ── configuration ────────────────────────────────────────────────────────────

LCM_BIN = os.environ.get("LCM_BIN", "lcm")
LCM_CWD = os.environ.get("LCM_CWD", None)  # resolved lazily
LCM_SNAPSHOT_SEC = int(os.environ.get("LCM_SNAPSHOT_SEC", "60"))  # type: ignore[arg-type]


# ── internal state ───────────────────────────────────────────────────────────

_bootstrap_context: str = ""        # cached output of lcm restore
_last_snapshot_ts: float = 0.0      # timestamp of last session-snapshot
_snapshot_lock = Path(tempfile.gettempdir()) / "lcm_sidecar_snapshots"


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_cwd() -> str:
    """Resolve working directory for LCM calls.

    The sidecar protocol does NOT include ``workspace_dir`` in params.
    Use the env var or the subprocess's own cwd.
    """
    if LCM_CWD:
        return str(Path(LCM_CWD).resolve())
    return str(Path.cwd())


def _session_id(agent_id: str | None) -> str:
    """Derive a stable session_id for LCM.

    LCM uses session_id for per-project isolation.  We derive one from
    the cwd (always) + agent_id (when available).

    ``bootstrap`` fires before any agent context exists, so agent_id
    is unavailable — the session_id falls back to a cwd-only hash.

    ``ingest`` / ``assemble`` / ``after_turn`` all carry agent_id, so
    the session_id includes it, giving per-agent isolation within the
    same project.
    """
    cwd = _resolve_cwd()
    base = f"{cwd}:{agent_id or ''}"
    return hashlib.shake_128(base.encode()).hexdigest(8)


def _call_lcm(command: str, stdin_data: dict) -> tuple[int, str]:
    """Run an LCM CLI command, pipe *stdin_data* as JSON, capture stdout.

    Returns (exit_code, stdout_text).
    """
    try:
        proc = subprocess.run(
            [LCM_BIN, command],
            input=json.dumps(stdin_data),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode, proc.stdout
    except FileNotFoundError:
        return -1, ""
    except subprocess.TimeoutExpired:
        return -2, ""
    except Exception:
        return -3, ""


# ── sidecar handlers (one per method in sidecar-context-engine.md) ────────────

def bootstrap(params: dict) -> dict:
    """Call ``lcm restore`` to load cross-session context.

    Sidecar params (from sidecar-context-engine.md)::

        { "context_window_tokens": int,
          "max_recall_results": int,
          "stable_prefix_mode": bool }

    The output of ``lcm restore`` is cached and injected as a recalled
    memory on the next ``ingest`` call.

    Returns ``{}`` (sidecar bootstrap ignores the reply).
    """
    global _bootstrap_context

    sid = _session_id(agent_id=None)
    cwd = _resolve_cwd()

    code, stdout = _call_lcm("restore", {
        "session_id": sid,
        "cwd": cwd,
    })

    if code == 0 and stdout.strip():
        _bootstrap_context = stdout.strip()
    else:
        _bootstrap_context = ""

    return {}


def ingest(params: dict) -> dict:
    """Call ``lcm user-prompt`` to search LCM memory.

    Sidecar params::

        { "agent_id": "<uuid>",
          "user_message": "<text>",
          "peer_id": "<id>" | null }

    On the first call after bootstrap, also injects the cached
    ``lcm restore`` output as a recalled memory.

    Returns::

        { "recalled_memories": [ <MemoryFragment>, … ] }
    """
    global _bootstrap_context

    agent_id = params.get("agent_id", "")
    user_message = params.get("user_message", "")
    peer_id = params.get("peer_id")  # may be null

    sid = _session_id(agent_id=agent_id)
    cwd = _resolve_cwd()

    recalled: list[dict] = []

    # ── inject bootstrap context (first call only) ────────────────────────
    if _bootstrap_context:
        tag_block = (
            f"\n<learned-insights source=\"lcm-bootstrap\">\n"
            f"{_bootstrap_context}\n"
            f"</learned-insights>\n\n"
            f"<instruction>"
            f"When you recognize a durable insight, call lcm_store with: "
            f"decision, preference, root-cause, pattern, gotcha, solution, "
            f"or workflow."
            f"</instruction>"
        )
        recalled.append({
            "tag": "lcm-bootstrap",
            "content": tag_block,
            "confidence": 0.9,
        })
        _bootstrap_context = ""

    # ── query LCM for relevant memory hints ───────────────────────────────
    if user_message:
        query_prompt = user_message
        if peer_id:
            query_prompt = f"[peer={peer_id}] {query_prompt}"

        code, stdout = _call_lcm("user-prompt", {
            "prompt": query_prompt,
            "session_id": sid,
            "cwd": cwd,
        })
        if code == 0 and stdout.strip():
            recalled.append({
                "tag": "lcm-memory",
                "content": stdout.strip(),
                "confidence": 0.8,
            })

    return {"recalled_memories": recalled}


def assemble(params: dict) -> dict:
    """Pass-through — context is injected via ``ingest`` as recalled memories.

    Sidecar params::

        { "agent_id": "<uuid>",
          "messages": [<Message>],
          "system_prompt": "<text>",
          "tools": [<ToolDefinition>],
          "context_window_tokens": int }

    LCM queries (``lcm user-prompt``) are performed in ``ingest``
    because that's where recalled memories are wired into the system
    prompt in LibreFang's architecture.

    Returns::

        { "messages": [<Message>],
          "recovery": "None" }    # ← JSON string "None", not null

    ``recovery`` lets the Rust engine know whether a recovery action
    (compaction, truncation) was applied.  Since this sidecar is a
    pure passthrough, ``recovery`` is always ``"None"``.
    """
    return {
        "messages": params.get("messages", []),
        "recovery": "None",   # serde: None variant → JSON string "None"
    }


def after_turn(params: dict) -> dict:
    """Post-turn: rolling transcript ingest via ``lcm session-snapshot``.

    Sidecar params::

        { "agent_id": "<uuid>",
          "messages": [<Message>] }

    No ``tool_calls`` or ``llm_events`` — those are plugin-hook fields,
    not part of the sidecar protocol.

    ``lcm session-snapshot`` is throttled to once every
    ``LCM_SNAPSHOT_SEC`` seconds to avoid per-turn CLI overhead.

    Returns ``{}`` (after_turn reply is ignored by the daemon).
    """
    global _last_snapshot_ts

    agent_id = params.get("agent_id", "")
    sid = _session_id(agent_id=agent_id)
    cwd = _resolve_cwd()

    # ── rolling session-snapshot (throttled) ───────────────────────────────
    now = time.time()
    if (now - _last_snapshot_ts) >= LCM_SNAPSHOT_SEC:
        messages = params.get("messages", [])
        if messages:
            _snapshot_lock.mkdir(parents=True, exist_ok=True)
            transcript_path = _snapshot_lock / f"librefang-{sid}.json"
            try:
                transcript_path.write_text(
                    json.dumps(messages, ensure_ascii=False)
                )
                _call_lcm("session-snapshot", {
                    "session_id": sid,
                    "cwd": cwd,
                    "transcript_path": str(transcript_path),
                })
                _last_snapshot_ts = now
            except OSError:
                pass  # temp write failed — skip this snapshot

    return {}


# ── NDJSON protocol loop ────────────────────────────────────────────────────

HANDLERS = {
    "bootstrap":   bootstrap,
    "ingest":      ingest,
    "assemble":    assemble,
    "after_turn":  after_turn,
}


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
        except ValueError:
            continue

        rid = req.get("id")
        method = req.get("method", "")
        handler = HANDLERS.get(method)

        if handler is None:
            reply = {"id": rid, "error": f"unknown method: {method}"}
        else:
            try:
                reply = {"id": rid, "ok": handler(req.get("params", {}))}
            except Exception as exc:
                reply = {"id": rid, "error": str(exc)}

        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
