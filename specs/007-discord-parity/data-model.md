# Phase 1 Data Model — Discord Parity

Extends the existing `data/collaborative_workspaces.json` schema with three
new fields and changes `chat_id` from `int` to `str`. All changes are
additive at the JSON layer; `from_dict` tolerates pre-007 records on load.
Every write is atomic (temp-file + `os.replace`).

---

## 1. CollabWorkspace (extends existing `data/collaborative_workspaces.json` records)

Existing fields (from spec 003) preserved as-is unless marked CHANGED.

| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `id` | `str` | yes | — | Stable workspace identifier (`collab-<name>`) |
| `name` | `str` | yes | — | Canonical safe name (lowercase, alphanumeric, hyphens) |
| `display_name` | `str` | yes | — | Human-readable title |
| `agent_name` | `str` | yes | — | Agent file basename in `data/agents/<name>.md` |
| `chat_id` | `str` (CHANGED from `int`) | yes | `"0"` | Canonical chat-id string per platform encoding (see §2). `"0"` = pending unbound. |
| `platform` | `str` (NEW) | yes | `"telegram"` | Platform of the chat. Back-compat default for pre-007 records. |
| `expected_platform` | `str | None` (NEW) | no | `None` | Pending workspaces refuse cross-platform binding unless `expected_platform` matches the bot-added event's platform. |
| `interaction_mode` | `str` | no | `"intelligent"` | (unchanged) |
| `parent_workspace` | `str | None` | no | `None` | (unchanged) |
| `inherit_memory` | `bool` | no | `True` | (unchanged) |
| `invite_link` | `str | None` | no | `None` | (unchanged; populated by `platform.get_invite_link` on Discord too in 007) |
| `status` | `str` | no | `"active"` | (unchanged: pending / setup / active / closed) |
| `created_at` | `float` | no | 0 | (unchanged) |
| `created_by` | `int | str` | no | 0 | (unchanged type-hint widened to accept Slack `"U..."` for spec 008) |
| `expected_creator_id` | `int | str | None` | no | `None` | (unchanged type-hint widened) |
| `roles` | `dict[str, str]` | no | `{}` | (unchanged) |

### Invariants

- `chat_id` MUST be a string in the on-disk JSON post-migration.
- `platform in ("telegram", "discord", "slack")` — Slack values appear only after spec 008.
- `chat_id == "0"` IFF `status == "pending"` (a workspace is bound IFF status is active/setup/closed).
- `expected_platform` is set only on `pending` workspaces; it is cleared (left None) on transitions to `active`.
- Legacy on-disk records with `chat_id: int` and no `platform` MUST be tolerated by `from_dict` (loader-side coercion) so the migration is purely a normalisation pass — not a hard cutover.

### Derived properties (not persisted)

- `chat_ref := ChatRef(platform=self.platform, chat_id=self.chat_id)` — convenience accessor exposed as a property on `CollabWorkspace`.
- `guild_id_or_none := self.chat_id.split(":", 1)[0] if self.platform == "discord" else None` — used by `find_active_in_guild`.

---

## 2. ChatRef (new; ephemeral dataclass in `bot/messaging/base.py`)

| Field | Type | Required | Purpose |
|---|---|---|---|
| `platform` | `str` | yes | One of `"telegram"`, `"discord"`, `"slack"`. |
| `chat_id` | `str` | yes | Canonical chat-id encoding per §3. |

Frozen, hashable, JSON-serialisable via `{"platform": ..., "chat_id": ...}`.

No equality / ordering beyond default dataclass `eq=True`.

```python
@dataclass(frozen=True)
class ChatRef:
    platform: str
    chat_id: str

    def to_dict(self) -> dict:
        return {"platform": self.platform, "chat_id": self.chat_id}

    @classmethod
    def from_dict(cls, d: dict) -> ChatRef:
        return cls(platform=d["platform"], chat_id=str(d["chat_id"]))
```

---

## 3. ChatRef canonical encodings

| Platform | `chat_id` form | Example |
|---|---|---|
| Telegram | `"<chat_id_int>"` (numeric, signed; supergroups are negative) | `"-1001234567890"` |
| Discord | `"<guild_id>:<channel_id>"` (two unsigned 64-bit ints, colon-separated, no spaces) | `"123456789012345678:987654321098765432"` |
| Slack | `"<team_id>:<channel_id>"` (two opaque slack id strings, colon-separated) — reserved for spec 008 | `"T01ABC:C02DEF"` |

Helpers in `bot/collaborative.py`:

- `parse_discord_chat_id(chat_id: str) -> tuple[int, int]` — splits and `int()`-coerces. Raises `ValueError` on malformed input.
- `make_discord_chat_id(guild_id: int, channel_id: int) -> str` — emits the canonical form.
- `parse_slack_chat_id(chat_id: str) -> tuple[str, str]` — splits; both halves remain strings.

---

## 4. LifecycleAdded / LifecycleRemoved / LifecycleMigrated (new; ephemeral dataclasses in `bot/messaging/base.py`)

```python
@dataclass(frozen=True)
class LifecycleAdded:
    chat_ref: ChatRef
    chat_title: str | None = None
    added_by_id: int | str | None = None
    added_by_name: str | None = None
    raw_event: Any = None  # opaque diagnostic carrier

@dataclass(frozen=True)
class LifecycleRemoved:
    chat_ref: ChatRef
    chat_title: str | None = None
    raw_event: Any = None

@dataclass(frozen=True)
class LifecycleMigrated:
    old_chat_ref: ChatRef
    new_chat_ref: ChatRef
    raw_event: Any = None
```

