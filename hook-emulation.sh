#!/usr/bin/env bash
# =============================================================================
# LCM Hook Emulation — 3 conversation turns
# Shows exactly what data each hook receives and how it responds.
# All output logged to ./hook-emulation-output.log
# =============================================================================

LOGFILE="/Users/y.stepin/dev/libre-plugins/hook-emulation-output.log"
LCM="/Users/y.stepin/npm_global/bin/lcm"
SESSION_ID="emu-session-$(date +%s)"
CWD="/Users/y.stepin/dev/libre-plugins"
TRANSCRIPT_FILE="/tmp/lcm-emu-transcript-${SESSION_ID}.jsonl"

> "$LOGFILE"

log_header() {
  tee -a "$LOGFILE"
}
log_sep() {
  echo -e "\n$(printf '=%.0s' {1..80})" | tee -a "$LOGFILE"
}
log_run() {
  echo "" | tee -a "$LOGFILE"
  echo "[CMD] echo '$1' | $LCM $2" | tee -a "$LOGFILE"
  echo "[STDIN] $1" | tee -a "$LOGFILE"
  echo "---" | tee -a "$LOGFILE"
  echo "$1" | "$LCM" $2 2>&1 </dev/null | tee -a "$LOGFILE"
  echo "" | tee -a "$LOGFILE"
  echo "[EXIT: $?]" | tee -a "$LOGFILE"
}

# Create a minimal transcript file for session-snapshot
echo '{"role":"user","content":"test"}' > "$TRANSCRIPT_FILE"

# =============================================================================
log_sep
echo "LCM HOOK EMULATION — 3 TURNS" | log_header
echo "Session: $SESSION_ID" | log_header
echo "CWD: $CWD" | log_header
echo "LCM binary: $LCM" | log_header
echo "Daemon URL: http://127.0.0.1:3737" | log_header
echo "$(date)" | log_header
log_sep

echo ""
echo "🔍 ARCHITECTURE OVERVIEW" | log_header
echo "" | log_header
cat <<'DOC' | log_header
 ┌─────────────┐     ┌──────────────┐     ┌────────────────────┐
 │ Claude Code │────▶│  plugin.json │────▶│  node lcm.mjs      │
 │  (hooks)    │     │  hooks config │     │  (bootstrap+delegate)│
 └─────────────┘     └──────────────┘     └─────────┬──────────┘
                                                    │
                    ┌───────────────────────────────┘
                    ▼
         ┌─────────────────────┐
         │  dist/bin/lcm.js    │  ← Commander CLI
         │  dispatchHook()     │  ← reads stdin JSON
         └──────────┬──────────┘
                    │
    ┌───────────────┼───────────────┬──────────────┬──────────┐
    ▼               ▼               ▼              ▼          ▼
 PreCompact     SessionStart    SessionEnd    UserPrompt  PostTool
    │               │               │           Submit       Use
    │               │               │              │          │
    ▼               ▼               ▼              ▼          ▼
 /compact       /restore        /ingest    /prompt-search  extractors
 (daemon)       (daemon)        (daemon)    (daemon)       (local)

Each hook reads a JSON object from stdin (piped by Claude Code).
The JSON contains session_id, cwd, hook_event_name, and hook-specific fields.
The daemon is the stateful context engine (SQLite + LLM summarizer).
DOC

# =============================================================================
# TURN 1: Session start + first prompt
# =============================================================================
log_sep
echo "🟢 TURN 1: New session starts, user asks a question" | log_header
log_sep

# --- Hook 1: SessionStart ---
echo ""
echo "HOOK 1/6: SessionStart → lcm restore" | log_header
echo "Event: SessionStart fires when Claude Code opens a new session" | log_header
echo "Data flow: Claude Code → stdin JSON → dispatchHook('restore') → DaemonClient → daemon /restore" | log_header
echo "Purpose: Restore context from previous sessions + passive-capture insights" | log_header
echo "" | log_header

RESTORE_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","hook_event_name":"SessionStart"}'
log_run "$RESTORE_IN" "restore"

