"""Tests for [COLLAB_ANNOUNCE ...] orchestrator command (feature 003-external-group-wiring)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_invoke import COLLAB_ANNOUNCE_PATTERN, parse_collab_attrs
from collaborative import CollabStore
from handlers import make_handlers


@pytest.fixture
def store(tmp_path):
    return CollabStore(path=tmp_path / "data" / "collab.json")


@pytest.fixture
def handlers(agent_manager, claude_backend, store):
    return make_handlers(agent_manager, claude_backend, store)


@pytest.fixture
def hq_platform(mock_platform):
    # Default mock_platform already treats thread_id=None as main thread.
    return mock_platform


class TestParser:
    def test_matches_full_attribute_set(self):
        text = '[COLLAB_ANNOUNCE name="nebula" display="Nebula" purpose="x" inherit="astro" inherit_memory="true"]'
        m = COLLAB_ANNOUNCE_PATTERN.search(text)
        assert m is not None
        attrs = parse_collab_attrs(m.group(1))
        assert attrs == {
            "name": "nebula",
            "display": "Nebula",
            "purpose": "x",
            "inherit": "astro",
            "inherit_memory": "true",
        }

    def test_attribute_order_free(self):
        text = '[COLLAB_ANNOUNCE purpose="x" name="nebula" inherit_memory="false" display="N" inherit=""]'
        m = COLLAB_ANNOUNCE_PATTERN.search(text)
        attrs = parse_collab_attrs(m.group(1))
        assert attrs["name"] == "nebula"
        assert attrs["inherit"] == ""
        assert attrs["inherit_memory"] == "false"


class TestHandleCollabAnnounce:
    @pytest.mark.asyncio
    async def test_creates_pending_and_writes_agent_file(
        self, handlers, store, hq_platform, tmp_path,
    ):
        from config import AGENTS_DIR
        response = (
            "Got it, will prepare nebula.\n\n"
            '[COLLAB_ANNOUNCE name="nebula" display="Nebula Research" '
            'purpose="Collab with Alice and Bob" inherit="astro-research" '
            'inherit_memory="true"]'
        )
        out = await handlers["_handle_collab_announce"](
            response, chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        # pending record persisted
        pending = store.list_pending_for_creator(12345)  # OWNER_ID from conftest
        assert len(pending) == 1
        ws = pending[0]
        assert ws.name == "nebula"
        assert ws.display_name == "Nebula Research"
        assert ws.parent_workspace == "astro-research"
        assert ws.inherit_memory is True

        # agent file written with purpose
        agent_file = AGENTS_DIR / "nebula.md"
        assert agent_file.exists()
        content = agent_file.read_text()
        assert "Collab with Alice and Bob" in content
        assert "astro-research" in content

        # confirmation trailer in response
        assert "[COLLAB_ANNOUNCE ok: name=nebula]" in out
        # original marker stripped
        assert "[COLLAB_ANNOUNCE name=" not in out

    @pytest.mark.asyncio
    async def test_missing_purpose_rejected(
        self, handlers, store, hq_platform,
    ):
        response = '[COLLAB_ANNOUNCE name="x" display="X" inherit="" inherit_memory="true"]'
        out = await handlers["_handle_collab_announce"](
            response, chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        assert "error" in out
        assert "missing required attribute" in out
        assert store.list_pending_for_creator(12345) == []

    @pytest.mark.asyncio
    async def test_collision_reports_error(
        self, handlers, store, hq_platform,
    ):
        response = (
            '[COLLAB_ANNOUNCE name="nebula" display="Nebula" purpose="first" '
            'inherit="" inherit_memory="true"]'
        )
        await handlers["_handle_collab_announce"](
            response, chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        # Same name again should collide.
        out = await handlers["_handle_collab_announce"](
            response, chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        assert "error" in out
        assert "collision" in out

    @pytest.mark.asyncio
    async def test_non_main_thread_rejected(
        self, handlers, store, hq_platform,
    ):
        # Simulate being in a forum topic (not HQ main thread).
        hq_platform.is_main_thread = MagicMock(return_value=False)
        response = (
            '[COLLAB_ANNOUNCE name="x" display="X" purpose="p" '
            'inherit="" inherit_memory="true"]'
        )
        out = await handlers["_handle_collab_announce"](
            response, chat_id=-100999, platform=hq_platform, thread_id=42,
        )
        assert "rejected" in out
        assert "not authorised" in out
        assert store.list_pending_for_creator(12345) == []

    @pytest.mark.asyncio
    async def test_no_marker_returns_response_unchanged(
        self, handlers, hq_platform,
    ):
        response = "Just a regular reply with no markers."
        out = await handlers["_handle_collab_announce"](
            response, chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        assert out == response

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_name", [
        "../../etc/passwd",
        "../evil",
        "/absolute",
        "sub/dir",
        "sub\\dir",
        "UPPERCASE",
        "with space",
        "with.dot",
        "with_underscore",
        "-leading-hyphen",
        "",
        ".",
        "..",
    ])
    async def test_rejects_path_traversal_or_invalid_name(
        self, handlers, store, hq_platform, tmp_path, bad_name,
    ):
        """P2-81 / T078a: AI-emitted ``name`` attributes must be
        rejected by ``validate_collab_name`` BEFORE any agent-file write.
        A traversal-crafted name must not touch AGENTS_DIR at all, nor
        create a pending record, nor appear in the outgoing trailer."""
        from config import AGENTS_DIR
        response = (
            '[COLLAB_ANNOUNCE name="%s" display="X" purpose="p" '
            'inherit="" inherit_memory="true"]' % bad_name
        )
        out = await handlers["_handle_collab_announce"](
            response, chat_id=-100999, platform=hq_platform, thread_id=None,
        )
        # Invalid-name error trailer surfaced.
        assert "error" in out
        assert "invalid name" in out or "missing required attribute" in out, (
            "expected either invalid-name or missing-attr error for %r" % bad_name
        )
        # NO pending record persisted.
        assert store.list_pending_for_creator(12345) == []
        # NO stray .md file under AGENTS_DIR from this announce.
        for f in AGENTS_DIR.glob("*.md"):
            assert bad_name.strip("./") not in f.stem, (
                "path-traversal name %r leaked into %s" % (bad_name, f)
            )


class TestFlowAUsesPreAnnouncedPurpose:
    @pytest.mark.asyncio
    async def test_match_welcomes_with_purpose_and_notifies_hq(
        self, handlers, store, mock_platform,
    ):
        # Arrange: announce "nebula" via the handler path (produces the
        # pending record + seed agent file).
        await handlers["_handle_collab_announce"](
            '[COLLAB_ANNOUNCE name="nebula" display="Nebula Research" '
            'purpose="Collab on Nebula with Alice and Bob" '
            'inherit="astro-research" inherit_memory="true"]',
            chat_id=-100999, platform=mock_platform, thread_id=None,
        )
        assert len(store.list_pending_for_creator(12345)) == 1

        mock_platform.send_message = AsyncMock()
        mock_platform.get_invite_link = AsyncMock(return_value=None)

        # Act: simulate the bot being added to the group by the owner.
        chat = MagicMock()
        chat.id = -100777
        chat.title = "Nebula Research"
        added_by = MagicMock()
        added_by.id = 12345  # OWNER_ID from conftest
        await handlers["collab_bot_added"](mock_platform, chat, added_by)

        # Assert: in-group welcome references the pre-announced purpose.
        sends = mock_platform.send_message.call_args_list
        group_sends = [c for c in sends if c.kwargs.get("chat_id") == -100777]
        assert group_sends, "No message sent to the group"
        group_text = group_sends[0].kwargs["text"]
        assert "Collab on Nebula with Alice and Bob" in group_text

        # HQ notification also references the purpose.
        hq_sends = [c for c in sends if c.kwargs.get("chat_id") == -100999]
        assert hq_sends, "No HQ notification sent"
        hq_text = hq_sends[0].kwargs["text"]
        assert "Collab on Nebula with Alice and Bob" in hq_text
        assert "Nebula Research" in hq_text

        # Workspace transitioned pending → active.
        assert store.get_by_chat_id(-100777) is not None
        assert store.get_by_chat_id(-100777).status == "active"
