# Memory System

← [Back to README](../README.md)

Agents need to remember context between conversations. Robyx has a two-tier memory system:

## Active Memory

A compact document (~5000 words max) loaded into the agent's context at the start of every conversation. Contains:

- Current state of the project/task
- Active decisions and the reasoning behind them
- Open TODOs
- Known issues and gotchas

Agents update their active memory **continuously** — not at session boundaries. A decision is made? Write it now. A TODO is completed? Update immediately.

## Archive

When information becomes obsolete (completed TODO, superseded decision), agents move it from active memory to a quarterly archive file. The archive is not loaded by default — it's queryable on demand when historical context is needed.

## Integration with existing projects

Robyx respects your existing setup:

| Project state | Memory behavior |
|---------------|-----------------|
| Has Claude Code memory (`.claude/`, `CLAUDE.md`) | Robyx doesn't interfere — native memory works as-is |
| No existing memory | Robyx creates `.robyx/memory/` with active + archive |
| Robyx and specialists | Always use `data/memory/{name}/` |

This means you can work on a project **both directly** (terminal + Claude Code) **and via Robyx** (chat) without memory conflicts.

---

← [Back to README](../README.md)
