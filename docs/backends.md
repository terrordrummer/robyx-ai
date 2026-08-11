# AI Backends

← [Back to README](../README.md)

Robyx is a thin orchestration layer on top of CLI-based AI tools:

| Backend | CLI | Sessions | Streaming | Config |
|---------|-----|:--------:|:---------:|--------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `claude` | Yes | Yes | `AI_BACKEND=claude` |
| [Codex CLI](https://github.com/openai/codex) | `codex` | — | — | `AI_BACKEND=codex` |
| [OpenCode](https://github.com/opencode-ai/opencode) | `opencode` | Yes | — | `AI_BACKEND=opencode` |

Adding a new backend is one class in [`ai_backend.py`](../bot/ai_backend.py) — implement `build_command()` and `parse_response()`.

When using Claude Code, responses are **streamed in real-time**. Agents can emit `[STATUS ...]` markers that appear instantly in chat, so you see progress instead of just "typing...".

## Autonomous-by-default permissions

Robyx ships every backend with the most permissive, non-interactive execution policy, since agents run headless and cannot answer approval prompts:

- **Claude Code** — `--permission-mode bypassPermissions`. Override with `CLAUDE_PERMISSION_MODE`.
- **Codex** — invoked as `codex exec` (Codex CLI 0.124+) with `-c approval_policy="never" --sandbox danger-full-access`. Override with `CODEX_APPROVAL_POLICY` / `CODEX_SANDBOX`.
- **OpenCode** — a managed `opencode-managed.json` with `"permission": "allow"` is created lazily for executive/system/scheduler work and passed via the AI child process's `OPENCODE_CONFIG`. Robyx never exports that path process-wide. Override with `OPENCODE_PERMISSION` (or set `OPENCODE_CONFIG` explicitly to point at your own config).

This is **intentionally unsafe**: agents can read/write anywhere on the disk and run any shell command. If you need stricter isolation, flip the relevant env var. On Linux systems with enterprise MDM that sets `permissions.disableBypassPermissionsMode: disable`, Claude will enforce the restriction regardless of what Robyx asks for.

### Collaborative participant boundary

The autonomous defaults above apply to owner/operator, system, scheduled, and
continuous work. Collaborative `participant` turns are different: Robyx derives
a typed execution profile from the persisted role before starting a CLI and
forces a stateless, backend-native read-only policy. Participant responses never
enter the state-changing macro dispatch path.

- Claude receives only `Read`, `Glob`, and `Grep`; safe mode and an empty strict
  MCP configuration disable hooks, plugins, shell, edit, web, and subagents.
- Codex ignores user config/rules, disables integration features and web search,
  uses an empty tool environment, and runs in the `read-only` sandbox. Its
  offline feature inventory is allowlisted: an unknown default-enabled feature
  makes the participant lane unavailable until it is explicitly reviewed.
- OpenCode receives a child-specific deny-by-default config; only built-in
  read/navigation permissions are enabled. Executive and scheduler invocations
  also receive their selected config through their own child environment.

Robyx checks required CLI flags offline and refuses a participant turn if the
installed backend cannot prove the profile. It never falls back to autonomous
execution. Participant AI is disabled by default; set
`COLLAB_PARTICIPANT_POLICY=read-only` explicitly to opt in.

This is an integrity boundary, not a confidentiality boundary: a read-only
participant may see files readable in the workspace. Do not enable it for a
user who must not see that content.

OpenCode runs with `--format json` and resumes its native session via `--session ses_…` so multi-turn conversations stay coherent across messages and bot restarts. Robyx captures the session id from the CLI output on the first turn and replays it automatically on every subsequent turn.

## Model preferences (`models.yaml`)

Workspaces, specialists, and scheduled tasks express their model intent as a **semantic alias** (`fast` / `balanced` / `powerful`) or as a **role** (`orchestrator` / `workspace` / `specialist` / `scheduled` / `one-shot`). Robyx resolves the alias at invocation time into the concrete model id understood by the active backend, using the table at the repo root in [`models.yaml`](../models.yaml):

```yaml
defaults:
  orchestrator: balanced
  workspace: balanced
  specialist: powerful
  scheduled: fast
  one-shot: fast

aliases:
  fast:
    claude: haiku
    codex: gpt-5-mini
    opencode: openai/gpt-5-mini
  balanced:
    claude: sonnet
    codex: gpt-5
    opencode: openai/gpt-5
  powerful:
    claude: opus
    codex: gpt-5.4
    opencode: openai/gpt-5.4
```

This is especially useful with `opencode`, which requires provider-qualified names like `openai/gpt-5`. With `models.yaml` you write `model="balanced"` once in `data/tasks.md` and the right id reaches the right backend.

If `models.yaml` is missing, Robyx falls back to the legacy `AI_MODEL_DEFAULTS` / `AI_MODEL_ALIASES` env vars (JSON-encoded), then to the hard-coded defaults baked into `bot/config.py`. Old `data/tasks.md` rows that still say `haiku` / `sonnet` / `opus` keep working — those are silently mapped onto `fast` / `balanced` / `powerful` by the resolver. Power users can also pass an explicit backend model id (e.g. `model="openai/gpt-5.4-preview"`) and Robyx will pass it through unchanged.

---

← [Back to README](../README.md)
