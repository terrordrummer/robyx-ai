# Memory Module API Contract

**Date**: 2026-04-16
**Module**: `bot/memory.py`

## Public API (must remain stable)

The following functions are the public interface of the memory module.
All callers (`config.py`, `handlers.py`, `task_runtime.py`) depend on these signatures.
The internal implementation (file I/O vs SQLite) is hidden behind this contract.

### Detection

```python
def has_native_claude_memory(work_dir: str) -> bool
```
Check if a project has Claude Code's native memory. Unchanged.

### Path Resolution

```python
def get_memory_dir(agent_name: str, agent_type: str, work_dir: str) -> Path
```
Returns the directory containing the agent's memory store.
With SQLite, the `.db` file lives inside this directory.

### Load

```python
def load_active(agent_name: str, agent_type: str, work_dir: str) -> str
```
**Signature change**: takes agent identity instead of `memory_dir: Path`.
Returns the current active memory text. Empty string if none exists.

```python
def search_archive(
    agent_name: str, agent_type: str, work_dir: str,
    query: str, limit: int = 10
) -> list[dict]
```
**New function**. Full-text search across archived entries.
Returns list of `{"content": str, "topic": str, "created_at": str, "rank": float}`.

```python
def load_archive_index(agent_name: str, agent_type: str, work_dir: str) -> list[str]
```
**Preserved for backward compatibility**. Returns list of topic strings
available in the archive. Callers that previously listed archive filenames
now get topic tags instead.

### Save

```python
def save_active(agent_name: str, agent_type: str, work_dir: str, content: str)
```
**Signature change**: takes agent identity instead of `memory_dir: Path`.
Atomically writes the active memory snapshot.

```python
def append_archive(
    agent_name: str, agent_type: str, work_dir: str,
    entry: str, reason: str = "obsolete",
    topic: str = "", tags: str = ""
)
```
**Extended signature**: adds optional `topic` and `tags` for indexed retrieval.

### Prompt Building

```python
def build_memory_context(agent_name: str, agent_type: str, work_dir: str) -> str
```
Unchanged. Builds the memory context string injected into agent prompts.

```python
def get_memory_instructions(agent_name: str, agent_type: str, work_dir: str) -> str
```
Unchanged. Returns memory management instructions for the agent.
Instructions will reference the new search capability.

### Migration

```python
def migrate_markdown_to_sqlite(agent_name: str, agent_type: str, work_dir: str) -> bool
```
**New function**. One-time migration from markdown files to SQLite.
Called automatically on first access. Returns True if migration was performed.

## Breaking Changes

- `load_active(memory_dir: Path)` → `load_active(agent_name, agent_type, work_dir)`
- `save_active(memory_dir: Path, content)` → `save_active(agent_name, agent_type, work_dir, content)`
- `append_archive(memory_dir: Path, entry, reason)` → `append_archive(agent_name, agent_type, work_dir, entry, reason, topic, tags)`

All callers must be updated. There are 3 call sites in the codebase:
- `bot/config.py` (build_memory_context)
- `bot/handlers.py` (memory management commands)
- `bot/task_runtime.py` (scheduled task context)
