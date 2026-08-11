"""Code-enforced collaborative invocation policy (RR-03 / D-002)."""

from authorization import invocation_context_for_role
from collaborative import Role
from execution_policy import ExecutionProfile


def test_owner_and_operator_map_to_executive():
    for role in (Role.OWNER, Role.OPERATOR):
        context = invocation_context_for_role(
            role, actor_id=123, collab_workspace_id="collab-atlas",
        )
        assert context.profile == ExecutionProfile.EXECUTIVE
        assert context.may_dispatch_side_effects is True


def test_participant_maps_to_read_only_with_audit_identity():
    context = invocation_context_for_role(
        Role.PARTICIPANT,
        actor_id="U123",
        collab_workspace_id="collab-atlas",
    )
    assert context.profile == ExecutionProfile.PARTICIPANT_READ_ONLY
    assert context.is_participant is True
    assert context.may_dispatch_side_effects is False
    assert context.actor_id == "U123"
    assert context.role == "participant"
    assert context.collab_workspace_id == "collab-atlas"