# --- Hook 2: UserPromptSubmit ---
echo ""
echo "HOOK 2/6: UserPromptSubmit → lcm user-prompt" | log_header
echo "Event: User submits a prompt in the Claude Code UI" | log_header
echo "Data flow: Claude Code → stdin JSON → dispatchHook('user-prompt') → DaemonClient → daemon /prompt-search" | log_header
echo "Purpose: Search promoted memory for relevant context + inject learning instruction" | log_header
echo "" | log_header

UP1_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","prompt":"How does the plugin hook system work? I want to understand the lifecycle."}'
log_run "$UP1_IN" "user-prompt"

# --- Hook 3: PostToolUse (after Read) ---
echo ""
echo "HOOK 3/6: PostToolUse → lcm post-tool (Read)" | log_header
echo "Event: After Claude Code uses a tool (Read) in response to the user" | log_header
echo "Data flow: Claude Code → stdin JSON → dispatchHook('post-tool') → extractPostToolEvents → EventsDb" | log_header
echo "Purpose: Extract passive events from tool usage (file reads, commands, decisions)" | log_header
echo "" | log_header

PT1_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","tool_name":"Read","tool_input":{"file_path":"/Users/y.stepin/dev/libre-plugins/claude-plugin_example/plugin.json"}}'
log_run "$PT1_IN" "post-tool"

# =============================================================================
# TURN 2: Context fills, compaction needed
# =============================================================================
log_sep
echo "🟡 TURN 2: User continues working, context window fills up" | log_header
log_sep

# --- Hook 4: PreCompact ---
echo ""
echo "HOOK 4/6: PreCompact → lcm compact --hook" | log_header
echo "Event: Claude Code detects context window is nearly full (85%+ token usage)" | log_header
echo "Data flow: Claude Code → stdin JSON → dispatchHook('compact') → DaemonClient → daemon /compact" | log_header
echo "Purpose: Summarize conversation into DAG nodes, replace native compaction" | log_header
echo "Note: The daemon /compact endpoint processes current session messages" | log_header
echo "" | log_header

COMPACT_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","hook_event_name":"PreCompact"}'
log_run "$COMPACT_IN" "compact --hook"

# --- Hook 5: PostToolUse (after Bash/git commit) ---
echo ""
echo "HOOK 5/6: PostToolUse → lcm post-tool (Bash+git)" | log_header
echo "Event: Claude Code runs a git commit command" | log_header
echo "Data flow: Claude Code → stdin JSON → dispatchHook('post-tool') → extractPostToolEvents → EventsDb" | log_header
echo "Purpose: Capture git operations as passive events (priority 2)" | log_header
echo "" | log_header

PT2_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","tool_name":"Bash","tool_input":{"command":"git commit -m \"feat: add hook lifecycle documentation\""}}'
log_run "$PT2_IN" "post-tool"

# Also — PostToolUse after a failed command (error extraction)
echo ""
echo "HOOK 5b/6: PostToolUse → lcm post-tool (Bash error)" | log_header
echo "Purpose: Extract error events (priority 1 — highest)" | log_header
echo "" | log_header

PT3_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","tool_name":"Bash","tool_input":{"command":"npm install nonexistent-pkg-xyz"},"tool_output":{"isError":true}}'
log_run "$PT3_IN" "post-tool"

# =============================================================================
# TURN 3: Stop hook fires (rolling snapshot), then session ends
# =============================================================================
log_sep
echo "🔴 TURN 3: SessionStop fires (periodic snapshot), then session ends" | log_header
log_sep

# --- Hook: Stop (session-snapshot, rolling ingest) ---
echo ""
echo "HOOK 6a/6: Stop → lcm session-snapshot" | log_header
echo "Event: Claude Code fires Stop hook periodically during session" | log_header
echo "Data flow: Claude Code → stdin JSON → dispatchHook('session-snapshot') → daemon /ingest" | log_header
echo "Purpose: Periodically ingest transcript deltas into the daemon" | log_header
echo "Note: Throttled — only fires if >60s since last snapshot" | log_header
echo "" | log_header

