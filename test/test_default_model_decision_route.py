"""The Default Model's ``Auto (decision model)`` entry.

Saved through the config PATCH, the entry is resolved the way the chat picker's is:
``agent.model`` stores plain ``auto`` and the choice survives as
``agent.model_jev_route``, so the sentinel never reaches a model reader. A new owner
chat then starts with ``slot.jev_route`` armed, but only while the gate would ask
``model.route`` for it and no agent it runs pins a model; otherwise it starts on
plain inherit. Saving the entry is owner-only, like arming it in a chat.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_turn import decisions_hooks

SENTINEL = chat_handlers.JEV_ROUTE_MODEL


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"agent": {"model": "auto"}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch_model(cfg_path, value: str, *, owner: bool) -> int:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application()
    app["state"] = _NoOwner()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    with patch(
        "kiro_crew.dashboard.handlers.core_config_patch._hot_apply_after_write",
        _noop,
    ):
        async with TestClient(TestServer(as_owner(app) if owner else app)) as client:
            resp = await client.patch(
                "/api/config/kirocrew", json={"path": "agent.model", "value": value}
            )
            return resp.status


async def _noop() -> None:
    return None


class _NoOwner:
    owner_id = ""


def _agent_section(cfg_path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8")).get("agent", {})


@pytest.mark.asyncio
async def test_only_the_owner_saves_the_entry_and_it_is_stored_as_auto_plus_the_flag(
    tmp_config,
):
    assert await _patch_model(tmp_config, SENTINEL, owner=False) == 403
    assert _agent_section(tmp_config) == {"model": "auto"}

    assert await _patch_model(tmp_config, SENTINEL, owner=True) == 200
    assert _agent_section(tmp_config) == {"model": "auto", "model_jev_route": True}

    # Any other Default Model clears the choice, Auto included.
    assert await _patch_model(tmp_config, "auto", owner=True) == 200
    assert _agent_section(tmp_config) == {"model": "auto"}


@pytest.mark.asyncio
async def test_an_overlay_pinning_the_flag_refuses_a_default_model_save(tmp_config):
    def pins(*key_path: str) -> bool:
        return key_path == ("agent", "model_jev_route")

    with patch("kiro_crew.config.loader.overlay_pins", pins):
        assert await _patch_model(tmp_config, "model-other", owner=True) == 409
    assert _agent_section(tmp_config) == {"model": "auto"}


async def _create(
    state: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    gate_permits: bool,
    agent_pin: str = "",
) -> dict[str, Any]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    real_load = chat_handlers.KiroCrewConfig.load

    def load(*a: Any, **kw: Any) -> Any:
        cfg = real_load(*a, **kw)
        cfg.agent.model = "auto"
        cfg.agent.model_jev_route = True
        cfg.dashboard.new_chat_crew_mode = False
        return cfg

    monkeypatch.setattr(decisions_hooks, "_jev_preview_on", lambda _key: gate_permits)
    monkeypatch.setattr("kiro_crew.config.loader.agent_model_pin", lambda *_a, **_kw: agent_pin)
    with (
        patch.object(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None),
        patch.object(chat_handlers.KiroCrewConfig, "load", load),
    ):
        async with TestClient(TestServer(as_owner(app))) as client:
            resp = await client.post("/api/chat/slots", json={"name": name})
            assert resp.status < 300, await resp.text()
            return await resp.json()


@pytest.mark.asyncio
async def test_a_new_owner_chat_starts_routed_when_the_gate_permits(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    row = await _create(state, monkeypatch, name="routed-chat", gate_permits=True)
    slot = state._slots["routed-chat"]
    assert slot.jev_route is True
    # The picker entry's own state: the inherit row's ``auto``, never the sentinel.
    assert slot.model == "auto"
    assert row["jev_route"] is True


@pytest.mark.asyncio
async def test_a_refused_gate_or_an_agent_pin_leaves_the_new_chat_on_plain_inherit(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    await _create(state, monkeypatch, name="gate-off", gate_permits=False)
    await _create(
        state, monkeypatch, name="pinned-agent", gate_permits=True, agent_pin="model-pinned"
    )
    await _create(state, monkeypatch, name="control", gate_permits=True)
    for name in ("gate-off", "pinned-agent"):
        assert state._slots[name].jev_route is False, name
        assert state._slots[name].model == "", name
    # The same create with the gate open and no pin arms, so the refusals above
    # are the gate and the pin, not a create that never arms.
    assert state._slots["control"].jev_route is True
