# Architecture

← [Back to README](../README.md)

## How It Works

You talk to **Robyx** in **Headquarters** — the control channel where the orchestrator lives. Robyx understands your requests, creates the right agents, and coordinates everything.

```
You:   "Create a workspace to monitor BTC price every hour, alert me below 60k"
Robyx:  Creates a scheduled workspace. Agent checks price hourly,
       sends alerts to its dedicated topic/channel.

You:   "I need a code reviewer that knows our Python conventions"
Robyx:  Creates a cross-functional specialist. Available to all
       workspaces via @code-reviewer.

You:   "Remind me Thursday at 9am — dentist appointment"
Robyx:  Schedules a [REMIND] entry. The Python reminder engine fires
       at the exact minute, survives bot restarts, no LLM needed.
```

Reminders are a **universal skill**: any agent in Robyx — Robyx, workspaces, specialists, and focused-mode agents — can schedule one with the `[REMIND ...]` pattern. The bot parses the pattern, queues it into the unified `data/queue.json`, and the scheduler delivers the message at the exact time. See **Reminders** in [`ORCHESTRATOR.md`](../ORCHESTRATOR.md) for the attribute reference.

Every agent lives in its own topic/channel. You can talk to any agent directly by opening it, or use `/focus <name>` to redirect all messages to that agent.

---

## The Three Roles

Robyx has three types of agents, each with a distinct purpose:

```
                        ┌──────────────────────────┐
                        │          YOU              │
                        │      (Chat messages)      │
                        └────────────┬─────────────┘
                                     │
                                     ▼
                        ┌──────────────────────────┐
                        │        ROBYX              │
                        │  Principal Orchestrator   │
                        │  Lives in Headquarters    │
                        │  Creates & manages all    │
                        │  agents and workspaces    │
                        └──┬─────────┬──────────┬──┘
                           │         │          │
              ┌────────────▼──┐  ┌───▼────────┐ │
              │  WORKSPACE    │  │ WORKSPACE   │ │
              │  Agent        │  │ Agent       │ │
              │               │  │             │ │
              │ One channel.  │  │ One channel.│ │
              │ One job.      │  │ One job.    │ │
              │ Focused.      │  │ Focused.    │ │
              └──────┬────────┘  └──────┬──────┘ │
                     │                  │        │
                     │   ┌──────────────▼────┐   │
                     └──►│   SPECIALIST      │◄──┘
                         │   Cross-functional│
                         │                   │
                         │ Available to ALL   │
                         │ workspaces via     │
                         │ @mention           │
                         └───────────────────┘
```

### Robyx — The Orchestrator

Robyx is your single point of contact. It lives in **Headquarters** — the control channel of Robyx — and handles:

- **Creating workspaces** when you describe a task or project
- **Spawning specialists** when cross-functional expertise is needed
- **Delegating work** to the right agent
- **Managing focus** — routing your messages to the correct agent
- **Coordinating** the overall team

You never need to configure agents manually. Just describe what you need, and Robyx builds it.

**Headquarters is coordination-only.** Robyx treats the control channel as a dispatch point, not a workbench. Fleet status, workspace creation, delegation, and meta-operations belong in Headquarters; real project work (R&D iterations, builds, deploys, feature implementation) belongs in the workspace topic/channel of the project that owns it. When a request implies deep work inside a specific project, Robyx offers `[DELEGATE @agent: ...]` or suggests switching to the workspace topic/channel — it does not silently start executing from Headquarters.

### Workspace Agents — The Workers

Each workspace is its own **topic/channel** with a **dedicated AI agent**. The agent:

- Has its own conversation history (persistent sessions)
- Runs in its stored `work_dir` on your machine
- Follows custom instructions written by Robyx (or by you)
- Can request help from specialists

New workspaces inherit the configured `ROBYX_WORKSPACE` (or legacy `KAELOPS_WORKSPACE`) as their initial `work_dir`.

A workspace is not limited to a single mode — the same agent can respond interactively when you message it, run scheduled tasks on a timer, and have continuous autonomous work in progress. See [Scheduler](scheduler.md) for the full range of what agents can do.

For iterative, long-running work (R&D loops, optimization, training cycles), agents support the **agentic loop** mechanism. You can trigger it explicitly with `/loop` or let the agent suggest it when it recognizes the need from conversation context. The agent conducts a setup interview (objective, stopping criteria, constraints) before launching a structured iterative process with a dedicated topic and git branch. See [Scheduler -- Continuous Tasks](scheduler.md) for details.

### Specialists — The Experts

Specialists are **horizontal agents** that serve all workspaces. Think of them as team-wide resources:

- A **code reviewer** that any workspace can ask for a review
- A **deployer** that knows your infrastructure
- A **researcher** that can deep-dive into any topic

Any workspace agent can call a specialist with `@name`. The specialist responds in the requesting workspace's topic/channel, keeping context local.

---

## Workspaces

A workspace is the fundamental unit of Robyx. When Robyx creates one, this is what happens:

```
1. Topic/channel created           →  #btc-monitor
                                       (forum topic on Telegram,
                                        channel on Discord/Slack)
2. Agent instructions generated    →  data/agents/btc-monitor.md
3. Scheduler entry written         →  data/queue.json (one-shot/periodic/continuous)
                                       (interactive workspaces are agent-only)
4. Data directory created          →  data/btc-monitor/
5. Agent activated                 →  ready to work
```

### Lifecycle

```
    You ask Robyx ──→ [Created] ──→ [Active] ──→ [Closed]
                                      │
                                      ▼
                                   [Paused]
                                   (scheduler
                                    skips it)
```

- **Active** — agent works, responds, and maintains its state
- **Paused** — agent stops; you can resume anytime
- **Closed** — the platform topic/channel is archived or closed; the agent is removed

### Talking to Workspaces

Three ways to interact with a workspace agent:

1. **Open its topic/channel** — messages go directly to that agent
2. **@mention** — write `@agent-name do something` from any channel
3. **Focus mode** — `/focus agent-name` routes ALL your messages to that agent until you say "back to Robyx"

---

← [Back to README](../README.md)
