"""Chat-facing configuration command boundary.

The handler factory delegates only configuration-shaped messages here.  This
keeps parsing, redacted errors, transactional writes, and restart ordering in a
small independently-testable service while preserving the existing platform
callback contract.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from config_schema import EnvValidationError
from config_updates import (
    ConfigPreflightError,
    ConfigRollbackError,
    LocalOnlyConfigAssignmentError,
    apply_env_updates_transactionally,
    parse_direct_env_updates,
    reject_local_only_env_assignments,
)


class ConfigPlatform(Protocol):
    async def reply(
        self,
        msg_ref: Any,
        text: str,
        parse_mode: str | None = None,
    ) -> Any: ...

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        thread_id: Any = None,
        parse_mode: str | None = None,
    ) -> Any: ...


class ConfigMessage(Protocol):
    chat_id: Any
    thread_id: Any


class ConfigCommandService:
    """Handle local-only guards and owner-HQ direct configuration updates."""

    def __init__(
        self,
        *,
        project_root: Callable[[], Path],
        restart_service: Callable[[], None],
        strings: Mapping[str, str],
        logger: logging.Logger,
    ) -> None:
        self._project_root = project_root
        self._restart_service = restart_service
        self._strings = strings
        self._log = logger

    async def reject_sensitive_assignment(
        self,
        text: str,
        platform: ConfigPlatform,
        msg_ref: Any,
        *,
        include_chat_secrets: bool = False,
    ) -> bool:
        """Reply locally when a credential/authority assignment is present."""
        try:
            reject_local_only_env_assignments(
                text,
                include_chat_secrets=include_chat_secrets,
            )
        except LocalOnlyConfigAssignmentError as error:
            self._log.warning(
                "Rejected local-only configuration assignment: %s",
                error.key,
            )
            await platform.reply(
                msg_ref,
                self._strings["config_local_only"] % error.key,
                parse_mode="markdown",
            )
            return True
        return False

    async def handle_owner_update(
        self,
        text: str,
        platform: ConfigPlatform,
        msg: ConfigMessage,
        msg_ref: Any,
    ) -> bool:
        """Apply a strict direct update, or return ``False`` for ordinary text."""
        try:
            direct_updates = parse_direct_env_updates(text)
        except LocalOnlyConfigAssignmentError as error:
            self._log.warning(
                "Rejected local-only configuration assignment: %s",
                error.key,
            )
            await platform.reply(
                msg_ref,
                self._strings["config_local_only"] % error.key,
                parse_mode="markdown",
            )
            return True
        except EnvValidationError as error:
            self._log.warning(
                "Rejected direct config update for %s: %s",
                error.key,
                error.reason,
            )
            await platform.reply(
                msg_ref,
                self._strings["config_invalid"] % (error.key, error.reason),
                parse_mode="markdown",
            )
            return True

        if not direct_updates:
            return False

        keys = ", ".join("`%s`" % key for key in direct_updates)
        try:
            root = self._project_root()
            apply_env_updates_transactionally(
                root / ".env",
                direct_updates,
                project_root=root,
            )
        except ConfigRollbackError as error:
            self._log.critical(
                "Direct config update rollback failed for keys [%s]: %s",
                ", ".join(sorted(direct_updates)),
                error,
                exc_info=True,
            )
            await platform.reply(
                msg_ref,
                self._strings["config_rollback_failed"],
                parse_mode="markdown",
            )
            return True
        except ConfigPreflightError as error:
            self._log.error(
                "Direct config preflight failed for keys [%s]: %s",
                ", ".join(sorted(direct_updates)),
                error,
                exc_info=True,
            )
            await platform.reply(
                msg_ref,
                self._strings["config_preflight_failed"],
                parse_mode="markdown",
            )
            return True
        except Exception as error:
            self._log.error(
                "Direct config update failed for keys [%s]: %s",
                ", ".join(sorted(direct_updates)),
                error,
                exc_info=True,
            )
            await platform.reply(
                msg_ref,
                self._strings["config_write_failed"],
                parse_mode="markdown",
            )
            return True

        self._log.info(
            "Applied direct config update for keys: %s",
            ", ".join(sorted(direct_updates)),
        )
        await platform.reply(
            msg_ref,
            self._strings["config_updated"] % keys,
            parse_mode="markdown",
        )
        await platform.send_message(
            chat_id=msg.chat_id,
            text=self._strings["restart_pending"],
            thread_id=msg.thread_id,
            parse_mode="markdown",
        )
        self._restart_service()
        return True
