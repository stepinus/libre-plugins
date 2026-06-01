# LCM Context Plugin for LibreFang

Lossless context memory — persists every conversation turn to SQLite with FTS5 search, DAG summaries, and cross-session recall. Zero external dependencies (stdlib only).

## Architecture

Follows the claude-plugin_example hook mapping pattern. The sidecar bridges LibreFang context engine protocol events to LCM operations.

### Hook mapping

| Claude Code hook | LibreFang sidecar method | LCM operation |
|---|---|---|
| `SessionStart` | `bootstrap` | Init DB, restore context from prior sessions |
| `UserPromptSubmit` | `ingest` | FTS5 search across messages + promoted knowledge → `recalled_memories` |
| `PreCompact` | `assemble` | Window management — head/tail keep, trim/reorder. Returns trimmed window + `recovery` stage. **No LLM compaction** (compact runs in Rust, not bridged to sidecar) |
| `SessionEnd` | `after_turn` | Persist full turn transcript to `messages` table with dedup |

### Sidecar flow (primary integration)

```
Daemon ──(stdin JSON line)──▶ sidecar.py ──(stdout JSON line)──▶ Daemon
```

All methods share one SQLite database at `~/.librefang/lcm-context/lcm.db`.

### Plugin hooks (lightweight alternative)

Standalone command hooks in `hooks/`` for non-context-engine events:

| Hook file | Hook event | What it does |
|---|---|---|
| `hooks/ingest.py` | Per-user-message | Injects LCM memories into the prompt |
| `hooks/after_turn.py` | Post-turn | Persists transcript to DB |

## Quick Start

### As a sidecar context engine

Add to your LibreFang config:

```toml
[context_engine]
engine = "sidecar"

[context_engine.sidecar]
command = "python3"
args = ["path/to/librefang-lcm-plugin/sidecar.py"]
```

### As a plugin

```bash
librefang plugin install lcm-context
librefang plugin enable lcm-context --agent coder
```

## Database Schema

Three tables sharing one SQLite file:

- **messages** — every turn persisted with dedup (by `session_id` + SHA-256 content hash)
- **summaries** — DAG of summary nodes (each compaction creates a child node linked to parent)
- **promoted_knowledge** — curated reusable insights with tags and confidence scoring
- **messages_fts** — FTS5 virtual table for full-text search with auto-sync triggers

Schema is LCM-compatible (migration-safe with `@lossless-claude/lcm`).

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `LFRANG_LCM_DB_PATH` | `~/.librefang/lcm-context/lcm.db` | Database location |
| `LFRANG_LCM_HEAD_KEEP` | `2` | Messages kept at window start during compaction |
| `LFRANG_LCM_TAIL_KEEP` | `16` | Messages kept at window end during compaction |
| `LFRANG_LCM_THRESHOLD_PCT` | `0.75` | Fraction of context window that triggers compaction |
| `LFRANG_LCM_RECALL_LIMIT` | `5` | Max memories returned by `ingest` |

## How It Works

### Sidecar flow

```
Daemon ──(stdin JSON line)──▶ sidecar.py ──(stdout JSON line)──▶ Daemon
```

1. **bootstrap** (SessionStart) — Creates DB, runs migrations, enables FTS5
2. **ingest** (UserPromptSubmit) — FTS5 search across messages + promoted knowledge + summaries → `recalled_memories`
3. **assemble** (PreCompact) — If window > threshold: head/tail keep, returns trimmed window + `recovery` stage. LLM-based compaction (summary DAG, promote) runs in Rust — NOT bridged to sidecar.
4. **after_turn** (SessionEnd) — Persists all messages to `messages` table with dedup

### Hook flow

```
Daemon ──{"type":"ingest"}──▶ ingest.py      ──{"memories":[...]}──▶ Daemon
Daemon ──{"type":"after_turn"}──▶ after_turn.py ──{"type":"ok"}──────▶ Daemon
```

## Design Decisions

- **No daemon, no server** — direct SQLite with WAL mode. No subprocess management, no port binding.
- **LCM-compatible schema** — same table names, same hash method, migration-safe with `@lossless-claude/lcm`
- **Defense in depth** — every handler wraps in try/except; a DB error never breaks a turn
- **Cross-session recall** — `ingest` searches both current and past sessions by default
- **compact stays in Rust** — LLM-based summary DAG and promote knowledge run in the built-in engine (`compact` takes `Arc<dyn LlmDriver>`). Sidecar only does window management (head/tail keep + marker).
- **DAG summaries** — each Rust-side compaction creates a summary node linked to its parent, enabling `lcm_expand`-style context recovery

## Related

- [lossless-claude/lcm](https://github.com/lossless-claude/lcm) — Node.js implementation
- [Hermes LCM engine](../implements/lcm-context-engine-hermes-plugin/) — Hermes reference implementation
