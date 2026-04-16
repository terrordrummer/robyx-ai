# Quickstart: Memory Engine Evolution

**Date**: 2026-04-16
**Feature**: Memory Engine Evolution

## What This Feature Does

Replaces Robyx's markdown-based agent memory system with a SQLite-backed engine
that provides full-text search, atomic writes, and crash safety — while keeping
zero external dependencies.

## Before / After

### Before (markdown)
- Active memory: read/write a flat `.md` file
- Archive: append to quarterly `.md` files
- Search: none (agent must glob and read files manually)
- Crash safety: no atomicity (partial writes possible)

### After (SQLite + FTS5)
- Active memory: single SELECT/UPDATE on `active_snapshots` table
- Archive: INSERT into `entries` table with FTS5 index
- Search: full-text queries with BM25 ranking in <10ms
- Crash safety: ACID transactions via WAL mode

## How to Verify

### 1. Active Memory Works
```
# Talk to a workspace agent
> remember that the API uses JWT tokens for auth

# Start a new conversation with the same agent
> what do you know about our auth setup?
# Agent should mention JWT tokens
```

### 2. Archive Search Works
```
# After many interactions, ask about historical decisions
> what did we decide about the deployment strategy last month?
# Agent should retrieve relevant archived entries, not dump everything
```

### 3. Migration Works
```
# On an existing installation with markdown memory:
# Restart the bot — migration runs automatically
# Verify:
#   - data/memory/robyx.db exists
#   - active.md.bak exists (backup of old file)
#   - Agent behavior unchanged
```

### 4. Crash Safety
```
# Kill the bot mid-conversation (kill -9)
# Restart
# Verify no memory corruption — agent recalls everything from before the kill
```

## What's NOT Included

- Semantic/vector search (planned Phase 2, requires sqlite-vec)
- Changes to how AI backends handle context compaction (delegated to backends)
- Multi-agent memory sharing (each agent's memory remains isolated)
