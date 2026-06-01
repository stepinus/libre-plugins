#!/usr/bin/env python3
"""LCM compact hook — trigger LCM knowledge-graph compaction on context pressure.

Fires when the context window is under pressure. Calls LCM's compact via the
CLI (not direct HTTP) to compress the cross-session knowledge graph, then
passes through the original messages unchanged for LibreFang's default compaction.

Input (stdin):
  {"type": "compact", "agent_id": "...", "messages": [...], "model": "...", "context_window_tokens": 200000}
Output (stdout):
  {"type": "compact_result", "messages": [...]}  ← passthrough; LCM compaction is side-effect

Two LCM calls (both fire-and-forget):
  1. lcm compact           — batch promote-events + compact for all sessions
  2. lcm compact --hook    — compact the current session's knowledge graph
"""

import json
import os
import subprocess
import sys

LCM_BIN = os.environ.get("LCM_BIN", "lcm")


def _fire_and_forget(args: list[str], stdin_data: dict | None = None) -> None:
    """Launch LCM CLI as a detached subprocess."""
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_data else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if stdin_data:
            proc.communicate(input=json.dumps(stdin_data), timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def emit(obj: dict) -> None:
    json.dump(obj, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        emit({"type": "compact_result", "messages": []})
        return

    agent_id = data.get("agent_id", "")
    messages: list = data.get("messages", [])
    context_tokens = data.get("context_window_tokens", 200000)
    model = data.get("model", "")

    # ── 1. Batch promote-events + compact for all sessions ──────────────────
    _fire_and_forget([LCM_BIN, "compact"])

    # ── 2. Compact current session's knowledge graph ───────────────────────
    _fire_and_forget([LCM_BIN, "compact", "--hook"], {
        "session_id": agent_id,
        "cwd": os.path.abspath(os.curdir),
        "model": model,
        "context_tokens": context_tokens,
    })

    print(
        f"[lcm:compact] triggered LCM compaction (ctx={context_tokens}, model={model})",
        file=sys.stderr,
        flush=True,
    )

    # Passthrough — LCM compacts the knowledge graph, not conversation history.
    # LibreFang handles conversation compaction via its default strategy.
    emit({"type": "compact_result", "messages": messages})


if __name__ == "__main__":
    main()
