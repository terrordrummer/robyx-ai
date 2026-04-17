"""Tests for [COLLAB_SEND], [NOTIFY_HQ], and orchestrator registry
injection (feature 003)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from collaborative import CollabStore, CollabWorkspace
from handlers import make_handlers


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def active_ws(store):
    ws = CollabWorkspace(
        id="collab-nebula",
        name="nebula",
        display_name="Nebula Research",
        agent_name="nebula",
        chat_id=-100777,
        status="active",
    )
    store.add(ws)
    return ws


@pytest.fixture
def handlers(agent_manager, claude_backend, store):
    return make_handlers(agent_manager, claude_backend, store)


class TestHandleCollabSend:
    @pytest.mark.asyncio
    async def test_delivers_to_active_group(
        self, handlers, active_ws, mock_platform,
    ):
        mock_platform.send_message = AsyncMock()
        response = (
            'Sent.\n[COLLAB_SEND name="nebula" text="Hello Nebula"]'
        )
        out = await handlers["_handle_collab_send"](
            response, chat_id=-100999, platform=mock_platform,
        )
        assert "[COLLAB_SEND ok: nebula]" in out
        assert "[COLLAB_SEND name=" not in out
        delivery = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100777
        ]
        assert delivery, "Expected delivery to the Nebula group"
        assert delivery[0].kwargs["text"] == "Hello Nebula"

    @pytest.mark.asyncio
    async def test_unknown_group_errors(self, handlers, mock_platform):
        response = '[COLLAB_SEND name="ghost" text="hi"]'
        out = await handlers["_handle_collab_send"](
            response, chat_id=-100999, platform=mock_platform,
        )
        assert "error" in out
        assert "unknown group ghost" in out

    @pytest.mark.asyncio
    async def test_setup_group_not_active_errors(
        self, handlers, store, mock_platform,
    ):
        ws = CollabWorkspace(
            id="collab-draft",
            name="draft",
            display_name="Draft",
            agent_name="draft",
            chat_id=-100111,
            status="setup",
        )
        store.add(ws)
        response = '[COLLAB_SEND name="draft" text="hi"]'
        out = await handlers["_handle_collab_send"](
            response, chat_id=-100999, platform=mock_platform,
        )
        assert "error" in out
        assert "not active" in out

    @pytest.mark.asyncio
    async def test_delivery_failure_surfaces_error(
        self, handlers, active_ws, mock_platform,
    ):
        mock_platform.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        response = '[COLLAB_SEND name="nebula" text="hi"]'
        out = await handlers["_handle_collab_send"](
            response, chat_id=-100999, platform=mock_platform,
        )
        assert "error" in out
        assert "delivery failed" in out


class TestHandleNotifyHQ:
    @pytest.mark.asyncio
    async def test_delivers_prefixed_message_to_control_room(
        self, handlers, active_ws, mock_platform,
    ):
        mock_platform.send_message = AsyncMock()
        response = (
            'Will do.\n[NOTIFY_HQ text="Alice confirmed plan B."]'
        )
        out = await handlers["_handle_notify_hq"](
            response, active_ws, mock_platform,
        )
        assert "[NOTIFY_HQ" not in out
        assert "Will do." in out
        sends = mock_platform.send_message.call_args_list
        hq_sends = [
            c for c in sends if c.kwargs.get("chat_id") == -100999
        ]
        assert hq_sends, "Expected delivery to HQ control room"
        body = hq_sends[0].kwargs["text"]
        assert "Nebula Research" in body
        assert "Alice confirmed plan B." in body

    @pytest.mark.asyncio
    async def test_truncates_over_2000_chars(
        self, handlers, active_ws, mock_platform,
    ):
        mock_platform.send_message = AsyncMock()
        long_text = "x" * 2500
        response = '[NOTIFY_HQ text="%s"]' % long_text
        await handlers["_handle_notify_hq"](
            response, active_ws, mock_platform,
        )
        hq_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100999
        ]
        assert hq_sends
        body = hq_sends[0].kwargs["text"]
        # body = header + truncated text + "..."
        assert body.endswith("...")
        assert len(body) < 2500

    @pytest.mark.asyncio
    async def test_stripped_by_executive_filter_on_non_executive_turn(self):
        from handlers import _strip_executive_markers
        response = 'ok.\n[NOTIFY_HQ text="leak"]'
        cleaned = _strip_executive_markers(response, "nebula")
        assert "NOTIFY_HQ" not in cleaned


class TestOrchestratorRegistryInjection:
    def test_available_groups_rendered_from_store(self, store):
        # 2 active + 1 pending + 1 closed.
        active1 = CollabWorkspace(
            id="c-a1", name="alpha", display_name="Alpha", agent_name="alpha",
            chat_id=-100001, status="active",
        )
        active2 = CollabWorkspace(
            id="c-a2", name="beta", display_name="Beta", agent_name="beta",
            chat_id=-100002, status="active",
        )
        pending = CollabWorkspace(
            id="c-p1", name="gamma", display_name="Gamma", agent_name="gamma",
            chat_id=0, status="pending",
        )
        closed = CollabWorkspace(
            id="c-c1", name="delta", display_name="Delta", agent_name="delta",
            chat_id=-100004, status="closed",
        )
        for w in (active1, active2, pending, closed):
            store.add(w)

        from ai_invoke import _render_external_groups_block, register_collab_store
        register_collab_store(store)
        try:
            rendered = _render_external_groups_block()
        finally:
            register_collab_store(None)

        assert "[AVAILABLE_EXTERNAL_GROUPS]" in rendered
        assert "alpha" in rendered
        assert "beta" in rendered
        assert "gamma" in rendered
        assert "delta" not in rendered  # closed excluded
        # status rendered for each entry
        assert "status: active" in rendered
        assert "status: pending" in rendered

    def test_empty_when_no_store_registered(self):
        from ai_invoke import _render_external_groups_block, register_collab_store
        register_collab_store(None)
        assert _render_external_groups_block() == ""

    def test_empty_when_store_has_no_workspaces(self, store):
        from ai_invoke import _render_external_groups_block, register_collab_store
        register_collab_store(store)
        try:
            assert _render_external_groups_block() == ""
        finally:
            register_collab_store(None)
