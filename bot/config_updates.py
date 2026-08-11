"""Typed, atomic and reversible ``.env`` updates from chat messages."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Mapping

from config_schema import (
    CHAT_ENV_FIELDS,
    LOCAL_ONLY_ENV_KEYS,
    SECRET_CHAT_ENV_KEYS,
    EnvValidationError,
    validate_env_updates,
)

# SECURITY: tokens, owner IDs, chat IDs and COLLAB_PARTICIPANT_POLICY are
# excluded. They must be edited locally on the server. Keep this alias for the
# public/test contract while deriving it from the one typed schema.
KNOWN_ENV_KEYS = frozenset(CHAT_ENV_FIELDS)

_DIRECT_ENV_LINE = re.compile(r"^\s*(?:set\s+)?([A-Z0-9_]+)\s*[:=]\s*(.+?)\s*$")
_KNOWN_ASSIGNMENT = re.compile(
    r"\b(?:%s)\s*[:=]" % "|".join(re.escape(key) for key in CHAT_ENV_FIELDS),
)
_ENV_UPDATE_LOCK = threading.Lock()
_ASSIGNMENT_KEY = re.compile(
    r"(?<![A-Z0-9_])[\"']?([A-Z][A-Z0-9_]*)[\"']?\s*[:=]",
)
_CREDENTIAL_KEY_SUFFIXES = (
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_API_KEY",
    "_PRIVATE_KEY",
    "_KEY",
    "PASSWORD",
)


class LocalOnlyConfigAssignmentError(ValueError):
    """A chat message contained a credential/local-authority assignment."""

    def __init__(self, key: str):
        self.key = key
        super().__init__("%s is local-only" % key)


def reject_local_only_env_assignments(
    text: str | None,
    *,
    include_chat_secrets: bool = False,
) -> None:
    """Reject assignment-shaped sensitive settings without retaining values.

    Matching is intentionally tolerant of surrounding prose so a message such
    as ``please set ROBYX_BOT_TOKEN=...`` cannot fall through to ``invoke_ai``.
    Only the key name is carried into the exception/log/reply.
    """
    if not text:
        return
    forbidden = set(LOCAL_ONLY_ENV_KEYS)
    if include_chat_secrets:
        forbidden.update(SECRET_CHAT_ENV_KEYS)
    for match in _ASSIGNMENT_KEY.finditer(text):
        key = match.group(1)
        credential_shaped = key.endswith(_CREDENTIAL_KEY_SUFFIXES)
        allowed_owner_chat_secret = (
            not include_chat_secrets and key in SECRET_CHAT_ENV_KEYS
        )
        if key in forbidden or (credential_shaped and not allowed_owner_chat_secret):
            raise LocalOnlyConfigAssignmentError(key)


class ConfigPreflightError(RuntimeError):
    """The candidate file could not boot in an isolated Python process."""


class ConfigRollbackError(RuntimeError):
    """Both candidate validation and restoration of the previous file failed."""


def parse_direct_env_updates(text: str | None) -> dict[str, str]:
    """Parse and validate strict ``KEY=value`` / ``KEY: value`` messages.

    Natural language and assignments outside the chat-editable allow-list
    return an empty mapping so normal routing remains unchanged. A syntactically
    explicit request involving a known key raises ``EnvValidationError`` on an
    invalid value; this lets the handler answer locally instead of leaking the
    message (and potentially a secret) to an AI backend.
    """
    if not text:
        return {}

    reject_local_only_env_assignments(text)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {}

    matches: list[tuple[str, str] | None] = []
    contains_known_key = bool(_KNOWN_ASSIGNMENT.search(text))
    for raw_line in lines:
        line = raw_line.lstrip("-* ").strip()
        match = _DIRECT_ENV_LINE.fullmatch(line)
        if match is None:
            matches.append(None)
            continue
        key = match.group(1)
        value = match.group(2).strip()
        matches.append((key, value))

    # A mixed/broken explicit update containing a known setting is still
    # intercepted locally. Otherwise a valid secret line plus one typo would
    # be forwarded wholesale to the AI backend.
    if any(match is None for match in matches):
        if contains_known_key:
            raise EnvValidationError(
                "configuration request",
                "every non-empty line must be a KEY=value assignment",
            )
        return {}

    if any(key not in KNOWN_ENV_KEYS for key, _value in matches if key):
        if contains_known_key:
            raise EnvValidationError(
                "configuration request",
                "contains a setting that is not chat-editable",
            )
        return {}

    updates: dict[str, str] = {}
    for match in matches:
        assert match is not None
        key, value = match
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        updates[key] = value

    return validate_env_updates(updates)


def _render_env_update(existing: bytes, updates: Mapping[str, str]) -> bytes:
    """Render a candidate while preserving unrelated lines and comments."""
    try:
        existing_text = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError("existing .env is not valid UTF-8") from exc

    existing_lines = existing_text.splitlines()
    written: set[str] = set()
    new_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue

        key, _old_value = line.split("=", 1)
        if key in updates:
            new_lines.append("%s=%s" % (key, updates[key]))
            written.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in written:
            new_lines.append("%s=%s" % (key, value))

    return ("\n".join(new_lines).rstrip("\n") + "\n").encode("utf-8")


def _fsync_directory(directory: Path) -> None:
    """Persist a directory-entry replacement where the platform supports it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Directory fsync is unsupported by some filesystems/platforms. The
        # file itself was already fsynced; keep replacement portable.
        pass
    finally:
        os.close(fd)


