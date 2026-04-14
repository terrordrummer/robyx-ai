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
- **Codex** — `--approval-policy never --sandbox danger-full-access`. Override with `CODEX_APPROVAL_POLICY` / `CODEX_SANDBOX`.
- **OpenCode** — managed `opencode-managed.json` config with `"permission": "allow"`, wired via `OPENCODE_CONFIG`. Override with `OPENCODE_PERMISSION` (or set `OPENCODE_CONFIG` explicitly to point at your own config).

This is **intentionally unsafe**: agents can read/write anywhere on the disk and run any shell command. If you need stricter isolation, flip the relevant env var. On Linux systems with enterprise MDM that sets `permissions.disableBypassPermissionsMode: disable`, Claude will enforce the restriction regardless of what Robyx asks for.

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