### Invariants

- `chat_ref` is always present and well-formed (the adapter validates before constructing the dataclass).
- `raw_event` carries the platform-specific event object (PTB `ChatMemberUpdated`, discord `Guild`, slack `dict`); used only for debug logging — handlers MUST NOT branch on it.
- `added_by_id` is `int` for Telegram/Discord, `str` for Slack; `None` indicates "could not resolve" (e.g. Discord audit-log failure).
- `added_by_name` is best-effort (e.g. Telegram `from_user.username`, Discord `audit_log.user.name`); never load-bearing for authorisation.

---

## 5. CollabStore — multi-platform `_chat_map`

Internal map shape change:

```python
# Before (spec 003):
self._chat_map: dict[int, str] = {}        # chat_id -> ws_id

# After (spec 007):
self._chat_map: dict[tuple[str, str], str] = {}   # (platform, chat_id) -> ws_id
```

### API additions and changes

```python
# Existing — signature changed to take ChatRef:
def get_by_chat_id(self, chat_ref: ChatRef) -> CollabWorkspace | None: ...
def update_chat_id(
    self, ws_id: str, chat_ref: ChatRef, *, expected_creator_id: int | str | None = None,
) -> bool: ...
def migrate_chat_id(self, old_ref: ChatRef, new_ref: ChatRef) -> bool: ...

# New helpers:
def find_active_in_guild(self, guild_id: str) -> list[CollabWorkspace]:
    """Discord-specific. Returns active+setup workspaces whose
    platform=="discord" and chat_id starts with f"{guild_id}:"."""

def find_active_by_platform(self, platform: str) -> list[CollabWorkspace]:
    """Forward-compat helper. Returns active+setup workspaces on a given platform."""
```

### Invariants

- `_chat_map` is rebuilt by `_rebuild_chat_map()` after every mutation. Keys are
  `(ws.platform, ws.chat_id)` for routable statuses (`active`, `setup`).
- `chat_ids` property (currently exposing `set[int]`) is retained for backwards
  compatibility but its contents now reflect `chat_id` strings; callers in
  `bot/handlers.py:_handle_collab_send` are updated to use `ChatRef` lookups.
- `pending` records (where `chat_id == "0"`) are NOT in `_chat_map`. The pending
  matcher uses `list_pending_for_creator` / `list_pending_for_agent` instead.

---

## 6. Migration `v0_28_0`

Applies once per install. Procedure:

1. Read `data/collaborative_workspaces.json` (return early if missing).
2. For each record:
   - If `"platform"` key absent → set `record["platform"] = "telegram"`.
   - If `record["chat_id"]` is `int` → set `record["chat_id"] = str(record["chat_id"])`.
3. Write atomically via `temp-file + os.replace`.
4. Set a done marker `data/migrations/v0_28_0.done` (same pattern as v0_26_0).

Idempotency: a fully migrated file finds every record already in canonical form
and writes back an identical file (or the migration runner skips on the done
marker — the first guard).

Rollback safety: a pre-007 build reading a post-007 JSON sees:
- `chat_id` as `str` — `CollabWorkspace.from_dict` in pre-007 does `chat_id=d["chat_id"]` without coercion, so the dataclass would carry a `str` where an `int` is expected. Pre-007 code paths that `int()` the chat_id would fail loudly (not silently corrupt). **In practice we mitigate by writing the migration in 0.27.2-compatible form: the migration only runs when targeting 0.28.0+, and a downgrade requires an explicit operator action to reset state.**
- `platform` and `expected_platform` — silently ignored as unknown keys by pre-007's `from_dict`. No data loss; the fields would re-appear on the next post-007 save.

---

## 7. Relationships

```
CollabWorkspace ─ 1 : 1 ─ ChatRef                    (chat_ref accessor)
CollabWorkspace ─ 1 : 1 ─ AgentFile                  (data/agents/<name>.md)
CollabStore     ─ 1 : N ─ CollabWorkspace            (in-memory + on-disk)
Platform.on_added  ─ emits ─ LifecycleAdded   → handler binds CollabWorkspace
Platform.on_removed ─ emits ─ LifecycleRemoved → handler closes CollabWorkspace
Platform.on_migrated ─ emits ─ LifecycleMigrated → handler rebinds CollabWorkspace.chat_id
```

No new indexes; lookups are bounded by workspace count (≤50 typical) and use
in-memory dict/list scans.

---

## 8. JSON schema example (post-007)

```json
{
  "collab-atlas-007": {
    "id": "collab-atlas-007",
    "name": "atlas-007",
    "display_name": "Atlas Test",
    "agent_name": "atlas-007",
    "chat_id": "123456789012345678:987654321098765432",
    "platform": "discord",
    "expected_platform": null,
    "interaction_mode": "intelligent",
    "parent_workspace": null,
    "inherit_memory": true,
    "invite_link": "https://discord.gg/abc123",
    "status": "active",
    "created_at": 1747130000.0,
    "created_by": 456789012345678901,
    "expected_creator_id": null,
    "roles": {
      "456789012345678901": "owner"
    }
  },
  "collab-legacy-telegram": {
    "id": "collab-legacy-telegram",
    "name": "legacy-telegram",
    "display_name": "Pre-007 Telegram Group",
    "agent_name": "legacy-telegram",
    "chat_id": "-1001234567890",
    "platform": "telegram",
    "expected_platform": null,
    "...": "..."
  }
}
```

Pre-007 records lacking `platform` and carrying `chat_id: int` are accepted
on load via `from_dict` coercion AND normalised to the above shape by the
`v0_28_0` migration.
