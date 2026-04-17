"""Lifecycle tests for the external-group wiring feature (003):
bot removal, supergroup migration, and the unauthorised-adder guard."""

from unittest.mock import AsyncMock, MagicMock, patch

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


class TestBotRemoved:
    @pytest.mark.asyncio
    async def test_close_workspace_and_notify_hq(
        self, handlers, store, active_ws, mock_platform,
    ):
        mock_platform.send_message = AsyncMock()
        chat = MagicMock()
        chat.id = -100777
        chat.title = "Nebula Research"
        await handlers["collab_bot_removed"](mock_platform, chat)

        ws = store.get("collab-nebula")
        assert ws.status == "closed"
        hq_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100999
        ]
        assert hq_sends
        assert "Nebula Research" in hq_sends[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_list_for_orchestrator_excludes_closed_after_removal(
        self, handlers, store, active_ws, mock_platform,
    ):
        chat = MagicMock()
        chat.id = -100777
        chat.title = "Nebula Research"
        await handlers["collab_bot_removed"](mock_platform, chat)
        rows = store.list_for_orchestrator()
        names = [r["name"] for r in rows]
        assert "nebula" not in names


class TestBotMigrated:
    @pytest.mark.asyncio
    async def test_rebinds_chat_id_keeps_status_active(
        self, handlers, store, active_ws, mock_platform,
    ):
        await handlers["collab_bot_migrated"](
            mock_platform, old_chat_id=-100777, new_chat_id=-1009999,
        )
        ws = store.get("collab-nebula")
        assert ws.chat_id == -1009999
        assert ws.status == "active"
        # still routable via the new chat id
        assert store.get_by_chat_id(-1009999) is not None


class TestUnauthorisedAdder:
    @pytest.mark.asyncio
    async def test_non_owner_add_gets_refusal_leave_and_hq_notice(
        self, handlers, store, mock_platform,
    ):
        mock_platform.send_message = AsyncMock()
        mock_platform.leave_chat = AsyncMock()
        chat = MagicMock()
        chat.id = -100111
        chat.title = "Stranger Group"
        added_by = MagicMock()
        added_by.id = 777777  # not owner, no role anywhere

        await handlers["collab_bot_added"](mock_platform, chat, added_by)

        # group-facing refusal
        group_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100111
        ]
        assert group_sends, "Expected refusal message in the group"
        assert "can't be added" in group_sends[0].kwargs["text"].lower()

        # leave_chat invoked
        assert mock_platform.leave_chat.await_count == 1

        # HQ notification
        hq_sends = [
            c for c in mock_platform.send_message.call_args_list
            if c.kwargs.get("chat_id") == -100999
        ]
        assert hq_sends
        assert "777777" in hq_sends[0].kwargs["text"]

        # no CollabWorkspace persisted
        assert store.list_all() == []

    @pytest.mark.asyncio
    async def test_operator_of_existing_workspace_is_authorised(
        self, handlers, store, mock_platform,
    ):
        # Seed a workspace where user 55555 is an operator.
        seed = CollabWorkspace(
            id="c-seed",
            name="seed",
            display_name="Seed",
            agent_name="seed",
            chat_id=-100002,
            status="active",
            roles={"55555": "operator"},
        )
        store.add(seed)

        mock_platform.send_message = AsyncMock()
        mock_platform.leave_chat = AsyncMock()
        mock_platform.get_invite_link = AsyncMock(return_value=None)
        chat = MagicMock()
        chat.id = -100222
        chat.title = "Operator Group"
        added_by = MagicMock()
        added_by.id = 55555

        # Intercept the AI bootstrap so Flow B doesn't hang on a real CLI.
        with patch(
            "handlers.invoke_ai", new=AsyncMock(return_value="Hi."),
        ):
            await handlers["collab_bot_added"](mock_platform, chat, added_by)

        # leave_chat NOT invoked.
        assert mock_platform.leave_chat.await_count == 0
        # workspace persisted for the new chat.
        assert store.get_by_chat_id(-100222) is not None
