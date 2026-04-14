# Building Your Team

← [Back to README](../README.md)

This is the core of Robyx: **you build your team through conversation**.

## Start with Robyx

Tell Robyx what you need in natural language:

```
"I need a React app workspace, a Python API workspace, and an infrastructure workspace.
Create one for each."
```

Robyx creates three workspace topics/channels and three agents with appropriate instructions. By default, each new workspace inherits the configured `ROBYX_WORKSPACE` as its starting `work_dir`; Robyx does not auto-map each workspace to a separate project directory.

## Add Specialists

As you work, you'll notice patterns — tasks that cut across projects:

```
"Create a code reviewer specialist that checks for security issues
and follows our team's Python conventions."
```

Now any workspace agent can call `@code-reviewer` when it needs a review.

## Evolve Over Time

Your team grows organically:

```
Week 1:  Robyx + 2 project workspaces
Week 2:  + code reviewer specialist
Week 3:  + system monitor (scheduled, runs every 6h)
Week 4:  + deployment specialist that knows your Cloudflare setup
Month 2: + research workspace for ML experiments
         + data pipeline monitor
```

Each agent has its own memory, its own instructions, and its own topic/channel. You interact with them like colleagues — assign tasks, ask questions, review their work.

## Why This Approach

Pre-built agent platforms give you 500 skills you didn't ask for and charge you for the complexity. Robyx gives you:

- **Zero skill bloat** — every agent does exactly what you defined
- **Your vocabulary** — agents speak your domain language because you trained them
- **Your workflow** — no adapting to someone else's idea of how work should flow
- **Full transparency** — agent instructions are markdown files you can read and edit
- **No lock-in** — swap AI backends with one env var; everything is files on disk

---

← [Back to README](../README.md)