SNAP_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","transcript_path":"'"$TRANSCRIPT_FILE"'"}'
log_run "$SNAP_IN" "session-snapshot"

# --- Hook 6: SessionEnd ---
echo ""
echo "HOOK 6b/6: SessionEnd → lcm session-end" | log_header
echo "Event: Claude Code session ends (user closes tab or exits)" | log_header
echo "Data flow: Claude Code → stdin JSON → dispatchHook('session-end') → DaemonClient → daemon /ingest + /compact + /promote" | log_header
echo "Purpose: Final ingest, auto-compact if token threshold met, promote insights" | log_header
echo "" | log_header

END_IN='{"session_id":"'"$SESSION_ID"'","cwd":"'"$CWD"'","hook_event_name":"SessionEnd"}'
log_run "$END_IN" "session-end"

# =============================================================================
# SUMMARY
# =============================================================================
log_sep
echo "" | log_header
echo "🏁 EMULATION COMPLETE" | log_header
echo "$(date)" | log_header
echo "" | log_header
echo "Full lifecycle emulated:" | log_header
echo "" | log_header

cat <<'TABLE' | log_header
 ┌──────────────┬──────────────────────┬───────────────────────┬───────────────────────────────────────────┐
 │ Hook Event   │ LCM CLI Command      │ Daemon Endpoint       │ What It Does                             │
 ├──────────────┼──────────────────────┼───────────────────────┼───────────────────────────────────────────┤
 │ SessionStart │ lcm restore          │ POST /restore         │ Restore prior context + insights         │
 │ UserPrompt-  │ lcm user-prompt      │ POST /prompt-search   │ Search memory for relevant hints         │
 │   Submit     │                      │                       │                                           │
 │ PostToolUse  │ lcm post-tool        │ (local EventsDb)      │ Extract passive events from tool calls   │
 │ PreCompact   │ lcm compact --hook   │ POST /compact         │ DAG summarization → returns summary      │
 │ Stop         │ lcm session-snapshot │ POST /ingest          │ Rolling transcript ingest (throttled)    │
 │ SessionEnd   │ lcm session-end      │ POST /ingest +compact │ Final ingest, auto-compact, promote      │
 └──────────────┴──────────────────────┴───────────────────────┴───────────────────────────────────────────┘
TABLE

echo "" | log_header
echo "STDIN CONTRACTS (what Claude Code pipes to each hook):" | log_header
echo "" | log_header

cat <<'CONTRACTS' | log_header
  restore:
    { "session_id": "<uuid>", "cwd": "/path/to/project", "hook_event_name": "SessionStart" }

  user-prompt:
    { "session_id": "<uuid>", "cwd": "/path/to/project", "prompt": "user's actual message text" }

  post-tool:
    { "session_id": "<uuid>", "cwd": "/path/to/project", "tool_name": "Read|Write|Bash|...",
      "tool_input": {...}, "tool_response": "...", "tool_output": {"isError": bool} }

  compact --hook:
    { "session_id": "<uuid>", "cwd": "/path/to/project", "hook_event_name": "PreCompact" }

  session-snapshot:
    { "session_id": "<uuid>", "cwd": "/path/to/project", "transcript_path": "/path/to/session.jsonl" }

  session-end:
    { "session_id": "<uuid>", "cwd": "/path/to/project", "hook_event_name": "SessionEnd" }
CONTRACTS

echo "" | log_header
echo "Full log saved to: $LOGFILE" | log_header
echo "Transcript file: $TRANSCRIPT_FILE" | log_header

# Show post-tool events stored in DB
echo "" | log_header
echo "📊 Events Database (passive capture):" | log_header
EVENTS_DB=$(find /Users/y.stepin/.lossless-claude/projects -name "events.db" 2>/dev/null | head -1)
if [ -n "$EVENTS_DB" ]; then
  sqlite3 "$EVENTS_DB" "SELECT type, category, priority, data, created_at FROM events ORDER BY created_at DESC LIMIT 20;" 2>/dev/null | tee -a "$LOGFILE"
else
  echo "(No events.db found for this project yet)" | log_header
fi

echo "" | log_header
echo "Done." | log_header
