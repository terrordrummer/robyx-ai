"""Typed execution-authority contract for AI CLI invocations.

The collaborative role is resolved by application code before an AI process is
started.  These types carry that decision through the invocation stack so a
model-emitted tag can never grant itself additional capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExecutionProfile(str, Enum):
    """Fixed capability profiles understood by every supported backend."""

    SYSTEM = "system"
    EXECUTIVE = "executive"
    PARTICIPANT_READ_ONLY = "participant-read-only"


@dataclass(frozen=True)
class InvocationSecurityContext:
    """Application-authoritative identity and capability decision."""

    profile: ExecutionProfile
    actor_id: int | str | None = None
    role: str | None = None
    collab_workspace_id: str | None = None

    @property
    def is_participant(self) -> bool:
        return self.profile == ExecutionProfile.PARTICIPANT_READ_ONLY

    @property
    def may_dispatch_side_effects(self) -> bool:
        return self.profile in (ExecutionProfile.SYSTEM, ExecutionProfile.EXECUTIVE)


SYSTEM_INVOCATION = InvocationSecurityContext(profile=ExecutionProfile.SYSTEM)
EXECUTIVE_INVOCATION = InvocationSecurityContext(profile=ExecutionProfile.EXECUTIVE)


@dataclass(frozen=True)
class BackendInvocation:
    """A backend command plus child-only environment and session policy."""

    argv: list[str]
    env_overrides: dict[str, str] = field(default_factory=dict)
    persist_session: bool = True


class UnsupportedExecutionProfile(RuntimeError):
    """Raised when a backend cannot prove the requested profile is safe."""