def write_private_env_file(env_file: Path, contents: bytes) -> None:
    """Atomically replace an env file with mode ``0600`` and durable metadata."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".%s." % env_file.name,
        suffix=".tmp",
        dir=str(env_file.parent),
    )
    temp_path = Path(temp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            # mkstemp is 0600 on POSIX. Windows has no POSIX mode/ACL
            # equivalent in the stdlib, but the atomic replacement remains
            # valid there.
            pass
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, env_file)
        _fsync_directory(env_file.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def apply_env_updates(env_file: Path, updates: Mapping[str, str]) -> None:
    """Atomically rewrite/append keys, without performing a runtime preflight.

    This is the low-level primitive retained for migrations and tests. Chat
    updates must use ``apply_env_updates_transactionally`` below.
    """
    with _ENV_UPDATE_LOCK:
        existing = env_file.read_bytes() if env_file.exists() else b""
        write_private_env_file(env_file, _render_env_update(existing, updates))


def preflight_candidate_config(
    project_root: Path,
    *,
    python_executable: str | None = None,
    timeout: float = 15.0,
) -> None:
    """Import the candidate runtime config in a fresh isolated interpreter."""
    config_file = project_root / "bot" / "config.py"
    bot_dir = config_file.parent
    if not config_file.is_file():
        raise ConfigPreflightError("runtime config module is unavailable")

    # Load config.py directly from the target tree. ``-I`` prevents ambient
    # PYTHONPATH/user-site customisation; the bot directory is inserted only
    # so config.py can resolve its sibling modules. All schema-owned keys are
    # removed before import so inherited service env cannot mask the candidate
    # values written to .env by python-dotenv.
    program = """
import importlib.util
import json
import os
import pathlib
import sys

config_file = pathlib.Path(sys.argv[1])
bot_dir = pathlib.Path(sys.argv[2])
for key in json.loads(sys.argv[3]):
    os.environ.pop(key, None)
sys.path.insert(0, str(bot_dir))
spec = importlib.util.spec_from_file_location("robyx_config_preflight", config_file)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load config module")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""
    child_env = os.environ.copy()
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [
        python_executable or sys.executable,
        "-I",
        "-c",
        program,
        str(config_file),
        str(bot_dir),
        json.dumps(sorted(KNOWN_ENV_KEYS)),
    ]
    try:
        result = _run_preflight_process(
            argv,
            cwd=project_root,
            env=child_env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigPreflightError(
            "candidate configuration could not be verified in a fresh process",
        ) from exc
    if result.returncode != 0:
        # Do not include child output: import diagnostics can contain raw env
        # values and must never echo a token into chat or logs.
        raise ConfigPreflightError(
            "candidate configuration failed the fresh-process startup check",
        )


def _terminate_preflight_process(proc: subprocess.Popen) -> None:
    """Terminate the isolated preflight group and synchronously reap it."""
    if proc.poll() is not None:
        proc.wait()
        return
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    else:  # pragma: no cover - Windows CI is not available
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc.terminate()
    try:
        proc.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    else:  # pragma: no cover
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    proc.wait()


def _run_preflight_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run the config import with cancellation-safe process-group cleanup."""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=sys.platform != "win32",
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == "win32"
            else 0
        ),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        _terminate_preflight_process(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _restore_original(
    env_file: Path,
    *,
    existed: bool,
    contents: bytes,
) -> None:
    if existed:
        # Restore bytes, never an earlier permissive mode. RR-09 makes 0600 a
        # security invariant even on the rollback path.
        write_private_env_file(env_file, contents)
        return
    try:
        env_file.unlink()
    except FileNotFoundError:
        pass
    _fsync_directory(env_file.parent)


def apply_env_updates_transactionally(
    env_file: Path,
    updates: Mapping[str, object],
    *,
    project_root: Path,
    python_executable: str | None = None,
    preflight_timeout: float = 15.0,
) -> dict[str, str]:
    """Validate, atomically install, preflight, and roll back on any failure."""
    normalised = validate_env_updates(updates)

    with _ENV_UPDATE_LOCK:
        existed = env_file.exists()
        original = env_file.read_bytes() if existed else b""
        candidate = _render_env_update(original, normalised)
        write_private_env_file(env_file, candidate)
        try:
            preflight_candidate_config(
                project_root,
                python_executable=python_executable,
                timeout=preflight_timeout,
            )
        except BaseException as preflight_error:
            try:
                _restore_original(
                    env_file,
                    existed=existed,
                    contents=original,
                )
            except BaseException as rollback_error:
                raise ConfigRollbackError(
                    "candidate failed validation and the previous .env could not be restored",
                ) from rollback_error
            if isinstance(preflight_error, ConfigPreflightError):
                raise
            if not isinstance(preflight_error, Exception):
                raise
            raise ConfigPreflightError(
                "candidate configuration failed the startup check",
            ) from preflight_error

    return normalised
