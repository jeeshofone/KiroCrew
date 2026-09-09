"""Tests for POST /api/chat/slots/{slot}/migrate-remote (context-carry v1).

The migration is a context-carry move: a new local slot is bound to the chosen
crew, a digest of the source conversation rides the pending-context channel,
and the source is archived read-only LAST. The tests here pin the guard
surface, the digest, the failure ordering (a mid-flight failure must leave the
source intact), and the archived source's read-only resume.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import (
    _MIGRATE_DIGEST_MAX_CHARS,
    _MIGRATE_DIGEST_MESSAGES,
    SlotCloseError,
    _build_migration_digest,
    api_chat_slot_migrate_remote,
    api_chat_slot_resume,
)
from kiro_crew.dashboard.remote_relay import RemoteTurnError


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/migrate-remote", api_chat_slot_migrate_remote)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    return app


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    """Every test runs as the local dashboard owner unless it opts out."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _seed_conversation(slot, turns: int = 3) -> None:
    for i in range(turns):
        slot.messages.append({"role": "user", "content": f"question {i}"})
        slot.messages.append({"role": "assistant", "content": f"answer {i}"})


class TestGuards:
    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/nope/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_missing_instance_id_is_400(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/migrate-remote", json={})
            assert resp.status == 400
            assert (await resp.json())["code"] == "migrate_instance_required"

    @pytest.mark.asyncio
    async def test_app_token_is_refused_as_404(self, tmp_path):
        """An app token gets the same 404 shape as a missing slot: no oracle."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        @web.middleware
        async def _stamp_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.append(_stamp_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        assert "s1" in state._slots  # nothing was touched

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 403
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_already_remote_is_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.executor = "remote"
        slot.instance_id = "crew-a"
        slot.remote_slot = "peer-1"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-b"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_already_remote"

    @pytest.mark.asyncio
    async def test_member_thread_is_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.mode = "member"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_member_pinned"

    @pytest.mark.asyncio
    async def test_running_turn_is_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        # ``running`` is derived from the turn task, so stage a live one.
        slot.task = MagicMock(done=lambda: False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "turn_in_flight"
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_peer_bind_failure_is_502_and_source_intact(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        monkeypatch.setattr(
            chat_handlers,
            "create_peer_slot",
            AsyncMock(side_effect=RemoteTurnError("peer unreachable")),
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "remote_bind_failed"
        assert "s1" in state._slots
        assert len(state._slots) == 1  # no orphaned new slot


class TestSuccess:
    @pytest.mark.asyncio
    async def test_migrates_context_and_archives_source(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", agent="oncall")
        _seed_conversation(slot, turns=3)
        monkeypatch.setattr(chat_handlers, "create_peer_slot", AsyncMock(return_value="peer-key-9"))
        sent = AsyncMock()
        monkeypatch.setattr(chat_handlers, "send_peer_context", sent)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["instance_id"] == "crew-a"
            new_key = data["key"]

        # The source is archived (gone from the live map); the new slot is
        # bound to the chosen crew.
        assert "s1" not in state._slots
        new_slot = state._slots[new_key]
        assert new_slot.executor == "remote"
        assert new_slot.instance_id == "crew-a"
        assert new_slot.remote_slot == "peer-key-9"
        assert new_slot.agent == "oncall"

        # The digest was delivered to the PEER's context queue — the machine
        # that runs the turn. A remote-bound local slot never runs _run_chat,
        # so an entry parked on the local mirror would never be drained; the
        # local queue must stay empty.
        sent.assert_awaited_once()
        args = sent.await_args.args
        assert args[1] == "crew-a"
        assert args[2] == "peer-key-9"
        assert "question 2" in args[3]
        assert "answer 2" in args[3]
        assert args[4] == "session-migration"
        assert list(new_slot._pending_context) == []

        # The archived source's persisted metadata points at where the work
        # went, so the History row can render the affordance after a restart.
        meta = state.conversation_log.get_metadata("dashboard:s1") or {}
        assert meta.get("closed") is True
        assert meta.get("migrated") == {
            "instance_id": "crew-a",
            "remote_key": "peer-key-9",
        }

    @pytest.mark.asyncio
    async def test_fresh_session_migrates_with_no_digest(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        monkeypatch.setattr(chat_handlers, "create_peer_slot", AsyncMock(return_value="peer-key-9"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            data = await resp.json()
            assert resp.status == 200
        new_slot = state._slots[data["key"]]
        assert list(new_slot._pending_context) == []

    @pytest.mark.asyncio
    async def test_archived_source_resume_is_read_only(self, tmp_path, monkeypatch):
        """Resuming a migrated source is refused: the session lives on the crew."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        monkeypatch.setattr(chat_handlers, "create_peer_slot", AsyncMock(return_value="peer-key-9"))
        monkeypatch.setattr(chat_handlers, "send_peer_context", AsyncMock())
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "resume_migrated"
            assert data["migrated"]["instance_id"] == "crew-a"
        assert "s1" not in state._slots  # the refusal did not reanimate it


class TestFailureOrdering:
    @pytest.mark.asyncio
    async def test_context_delivery_failure_leaves_source_unarchived(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        monkeypatch.setattr(chat_handlers, "create_peer_slot", AsyncMock(return_value="peer-key-9"))
        monkeypatch.setattr(
            chat_handlers,
            "send_peer_context",
            AsyncMock(side_effect=RemoteTurnError("the crew refused the carried context")),
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "migrate_context_failed"
        # The source is untouched and live; the half-made new slot is gone.
        assert "s1" in state._slots
        assert len(state._slots) == 1
        assert (state.conversation_log.get_metadata("dashboard:s1") or {}).get("migrated") is None

    @pytest.mark.asyncio
    async def test_archive_failure_rolls_back_the_new_slot(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        monkeypatch.setattr(chat_handlers, "create_peer_slot", AsyncMock(return_value="peer-key-9"))
        monkeypatch.setattr(chat_handlers, "send_peer_context", AsyncMock())
        monkeypatch.setattr(
            chat_handlers,
            "close_slot",
            AsyncMock(side_effect=SlotCloseError("disk full", "close_persist_failed")),
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 500
            assert (await resp.json())["code"] == "close_persist_failed"
        assert "s1" in state._slots
        assert len(state._slots) == 1
        # The speculative stamp was cleared, so a later ordinary save cannot
        # persist a pointer at a slot that was rolled back.
        assert state._slots["s1"].migrated is None


class TestDigest:
    def test_no_visible_conversation_yields_empty(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        assert _build_migration_digest(slot) == ""

    def test_recent_turns_are_rendered_with_roles(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=2)
        digest = _build_migration_digest(slot)
        assert "You: question 1" in digest
        assert "Assistant: answer 1" in digest
        assert digest.startswith("[Migrated session")

    def test_long_history_is_capped_and_noted(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        for i in range(_MIGRATE_DIGEST_MESSAGES * 2):
            slot.messages.append({"role": "user", "content": f"m{i}"})
        digest = _build_migration_digest(slot)
        assert "earlier history remains" in digest
        assert f"m{_MIGRATE_DIGEST_MESSAGES * 2 - 1}" in digest  # newest kept
        assert "m0" not in digest.replace("m0" + str(0), "")  # oldest dropped

    def test_oversized_body_is_trimmed_under_the_budget(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        big = "x" * 6000
        for _ in range(10):
            slot.messages.append({"role": "assistant", "content": big})
        digest = _build_migration_digest(slot)
        assert len(digest) <= _MIGRATE_DIGEST_MAX_CHARS
        assert "earlier history remains" in digest


class TestDeliveryLeg:
    """The Design-review clearing condition: the digest must ARRIVE in the
    crew's first turn prompt, not merely sit in a local queue. The peer here is
    a real second DashboardState serving the real /context endpoint, and the
    proof is drain_pending_context on the PEER slot yielding the framed digest
    — the exact string _run_chat prepends to the crew's next prompt."""

    @pytest.mark.asyncio
    async def test_digest_arrives_in_the_peer_turn_prompt(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_context
        from kiro_crew.dashboard.chat_runner import drain_pending_context

        # The PEER: a second gateway state where the crew's slot is an
        # ordinary LOCAL slot, served by the real context endpoint.
        peer_state = _make_state(tmp_path / "peer")
        peer_slot = peer_state.get_or_create_slot("peer-key-9")
        peer_app = web.Application()
        peer_app["state"] = peer_state
        peer_app.router.add_post("/api/chat/slots/{slot}/context", api_chat_slot_context)

        source_state = _make_state(tmp_path / "src")
        slot = source_state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=2)
        monkeypatch.setattr(chat_handlers, "create_peer_slot", AsyncMock(return_value="peer-key-9"))

        async with TestClient(TestServer(peer_app)) as peer_client:
            # Stand in for the instances tunnel only: forward the same HTTP
            # request send_peer_context makes to the peer's real endpoint.
            async def deliver_via_peer_http(state, instance_id, remote_slot, content, source):
                resp = await peer_client.post(
                    f"/api/chat/slots/{remote_slot}/context",
                    json={"content": content, "source": source, "ephemeral": False},
                )
                assert resp.status == 200, await resp.text()

            monkeypatch.setattr(chat_handlers, "send_peer_context", deliver_via_peer_http)

            async with TestClient(TestServer(_make_app(source_state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
                )
                assert resp.status == 200, await resp.text()

        # The digest sits in the PEER slot's queue and the drain — the function
        # _run_chat calls to build the next prompt's context prefix — yields it
        # framed under the migration source label.
        prefix = drain_pending_context(peer_slot)
        assert 'from "session-migration"' in prefix
        assert "question 1" in prefix
        assert "answer 1" in prefix
        # Drained means consumed: the queue is empty for the following turn.
        assert drain_pending_context(peer_slot) == ""
