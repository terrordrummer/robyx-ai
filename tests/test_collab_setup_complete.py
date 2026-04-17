"""Tests for [COLLAB_SETUP_COMPLETE ...] handler (feature 003)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_invoke import COLLAB_SETUP_COMPLETE_PATTERN, parse_collab_attrs
from collaborative import CollabStore, CollabWorkspace
from handlers import make_handlers


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def setup_ws(store):
    ws = CollabWorkspace(
        id="collab-photon",
        name="collab-photon",
        display_name="Photon Group",
        agent_name="collab-photon",
        chat_id=-100555,
        status="setup",
        created_by=12345,
        roles={"12345": "owner"},
    )
    store.add(ws)
    return ws


@pytest.fixture
def handlers(agent_manager, claude_backend, store):
    return make_handlers(agent_manager, claude_backend, store)


class TestParser:
    def test_parses_full_attribute_set(self):
        text = (
            '[COLLAB_SETUP_COMPLETE purpose="Photon calibration" '
            'inherit="" inherit_memory="true"]'
        )
        m = COLLAB_SETUP_COMPLETE_PATTERN.search(text)
        assert m is not None
        attrs = parse_collab_attrs(m.group(1))
        assert attrs["purpose"] == "Photon calibration"
        assert attrs["inherit"] == ""
        assert attrs["inherit_memory"] == "true"

    def test_attribute_order_free(self):
        text = (
            '[COLLAB_SETUP_COMPLETE inherit_memory="false" '
            'purpose="p" inherit="astro"]'
        )
        m = COLLAB_SETUP_COMPLETE_PATTERN.search(text)
        attrs = parse_collab_attrs(m.group(1))
        assert attrs["purpose"] == "p"
        assert attrs["inherit"] == "astro"
        assert attrs["inherit_memory"] == "false"


class TestHandleSetupComplete:
    @pytest.mark.asyncio
    async def test_flips_status_and_writes_agent_file(
        self, handlers, store, setup_ws, mock_platform, tmp_path,
    ):
        from config import AGENTS_DIR
        response = (
            "Great, we're set up.\n"
            '[COLLAB_SETUP_COMPLETE purpose="Photon calibration with Alice" '
            'inherit="astro-research" inherit_memory="true"]'
        )
        out = await handlers["_handle_collab_setup_complete"](
            response, setup_ws, mock_platform,
        )
        # marker stripped
        assert "[COLLAB_SETUP_COMPLETE" not in out
        assert "Great, we're set up." in out
        # status flipped
        assert store.get("collab-photon").status == "active"
        # agent file rewritten with purpose
        agent_file = AGENTS_DIR / "collab-photon.md"
        assert agent_file.exists()
        content = agent_file.read_text()
        assert "Photon calibration with Alice" in content
        assert "astro-research" in content
        # HQ notification sent
        sends = mock_platform.send_message.call_args_list
        hq_sends = [c for c in sends if c.kwargs.get("chat_id") == -100999]
        assert hq_sends, "Expected HQ notification"
        assert "Photon calibration with Alice" in hq_sends[0].kwargs["text"]

    @pytest.mark.asyncio
    async def test_invalid_status_logs_warning_and_strips(
        self, handlers, store, mock_platform,
    ):
        ws = CollabWorkspace(
            id="collab-active",
            name="collab-active",
            display_name="Active",
            agent_name="collab-active",
            chat_id=-100666,
            status="active",
        )
        store.add(ws)
        response = (
            'ok.\n'
            '[COLLAB_SETUP_COMPLETE purpose="x" inherit="" inherit_memory="true"]'
        )
        out = await handlers["_handle_collab_setup_complete"](
            response, ws, mock_platform,
        )
        assert "[COLLAB_SETUP_COMPLETE" not in out
        # status unchanged
        assert store.get("collab-active").status == "active"

    @pytest.mark.asyncio
    async def test_missing_purpose_drops(
        self, handlers, setup_ws, mock_platform,
    ):
        response = '[COLLAB_SETUP_COMPLETE purpose="" inherit="" inherit_memory="true"]'
        out = await handlers["_handle_collab_setup_complete"](
            response, setup_ws, mock_platform,
        )
        assert "[COLLAB_SETUP_COMPLETE" not in out

    @pytest.mark.asyncio
    async def test_ordering_agent_file_before_status_flip(
        self, handlers, store, setup_ws, mock_platform, monkeypatch,
    ):
        """If the file write fails, status MUST stay 'setup' (FR-008)."""
        from config import AGENTS_DIR

        real_mkdir = AGENTS_DIR.__class__.mkdir

        def failing_mkdir(self, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(type(AGENTS_DIR), "mkdir", failing_mkdir)

        response = (
            '[COLLAB_SETUP_COMPLETE purpose="x" inherit="" inherit_memory="true"]'
        )
        out = await handlers["_handle_collab_setup_complete"](
            response, setup_ws, mock_platform,
        )
        assert "[COLLAB_SETUP_COMPLETE" not in out
        # status NOT flipped
        assert store.get("collab-photon").status == "setup"
        # recoverable failure surfaced in the group
        sends = mock_platform.send_message.call_args_list
        group_sends = [c for c in sends if c.kwargs.get("chat_id") == -100555]
        assert group_sends, "Expected recoverable-failure note in group"


class TestFlowBBootstrapIsRealAITurn:
    @pytest.mark.asyncio
    async def test_no_hardcoded_template_string(
        self, handlers, mock_platform, agent_manager,
    ):
        """Flow B must invoke the AI backend — no byte-identical template.

        Guard: after Flow B, if any group-facing message was sent it must
        have come from `_process_and_send` (which mocks intercept), not
        from a literal string.
        """
        mock_platform.send_message = AsyncMock()
        mock_platform.get_invite_link = AsyncMock(return_value=None)

        # Intercept invoke_ai so the bootstrap call succeeds without a real CLI.
        with patch("handlers.invoke_ai", new=AsyncMock(return_value="Hi group.")):
            chat = MagicMock()
            chat.id = -100888
            chat.title = "Ad-hoc Group"
            added_by = MagicMock()
            added_by.id = 12345  # OWNER_ID from conftest
            await handlers["collab_bot_added"](mock_platform, chat, added_by)

        # Assert: no message sent with the old canned template.
        for call in mock_platform.send_message.call_args_list:
            text = call.kwargs.get("text", "")
            assert "How would you like to set up this workspace" not in text
