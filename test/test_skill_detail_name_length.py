"""Pins the name-LENGTH bound on ``GET``/``PUT``/``DELETE /api/skills/{name}``.

``POST /api/skills`` bounds the name against ``MAX_PROMPT_NAME_BYTES`` and answers
``name_too_long``. The detail route passed the URL name straight to ``load_skill``,
``update_skill`` and ``delete_skill``, whose ``_safe_name`` gate has no length
bound, so an over-long name reached a ``Path`` probe that raised ``ENAMETOOLONG``
and the client got an uncoded 500. These tests drive the real handler over a real
``SkillsLoader`` so the failure is the filesystem's own, not a fake's.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import kiro_crew.dashboard.handlers.prompts as prompts_mod
from kiro_crew.skills import SkillsLoader

BUDGET = prompts_mod.MAX_PROMPT_NAME_BYTES


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    """Run as the dashboard owner; the owner gate is covered in test_skill_write_guard.py."""
    monkeypatch.setattr(
        prompts_mod, "is_owner_dashboard_request", lambda _request: True, raising=False
    )


class _FakeRequest:
    """The slice of ``web.Request`` the detail handler reads."""

    def __init__(self, method: str, name: str, body: dict | None = None) -> None:
        self.method = method
        self.match_info = {"name": name}
        self.app = {"state": SimpleNamespace(context_builder=None)}
        self.headers: dict = {}
        self.query: dict = {}
        self.cookies: dict = {}
        self._body = body or {}

    async def json(self) -> dict:
        return self._body


@pytest.fixture
def loader(monkeypatch, tmp_path) -> SkillsLoader:
    real = SkillsLoader(tmp_path / "skills", install_builtins=False)
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: real)
    return real


async def _call(method: str, name: str) -> object:
    body = {"content": "---\ndescription: x\n---\nbody\n"} if method == "PUT" else None
    return await prompts_mod.api_skill_detail(_FakeRequest(method, name, body))


def _is_name_too_long(resp) -> bool:
    return resp.status == 400 and json.loads(resp.body)["code"] == "name_too_long"


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
class TestSkillDetailNameLength:
    @pytest.mark.asyncio
    async def test_component_over_the_filesystem_cap_is_refused(self, loader, method):
        # 300 bytes in one component: the arm where the Path probe raised ENAMETOOLONG.
        assert _is_name_too_long(await _call(method, "a" * 300))

    @pytest.mark.asyncio
    async def test_long_joined_path_with_short_components_is_refused(self, loader, method):
        # Symmetry with create: the WHOLE name is measured, not each segment, so a
        # name create would refuse is refused here too rather than probed on disk.
        assert _is_name_too_long(await _call(method, "/".join(["b" * 20] * 30)))

    @pytest.mark.asyncio
    async def test_name_of_exactly_the_budget_is_not_refused_by_length(self, loader, method):
        # Accept side of the boundary: a missing name at the budget is still a 404.
        resp = await _call(method, "d" * BUDGET)
        assert resp.status == 404


class TestSkillDetailOrdinaryNamesUnchanged:
    @pytest.mark.asyncio
    async def test_existing_skill_reads_updates_and_deletes(self, loader):
        assert loader.create_skill("my-skill", "---\ndescription: x\n---\nold\n")
        assert (await _call("GET", "my-skill")).status == 200
        assert (await _call("PUT", "my-skill")).status == 200
        assert "body" in (loader.load_skill("my-skill") or "")
        assert (await _call("DELETE", "my-skill")).status == 200
        assert loader.load_skill("my-skill") is None
