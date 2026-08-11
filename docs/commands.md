# Commands

← [Back to README](../README.md)

Telegram and Discord support the slash commands below. On Slack, use natural language in the control room instead; setup does not register native Slack slash commands.

| Command | Description |
|---------|-------------|
| `/start` | Alias of `/help` — sent automatically by Telegram when a user first opens a chat with the bot |
| `/help` | Show available commands |
| `/workspaces` | List active workspaces with status |
| `/specialists` | List cross-functional agents |
| `/status` | System overview — agents, focus, activity |
| `/focus <name\|off>` | Talk directly to an agent (bypass Robyx) |
| `/reset <name>` | Reset an agent's session (fresh conversation) |
| `/clear [name]` | Archive and reset a non-HQ workspace, specialist, or collaborative conversation |
| `/stop <task>` | Stop a continuous task from its parent workspace topic |
| `/resume <task>` | Resume a stopped continuous task from its parent workspace topic |
| `/complete <task>` | Mark a continuous task complete from its parent workspace topic |
| `/delete <task>` | Delete and archive a continuous task from its parent workspace topic |
| `/ping` | Check if the bot is alive |
| `/checkupdate` | Check for new Robyx versions |
| `/doupdate` | Apply a pending update |

Commands are just shortcuts. Most interaction is **natural language** — talk to Robyx like a colleague.

Collaborative workspace chats additionally accept `/promote`, `/demote`,
`/role`, `/roles`, `/mode`, and `/close`. Lifecycle commands are deliberately
restricted to ordinary parent workspace topics: collaborative agents cannot
dispatch cross-workspace task-control markers, even though every task now
persists a canonical cross-platform ownership scope.

---

← [Back to README](../README.md)
