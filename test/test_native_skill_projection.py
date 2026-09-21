"""Native metadata cannot grow with the catalog behind an authored mapping."""

from __future__ import annotations

import gc
import hashlib
import json
import shutil
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import requires_symlinks
from kiro_crew.acp import skill_projection as projection
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.hooks import FileTooLargeError


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    crew_home = tmp_path / "crew"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: crew_home, raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    return home, agents, project


def test_native_view_bounds_metadata_and_preserves_original_scope(native_tree):
    home, agents, project = native_tree
    source = agents / "custom.json"
    spec = {
        "name": "custom",
        "prompt": "file://instructions.md",
        "tools": ["read", "@kirocrew-core"],
        "allowedTools": ["read"],
        "resources": ["file://RULES.md", *[f"skill://catalog/s{n}/SKILL.md" for n in range(1024)]],
    }
    original = json.dumps(spec)
    source.write_text(original, encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = prepared.agent("custom")
    view = json.loads((agents / f"{alias}.json").read_text(encoding="utf-8"))
    assert source.read_text(encoding="utf-8") == original
    assert all(not r.startswith("skill://") for r in view["resources"])
    assert len(view["resources"]) == 4
    assert view["tools"] == spec["tools"] and view["allowedTools"] == spec["allowedTools"]
    assert view["prompt"] == "file://" + (agents / "instructions.md").as_posix()
    assert iter_agent_spec_files(agents) == [source]
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings["chat.disableInheritingDefaultResources"] is True
    assert projection.prepare_native_skill_projection(project).agent("custom") == alias


def test_native_view_preserves_explicit_noninheritance_and_other_settings(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        '{"name":"custom","resources":["file://RULES.md"]}', encoding="utf-8"
    )
    settings_path = project / ".kiro/settings/cli.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": True, "toolSearch.enabled": False}),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["resources"] == ["file://RULES.md"]
    assert json.loads(settings_path.read_text(encoding="utf-8"))["toolSearch.enabled"] is False


def test_transport_keeps_original_agent_identity_and_rejects_unprepared_modes():
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    request = {"sessionId": "s", "modeId": "custom"}
    assert prepared.request("session/set_mode", request)["modeId"] == "native-alias"
    assert request["modeId"] == "custom"
    frame = prepared.frame(
        {
            "result": {
                "modes": {
                    "currentModeId": "native-alias",
                    "availableModes": [
                        {"id": "native-alias", "name": "native-alias"},
                        {"id": "unbounded-original"},
                    ],
                }
            }
        }
    )
    assert frame["result"]["modes"] == {
        "currentModeId": "custom",
        "availableModes": [{"id": "custom", "name": "custom"}],
    }
    with pytest.raises(ValueError, match="no prepared"):
        prepared.request("session/set_mode", {"modeId": "unknown"})


@pytest.mark.parametrize(
    "command", ["/agent swap custom", {"command": "agent", "args": {"value": "swap custom"}}]
)
def test_native_agent_switch_cannot_escape_crew_scope(command):
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    with pytest.raises(ValueError, match="agent selector"):
        prepared.request("_kiro.dev/commands/execute", {"command": command})


def test_custom_agent_gets_only_the_scoped_search_capability(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "tools": ["read"],
                "allowedTools": [],
                "resources": ["skill://skills/a/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["tools"] == ["read", "@kirocrew-core/skill_search"]
    assert view["allowedTools"] == []
    assert "kirocrew-core" in view["mcpServers"]
    assert "autoApprove" not in view["mcpServers"]["kirocrew-core"]


def test_global_inheritance_preference_is_refreshed(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir()
    settings.write_text('{"chat.disableInheritingDefaultResources":true}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["resources"] == []


def test_projected_search_uses_the_managed_command_and_preserves_approval(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": ["skill://skills/a/SKILL.md"],
                "mcpServers": {
                    "kirocrew-core": {"command": "other-server", "args": [], "autoApprove": []}
                },
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    entry = prepared.specs["custom"]["mcpServers"]["kirocrew-core"]
    assert entry["command"] == "test-core" and entry["autoApprove"] == []


def test_explicit_search_exclusion_fails_only_that_agent(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": ["skill://skills/a/SKILL.md"],
                "excludedTools": ["@kirocrew-core/skill_search"],
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    with pytest.raises(ValueError, match="explicitly excluded"):
        prepared.agent("custom")


def test_unmapped_custom_agent_does_not_gain_tools_or_servers(native_tree):
    _home, agents, project = native_tree
    spec = {"name": "custom", "tools": ["read"], "excludedTools": ["@kirocrew-core/skill_search"]}
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = prepared.specs["custom"]
    assert view["tools"] == ["read"]
    assert "mcpServers" not in view
    assert "custom" not in prepared.search_agents


@pytest.mark.parametrize(
    "field,value",
    [
        ("disabled", None),
        ("disabled", "false"),
        ("disabled", 0),
        ("disabledTools", None),
        ("disabledTools", "skill_search"),
        ("disabledTools", {}),
        ("disabledTools", [1]),
    ],
)
def test_invalid_core_restrictions_fail_only_the_affected_agent(
    native_tree, monkeypatch, field, value
):
    _home, agents, project = native_tree
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in ("custom", "healthy")
        ],
    )
    for name, core in (
        ("custom", {field: value}),
        ("healthy", {"disabled": False, "disabledTools": []}),
    ):
        (agents / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "resources": ["skill://skills/a/SKILL.md"],
                    "mcpServers": {"kirocrew-core": core},
                }
            ),
            encoding="utf-8",
        )
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    with pytest.raises(ValueError, match=field):
        prepared.agent("custom")
    assert (agents / f"{prepared.agent('healthy')}.json").exists()
    assert prepared.search_agents == {"healthy"}


@pytest.mark.parametrize(
    "original",
    [
        {},
        {"chat.disableInheritingDefaultResources": False},
        {"chat.disableInheritingDefaultResources": True},
        {"chat.disableInheritingDefaultResources": None},
        {"chat.disableInheritingDefaultResources": "false"},
        {"chat.disableInheritingDefaultResources": 1},
    ],
)
def test_rollback_restores_original_local_value_and_presence(native_tree, monkeypatch, original):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(original), encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    projection.prepare_native_skill_projection(project)
    current = json.loads(settings.read_text(encoding="utf-8"))
    current["toolSearch.enabled"] = False
    settings.write_text(json.dumps(current), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    restored = json.loads(settings.read_text(encoding="utf-8"))
    expected = {**original, "toolSearch.enabled": False}
    # JSON distinguishes numeric 1 from true, unlike Python dictionary equality.
    assert json.dumps(restored, sort_keys=True) == json.dumps(expected, sort_keys=True)
    # Repeated rollback does not recreate the overlay.
    before = settings.read_bytes()
    assert projection.prepare_native_skill_projection(project) is None
    assert settings.read_bytes() == before


@pytest.mark.parametrize("operator_value", [False, None, "deleted"])
def test_rollback_preserves_operator_changes(native_tree, monkeypatch, operator_value):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    settings = project / ".kiro/settings/cli.json"
    current = json.loads(settings.read_text(encoding="utf-8"))
    key = "chat.disableInheritingDefaultResources"
    if operator_value == "deleted":
        current.pop(key)
        expected = {}
    else:
        current[key] = operator_value
        expected = {key: operator_value}
    settings.write_text(json.dumps(current), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert json.loads(settings.read_text(encoding="utf-8")) == expected


def test_disabled_projection_does_not_enumerate_agents_or_create_settings(native_tree, monkeypatch):
    _home, agents, project = native_tree

    def unexpected(**kwargs):
        pytest.fail("disabled projection must not read authored agents")

    monkeypatch.setattr(projection, "list_agents", unexpected)
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert list(agents.iterdir()) == []
    assert not (project / ".kiro").exists()


@pytest.mark.parametrize(
    "source,inherited,expected",
    [
        ("global", True, {}),
        ("local", True, {"chat.disableInheritingDefaultResources": False}),
        ("local", False, {"chat.disableInheritingDefaultResources": True}),
    ],
)
def test_rollback_of_legacy_owned_overlay(native_tree, monkeypatch, source, inherited, expected):
    _home, _agents, project = native_tree
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "kirocrew.skillDiscovery.inheritFiles": inherited,
                "kirocrew.skillDiscovery.inheritSource": source,
                "chat.disableInheritingDefaultResources": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert json.loads(settings.read_text(encoding="utf-8")) == expected


def test_rollback_never_changes_unmanaged_settings(native_tree, monkeypatch):
    _home, _agents, project = native_tree
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    original = '{ "chat.disableInheritingDefaultResources": true, "other": 42 }'
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert settings.read_text(encoding="utf-8") == original


def test_running_projection_keeps_its_mode_until_restart(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    refreshed = projection.prepare_native_skill_projection(project, enabled=True)
    assert refreshed.aliases == prepared.aliases
    assert projection.prepare_native_skill_projection(project) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_client_spawn_uses_authored_agent_only_when_rolled_back(
    native_tree, monkeypatch, enabled
):
    from kiro_crew.acp import client as client_module

    home, agents, project = native_tree
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "1" if enabled else "0")
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(
        client_module, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="test-kiro")
    )
    monkeypatch.setattr(client_module, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(client_module, "require_fresh_derived_spec", lambda *args: None)
    monkeypatch.setattr(client_module, "require_fork_governance", lambda *args: None)
    monkeypatch.setattr(
        client_module, "delegated_workspace_exposes_sealed_target", lambda path: None
    )

    class StopSpawn(Exception):
        pass

    captured = []

    def stop_at_sandbox(argv, **kwargs):
        captured.extend(argv)
        raise StopSpawn

    monkeypatch.setattr(client_module, "wrap_argv", stop_at_sandbox)
    client = client_module.AcpClient(work_dir=project, agent="custom", sandbox_mode="off")
    with pytest.raises(StopSpawn):
        await client._spawn()
    assert captured[:3] == ["test-kiro", "acp", "--agent"]
    if enabled:
        assert captured[3] == client._native_skill_projection.agent("custom")
    else:
        assert captured[3] == "custom"
        assert client._native_skill_projection is None


@pytest.mark.parametrize("value", ["false", 1, False, True])
@pytest.mark.parametrize("source", ["local", "global"])
def test_only_literal_true_suppresses_inherited_instruction_files(native_tree, value, source):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = (project / ".kiro" if source == "local" else home) / "settings" / "cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": value}), encoding="utf-8"
    )
    prepared = projection.prepare_native_skill_projection(project)
    resources = prepared.specs["custom"]["resources"]
    assert any("AGENTS.md" in item for item in resources) is (value is not True)
    assert any("steering" in item for item in resources) is (value is not True)


@pytest.mark.parametrize("value", ["false", 1, False, True])
def test_global_preference_refresh_uses_only_literal_true(native_tree, value):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir()
    settings.write_text('{"chat.disableInheritingDefaultResources":true}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    assert first.specs["custom"]["resources"] == []
    settings.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": value}), encoding="utf-8"
    )
    refreshed = projection.prepare_native_skill_projection(project)
    resources = refreshed.specs["custom"]["resources"]
    assert any("AGENTS.md" in item for item in resources) is (value is not True)
    assert any("steering" in item for item in resources) is (value is not True)


# ── Managed skill-view alias lifecycle ───────────────────────────────────────


def _alias_file(agents, prepared, name="custom"):
    return agents / f"{prepared.agent(name)}.json"


def _metadata_file(agents, prepared, name="custom"):
    return agents / projection._PROJECTION_METADATA_DIR_NAME / f"{prepared.agent(name)}.json"


def test_generated_view_keeps_lifecycle_ownership_out_of_the_agent_spec(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, prepared)
    view = json.loads(alias_path.read_text(encoding="utf-8"))
    assert not any(str(key).startswith("x-kirocrew-") for key in view)
    metadata = json.loads(_metadata_file(agents, prepared).read_text(encoding="utf-8"))
    assert metadata[projection._MANAGED_MARKER] == projection._MANAGED_MARKER_VALUE
    assert metadata[projection._MANAGED_CREW_HOME] == (home.parent / "crew").as_posix()
    assert metadata[projection._MANAGED_WORK_DIR] == project.absolute().as_posix()
    assert metadata[projection._MANAGED_AGENT] == "custom"
    assert metadata[projection._MANAGED_SOURCE] == (agents / "custom.json").as_posix()
    assert (
        metadata[projection._MANAGED_ALIAS_SHA256]
        == hashlib.sha256(alias_path.read_bytes()).hexdigest()
    )


def test_alias_identity_isolated_by_crew_home_and_keeps_each_projected_mcp_binding(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        '{"name":"custom","resources":["skill://skills/a/SKILL.md"]}',
        encoding="utf-8",
    )
    current_home = {"path": tmp_path / "crew-a"}
    monkeypatch.setattr(projection, "data_home", lambda: current_home["path"])

    def managed_entry(name):
        assert name == "kirocrew-core"
        crew_home = projection.data_home().absolute().as_posix()
        return {
            "command": "test-core",
            "args": [],
            "env": {"KIROCREW_HOME": crew_home},
        }

    monkeypatch.setattr("kiro_crew.agent.managed_mcp_spec_entry", managed_entry)

    first = projection.prepare_native_skill_projection(project)
    current_home["path"] = tmp_path / "crew-b"
    second = projection.prepare_native_skill_projection(project)
    third = None
    try:
        first_alias = first.agent("custom")
        second_alias = second.agent("custom")
        assert first_alias != second_alias

        first_path = _alias_file(agents, first)
        second_path = _alias_file(agents, second)
        first_view = json.loads(first_path.read_text(encoding="utf-8"))
        second_view = json.loads(second_path.read_text(encoding="utf-8"))
        first_home = (tmp_path / "crew-a").absolute().as_posix()
        second_home = (tmp_path / "crew-b").absolute().as_posix()
        assert first_view == first.specs["custom"]
        assert second_view == second.specs["custom"]
        assert first_view["mcpServers"]["kirocrew-core"]["env"] == {"KIROCREW_HOME": first_home}
        assert second_view["mcpServers"]["kirocrew-core"]["env"] == {"KIROCREW_HOME": second_home}
        assert (
            json.loads(_metadata_file(agents, first).read_text(encoding="utf-8"))[
                projection._MANAGED_CREW_HOME
            ]
            == first_home
        )
        assert (
            json.loads(_metadata_file(agents, second).read_text(encoding="utf-8"))[
                projection._MANAGED_CREW_HOME
            ]
            == second_home
        )
        assert first_path.exists() and second_path.exists()

        current_home["path"] = tmp_path / "crew-a"
        third = projection.prepare_native_skill_projection(project)
        assert third.agent("custom") == first_alias
        assert json.loads(first_path.read_text(encoding="utf-8")) == first_view
    finally:
        for prepared in (third, second, first):
            if prepared is not None and prepared._lease_finalizer is not None:
                prepared._lease_finalizer()

    del prepared
    projection_ids = {id(first), id(second), id(third)}
    del first, second, third
    gc.collect()
    assert projection_ids.isdisjoint(projection._ACTIVE_PROJECTIONS)
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    assert list(lease_dir.iterdir()) == []


def test_prune_keeps_alias_held_by_a_live_projection(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    live_alias = _alias_file(agents, live)
    source.unlink()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )

    projection.prepare_native_skill_projection(project)
    assert live_alias.exists()


def test_prune_reclaims_alias_whose_agent_file_was_deleted(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    assert stale.exists()
    del first
    gc.collect()
    # The agent stops resolving, and a spawn for a DIFFERENT live agent runs.
    (agents / "custom.json").unlink()
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(project)
    assert not stale.exists()
    assert _alias_file(agents, second, "other").exists()


def test_prune_reclaims_alias_whose_work_dir_was_deleted(native_tree, monkeypatch, tmp_path):
    _home, agents, project = native_tree
    gone = tmp_path / "gone-workdir"
    gone.mkdir()
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(gone)
    stale = _alias_file(agents, first)
    assert stale.exists()
    del first
    gc.collect()
    shutil.rmtree(gone)
    # A spawn for a still-live work_dir triggers the prune of the dead one.
    projection.prepare_native_skill_projection(project)
    assert not stale.exists()


def test_prune_keeps_alias_replaced_after_stale_classification(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    replacement = stale.read_text(encoding="utf-8")
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    original_is_stale = projection._managed_alias_is_stale
    replacement_identity = []

    def replace_after_classification(spec):
        result = original_is_stale(spec)
        if result and spec.get(projection._MANAGED_AGENT) == "custom":
            # Model another gateway atomically recreating the source and alias
            # after this gateway classified the old alias as stale.
            source.write_text('{"name":"custom"}', encoding="utf-8")
            projection.atomic_write(stale, replacement, restrict_to_owner=True)
            current = stale.stat()
            replacement_identity.append((current.st_dev, current.st_ino))
        return result

    monkeypatch.setattr(projection, "_managed_alias_is_stale", replace_after_classification)
    projection.prepare_native_skill_projection(project)

    assert replacement_identity
    current = stale.stat()
    assert (current.st_dev, current.st_ino) == replacement_identity[0]


def test_projection_lock_covers_alias_publication_and_pruning(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    lock_held = 0
    real_file_lock = projection.platform_compat.file_lock
    real_atomic_write = projection.atomic_write
    real_prune = projection._prune_stale_managed_aliases

    @contextmanager
    def observed_file_lock(fd, **kwargs):
        nonlocal lock_held
        is_alias_lock = kwargs == {
            "exclusive": True,
            "timeout": projection._PROJECTION_LOCK_TIMEOUT_SECS,
        }
        with real_file_lock(fd, **kwargs):
            if is_alias_lock:
                lock_held += 1
            try:
                yield
            finally:
                if is_alias_lock:
                    lock_held -= 1

    def observed_atomic_write(path, *args, **kwargs):
        if path.parent == agents and path.stem.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX):
            assert lock_held
        return real_atomic_write(path, *args, **kwargs)

    def observed_prune(*args, **kwargs):
        assert lock_held
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(projection.platform_compat, "file_lock", observed_file_lock)
    monkeypatch.setattr(projection, "atomic_write", observed_atomic_write)
    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", observed_prune)

    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert _alias_file(agents, prepared).exists()


def test_projection_lock_precedes_every_authored_spec_read(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    (agents / "second.json").write_text('{"name":"second"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name="custom", filename="custom.json", scope="global"),
            SimpleNamespace(name="second", filename="second.json", scope="global"),
        ],
    )
    tokens = []
    real_lock = projection._projection_alias_lock
    real_read = projection._read_agent_spec

    @contextmanager
    def observed_lock(directory):
        with real_lock(directory):
            tokens.append("projection-enter")
            try:
                yield
            finally:
                tokens.append("projection-exit")

    def observed_read(*args, **kwargs):
        tokens.append("spec-read")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(projection, "_projection_alias_lock", observed_lock)
    monkeypatch.setattr(projection, "_read_agent_spec", observed_read)

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    read_positions = [index for index, token in enumerate(tokens) if token == "spec-read"]
    assert len(read_positions) == 2
    assert tokens[0] == "projection-enter"
    assert max(read_positions) < tokens.index("projection-exit")


def test_concurrent_projection_converges_after_stale_writer_is_gated_before_lock(
    native_tree, monkeypatch
):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text(
        '{"name":"custom","tools":["read","shell"],"description":"wide"}',
        encoding="utf-8",
    )
    stale_at_lock = threading.Event()
    resume_stale = threading.Event()
    stale_done = threading.Event()
    stale_results = []
    stale_errors = []
    real_lock = projection._projection_alias_lock

    def gate_stale_before_lock(directory):
        if threading.current_thread().name == "stale-projection-writer":
            stale_at_lock.set()
            if not resume_stale.wait(3.0):
                raise AssertionError("stale projection writer was not resumed")
        return real_lock(directory)

    def run_stale_writer():
        try:
            stale_results.append(projection.prepare_native_skill_projection(project))
        except BaseException as exc:
            stale_errors.append(exc)
        finally:
            stale_done.set()

    monkeypatch.setattr(projection, "_projection_alias_lock", gate_stale_before_lock)
    stale_thread = threading.Thread(target=run_stale_writer, name="stale-projection-writer")
    stale_thread.start()
    newer = None
    try:
        assert stale_at_lock.wait(3.0), "stale projection writer never reached the lock boundary"
        source.write_text(
            '{"name":"custom","tools":["read"],"description":"narrow"}',
            encoding="utf-8",
        )
        newer = projection.prepare_native_skill_projection(project)
        assert newer is not None
    finally:
        resume_stale.set()
    stale_thread.join(timeout=3.0)

    assert stale_done.is_set() and not stale_thread.is_alive()
    assert not stale_errors
    assert len(stale_results) == 1 and stale_results[0] is not None
    assert newer is not None
    final_view = json.loads(_alias_file(agents, newer).read_text(encoding="utf-8"))
    assert final_view["tools"] == ["read"]
    assert final_view["description"] == "narrow"


def test_projection_clears_same_signature_roster_after_source_replacement(native_tree, monkeypatch):
    from kiro_crew import agent_discovery

    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection, "list_agents", agent_discovery.list_agents)
    agent_discovery.clear_list_agents_cache()
    original_signature = agent_discovery._dir_signature
    same_tick_signature = original_signature(agents)
    assert [agent.name for agent in agent_discovery.list_agents(project_dir=str(project))] == [
        "custom"
    ]

    def preserved_signature(directory):
        if directory == agents:
            return same_tick_signature
        return original_signature(directory)

    monkeypatch.setattr(agent_discovery, "_dir_signature", preserved_signature)
    source.unlink()
    source.write_text('{"name":"replacement"}', encoding="utf-8")

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    assert "custom" not in prepared.aliases
    assert "replacement" in prepared.aliases


def test_projection_lock_is_held_while_waiting_for_workspace_settings_lock(
    native_tree, monkeypatch
):
    from kiro_crew.workspace_cli_settings import workspace_cli_settings_lock

    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection_acquired = threading.Event()
    worker_done = threading.Event()
    tokens = []
    worker_errors = []
    real_projection_lock = projection._projection_alias_lock
    real_read = projection._read_agent_spec

    def observed_projection_lock(directory):
        stack = real_projection_lock(directory)
        tokens.append("projection-acquired")
        projection_acquired.set()
        return stack

    def observed_read(*args, **kwargs):
        tokens.append("spec-read")
        return real_read(*args, **kwargs)

    def prepare_while_settings_locked():
        try:
            projection.prepare_native_skill_projection(project)
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            worker_done.set()

    monkeypatch.setattr(projection, "_projection_alias_lock", observed_projection_lock)
    monkeypatch.setattr(projection, "_read_agent_spec", observed_read)
    worker = threading.Thread(target=prepare_while_settings_locked)
    with workspace_cli_settings_lock(project):
        worker.start()
        assert projection_acquired.wait(1.0)
        assert not worker_done.wait(0.1), "projection did not wait for the held settings lock"
    worker.join(timeout=3.0)

    assert worker_done.is_set() and not worker.is_alive()
    assert not worker_errors
    assert tokens[:2] == ["projection-acquired", "spec-read"]


@requires_symlinks
def test_projection_lock_refuses_planted_symlink(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    target = agents / "unrelated.lock"
    target.write_text("unrelated", encoding="utf-8")
    (agents / projection._PROJECTION_LOCK_NAME).symlink_to(target)

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is None
    assert target.read_text(encoding="utf-8") == "unrelated"
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert not (project / ".kiro/settings/cli.json").exists()


@pytest.mark.parametrize("failure_at", ["open", "acquire"])
def test_projection_lock_failure_keeps_aliases_and_preserves_startup(
    native_tree, monkeypatch, failure_at
):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    settings = project / ".kiro/settings/cli.json"
    assert json.loads(settings.read_text(encoding="utf-8"))[projection._INHERIT_SETTING] is True
    settings_before = settings.read_bytes()
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )

    if failure_at == "open":

        def lock_failure(_path):
            raise OSError("test lock unavailable")

        monkeypatch.setattr(projection.platform_compat, "open_lock_file", lock_failure)
    else:

        @contextmanager
        def lock_failure(_fd, **_kwargs):
            raise OSError("test lock unavailable")
            yield

        monkeypatch.setattr(projection.platform_compat, "file_lock", lock_failure)
    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is None
    assert list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json")) == [stale]
    assert settings.read_bytes() == settings_before


def test_prune_keeps_a_live_pairs_alias(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, first)
    # A second spawn for the same live pair must not remove the shared alias.
    projection.prepare_native_skill_projection(project)
    assert alias.exists()


def test_prune_never_touches_another_homes_alias(native_tree):
    _home, agents, project = native_tree
    # A foreign instance's alias: correct marker, DIFFERENT home, dead pair.
    foreign = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}foreignaliasfilename01.json"
    foreign.write_text(
        json.dumps(
            {
                "name": foreign.stem,
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: "/some/other/crew/home",
                projection._MANAGED_WORK_DIR: "/nonexistent/workdir",
                projection._MANAGED_AGENT: "ghost",
                projection._MANAGED_SOURCE: "/nonexistent/workdir/.kiro/agents/ghost.json",
            }
        ),
        encoding="utf-8",
    )
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert foreign.exists()


def test_prune_keeps_other_crew_homes_alias_after_its_work_dir_disappears(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, project = native_tree
    first_crew_home = tmp_path / "crew-a"
    monkeypatch.setattr(projection, "data_home", lambda: first_crew_home)
    gone = tmp_path / "gone-for-first-home"
    gone.mkdir()
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(gone)
    foreign_after_switch = _alias_file(agents, first)
    shutil.rmtree(gone)

    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-b")
    projection.prepare_native_skill_projection(project)
    assert foreign_after_switch.exists()


def test_prune_uses_the_recorded_source_when_filename_differs_from_agent(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, first_project = native_tree
    source = agents / "authored-filename.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name="custom", filename="authored-filename.json", scope="global")
        ],
    )
    first = projection.prepare_native_skill_projection(first_project)
    live_alias = _alias_file(agents, first)

    second_project = tmp_path / "second-project"
    second_project.mkdir()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    projection.prepare_native_skill_projection(second_project)
    assert live_alias.exists()


@pytest.mark.parametrize(
    "filename,authored",
    [
        pytest.param("custom.json", {}, id="absent"),
        pytest.param("custom.json", {"name": ""}, id="empty"),
        pytest.param("custom.json", {"name": None}, id="null"),
        pytest.param("custom.json", {"name": ["custom"]}, id="non-string"),
        pytest.param("custom.agent-spec.json", {}, id="agent-spec-suffix"),
    ],
)
def test_prune_keeps_filename_named_source_and_ownership(
    native_tree, monkeypatch, tmp_path, filename, authored
):
    _home, agents, project = native_tree
    project_agents = project / ".kiro" / "agents"
    project_agents.mkdir(parents=True)
    source = project_agents / filename
    source.write_text(json.dumps(authored), encoding="utf-8")
    second_project = tmp_path / "second-project"
    second_project.mkdir()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")

    def discovered_agents(**kwargs):
        if kwargs["project_dir"] == str(project):
            return [SimpleNamespace(name="custom", filename=filename, scope="project")]
        return [SimpleNamespace(name="other", filename="other.json", scope="global")]

    monkeypatch.setattr(projection, "list_agents", discovered_agents)
    first = projection.prepare_native_skill_projection(project)
    assert first is not None
    alias = _alias_file(agents, first)
    metadata = _metadata_file(agents, first)
    metadata_before = metadata.read_bytes()
    assert first._lease_finalizer is not None
    first._lease_finalizer()
    del first
    gc.collect()

    projection.prepare_native_skill_projection(second_project)

    assert alias.exists()
    assert metadata.read_bytes() == metadata_before


def test_prune_reclaims_filename_named_source_after_declared_rename(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, project = native_tree
    project_agents = project / ".kiro" / "agents"
    project_agents.mkdir(parents=True)
    source = project_agents / "custom.json"
    source.write_text("{}", encoding="utf-8")
    second_project = tmp_path / "second-project"
    second_project.mkdir()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")

    def discovered_agents(**kwargs):
        if kwargs["project_dir"] == str(project):
            return [SimpleNamespace(name="custom", filename="custom.json", scope="project")]
        return [SimpleNamespace(name="other", filename="other.json", scope="global")]

    monkeypatch.setattr(projection, "list_agents", discovered_agents)
    first = projection.prepare_native_skill_projection(project)
    assert first is not None
    stale_alias = _alias_file(agents, first)
    stale_metadata = _metadata_file(agents, first)
    assert first._lease_finalizer is not None
    first._lease_finalizer()
    del first
    gc.collect()
    source.write_text('{"name":"renamed"}', encoding="utf-8")

    projection.prepare_native_skill_projection(second_project)

    assert not stale_alias.exists()
    assert not stale_metadata.exists()


def test_prune_keeps_oversized_alias_and_continues_startup(native_tree, monkeypatch):
    _home, agents, project = native_tree
    oversized = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}oversizedfilename0001.json"
    oversized.write_text("oversized", encoding="utf-8")
    original_read = projection.safe_read_file_bytes

    def read_with_oversized_failure(path):
        if path == str(oversized):
            raise FileTooLargeError("test oversized alias")
        return original_read(path)

    monkeypatch.setattr(projection, "safe_read_file_bytes", read_with_oversized_failure)
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert prepared.agent("custom")
    assert oversized.exists()


def test_prune_leaves_unmarked_and_malformed_prefix_files_alone(native_tree):
    _home, agents, project = native_tree
    # A prefix-named file with NO marker (a scanner-hostile squatter) and a
    # prefix-named file with unparseable content: neither is ours to delete.
    unmarked = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}unmarkedfilename000001.json"
    unmarked.write_text('{"name":"squatter"}', encoding="utf-8")
    malformed = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}malformedfilename00001.json"
    malformed.write_text("{ not json", encoding="utf-8")
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert unmarked.exists()
    assert malformed.exists()


@pytest.mark.parametrize("remote_verdict", [True, None])
def test_prune_refuses_windows_remote_metadata_before_any_path_probe(
    native_tree, monkeypatch, remote_verdict
):
    _home, agents, project = native_tree
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        projection.platform_compat,
        "path_volume_is_remote",
        lambda _path: remote_verdict,
    )

    def unexpected_probe(_path):
        pytest.fail("remote managed metadata must not reach Path.is_dir")

    monkeypatch.setattr(type(project), "is_dir", unexpected_probe)
    spec = {
        projection._MANAGED_WORK_DIR: r"\\attacker.example\share\project",
        projection._MANAGED_AGENT: "custom",
        projection._MANAGED_SOURCE: str(agents / "custom.json"),
    }

    assert projection._managed_alias_is_stale(spec) is False


def test_prune_refuses_windows_linked_ancestor_before_directory_probe(native_tree, monkeypatch):
    _home, agents, project = native_tree
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        projection.platform_compat,
        "path_volume_is_remote",
        lambda _path: False,
    )
    monkeypatch.setattr(
        projection.platform_compat,
        "first_linked_ancestor",
        lambda _path: str(project.parent),
    )

    def unexpected_probe(_path):
        pytest.fail("linked managed metadata must not reach Path.is_dir")

    monkeypatch.setattr(type(project), "is_dir", unexpected_probe)
    spec = {
        projection._MANAGED_WORK_DIR: str(project),
        projection._MANAGED_AGENT: "custom",
        projection._MANAGED_SOURCE: str(agents / "custom.json"),
    }

    assert projection._managed_alias_is_stale(spec) is False


def test_alias_deletion_is_retained_without_identity_safe_unlink(tmp_path, monkeypatch):
    alias = tmp_path / "alias.json"
    alias.write_text("managed", encoding="utf-8")
    identity = alias.stat().st_dev, alias.stat().st_ino
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def unexpected_unlink(_path):
        pytest.fail("a platform without identity-safe unlink must retain the alias")

    monkeypatch.setattr(type(alias), "unlink", unexpected_unlink)

    assert projection._unlink_alias_if_unchanged(alias, identity) is False
    assert alias.read_text(encoding="utf-8") == "managed"


def test_lock_failure_never_overwrites_a_newer_settings_generation(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    assert projection.prepare_native_skill_projection(project) is not None
    settings = project / ".kiro/settings/cli.json"
    newer = []

    def concurrent_update_then_failure(_directory):
        current = json.loads(settings.read_text(encoding="utf-8"))
        current["toolSearch.enabled"] = False
        settings.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        newer.append(settings.read_bytes())
        raise OSError("test lock timeout after concurrent settings update")

    monkeypatch.setattr(projection, "_projection_alias_lock", concurrent_update_then_failure)

    assert projection.prepare_native_skill_projection(project) is None
    assert newer and settings.read_bytes() == newer[0]


def test_prune_keeps_alias_while_an_external_projection_lease_is_locked(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    external = projection._acquire_projection_lease(agents, {stale.stem})
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    live = projection.prepare_native_skill_projection(project)
    assert stale.exists()

    external.close()
    projection.prepare_native_skill_projection(project)
    assert live is not None
    assert not stale.exists()


def test_held_lease_has_a_separate_lock_target_and_only_keeps_named_alias(native_tree):
    _home, agents, _project = native_tree
    named = "kirocrew-skill-view-named"
    lease = projection._acquire_projection_lease(agents, {named})
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    try:
        records = list(lease_dir.glob("*.json"))
        holders = list(lease_dir.glob("*.lock"))
        assert len(records) == 1
        assert len(holders) == 1
        assert records[0].stem == holders[0].stem
        assert json.loads(records[0].read_bytes()) == {"aliases": [named]}
        assert projection._alias_has_external_lease(agents, named)
        assert not projection._alias_has_external_lease(agents, "kirocrew-skill-view-not-named")
    finally:
        lease.close()


def test_projection_lease_refuses_records_beyond_reader_bounds(native_tree):
    _home, agents, _project = native_tree
    at_count_limit = {f"kirocrew-skill-view-{index:024x}" for index in range(1024)}
    lease = projection._acquire_projection_lease(agents, at_count_limit)
    lease.close()

    with pytest.raises(OSError, match="exceeds its reader bound"):
        projection._acquire_projection_lease(
            agents, at_count_limit | {"kirocrew-skill-view-one-too-many"}
        )
    with pytest.raises(OSError, match="exceeds its reader bound"):
        projection._acquire_projection_lease(agents, {"x" * 65536})

    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    assert list(lease_dir.iterdir()) == []
    assert projection._PROJECTION_LEASE_MAX_ALIASES == 1024
    assert projection._PROJECTION_LEASE_MAX_BYTES == 65536


def test_projection_finalizer_removes_both_lease_sidecars(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    assert len(list(lease_dir.glob("*.json"))) == 1
    assert len(list(lease_dir.glob("*.lock"))) == 1

    del prepared
    gc.collect()

    assert list(lease_dir.iterdir()) == []


def test_lease_scan_reclaims_valid_unlocked_crash_residue(native_tree):
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    stale = lease_dir / "crashed.json"
    holder = lease_dir / "crashed.lock"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(holder, "", restrict_to_owner=True)

    assert not projection._alias_has_external_lease(agents, "kirocrew-skill-view-stale")
    assert not stale.exists()
    assert not holder.exists()


def test_lease_scan_retains_a_record_with_a_missing_holder(native_tree):
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    record = lease_dir / "uncertain.json"
    projection.atomic_write(
        record,
        json.dumps({"aliases": ["kirocrew-skill-view-uncertain"]}),
        restrict_to_owner=True,
    )

    assert projection._alias_has_external_lease(agents, "kirocrew-skill-view-uncertain")
    assert record.exists()
    assert not (lease_dir / "uncertain.lock").exists()


def test_windows_alias_unlink_rechecks_identity_under_publication_lock(tmp_path, monkeypatch):
    alias = tmp_path / "alias.json"
    alias.write_text("managed", encoding="utf-8")
    identity = alias.stat().st_dev, alias.stat().st_ino
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)

    assert projection._unlink_alias_if_unchanged(alias, identity)
    assert not alias.exists()


def test_projection_and_provider_serialize_workspace_settings_writes(native_tree, monkeypatch):
    from kiro_crew.providers import acp as provider_acp

    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    provider_write_reached = threading.Event()
    provider_done = threading.Event()
    provider_errors = []
    real_provider_atomic_write = provider_acp.atomic_write
    real_projection_atomic_write = projection.atomic_write
    update_thread = None
    started = False

    def observed_provider_atomic_write(*args, **kwargs):
        provider_write_reached.set()
        return real_provider_atomic_write(*args, **kwargs)

    def update_tool_search():
        try:
            provider_acp._write_tool_search_overlay(project, True, 17, 4096)
        except BaseException as exc:
            provider_errors.append(exc)
        finally:
            provider_done.set()

    def start_concurrent_writer(path, *args, **kwargs):
        nonlocal update_thread, started
        if (
            not started
            and path.parent == agents
            and path.stem.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)
        ):
            started = True
            update_thread = threading.Thread(target=update_tool_search)
            update_thread.start()
            assert not provider_write_reached.wait(
                0.1
            ), "the provider reached its cli.json commit while projection held the settings lock"
        return real_projection_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(provider_acp, "atomic_write", observed_provider_atomic_write)
    monkeypatch.setattr(projection, "atomic_write", start_concurrent_writer)

    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert update_thread is not None
    update_thread.join(timeout=3.0)
    assert not update_thread.is_alive()
    assert provider_done.is_set() and not provider_errors
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings[projection._MANAGED_SETTING] is True
    assert settings["toolSearch.enabled"] is True
    assert settings["toolSearch.minPct"] == 17
    assert settings["toolSearch.minTokens"] == 4096


@requires_symlinks
def test_workspace_settings_lock_refuses_a_planted_symlink(tmp_path):
    from kiro_crew.workspace_cli_settings import (
        CLI_SETTINGS_LOCK_NAME,
        workspace_cli_settings_lock,
    )

    project = tmp_path / "project"
    settings = project / ".kiro" / "settings"
    settings.mkdir(parents=True)
    target = tmp_path / "unrelated.lock"
    target.write_text("unrelated", encoding="utf-8")
    (settings / CLI_SETTINGS_LOCK_NAME).symlink_to(target)

    with pytest.raises(OSError, match="symlink or junction"):
        with workspace_cli_settings_lock(project):
            pytest.fail("a planted settings lock must never be acquired")

    assert target.read_text(encoding="utf-8") == "unrelated"


def test_workspace_settings_lock_failure_publishes_no_alias(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")

    @contextmanager
    def unavailable(_work_dir):
        raise OSError("test settings lock unavailable")
        yield

    monkeypatch.setattr(projection, "workspace_cli_settings_lock", unavailable)

    assert projection.prepare_native_skill_projection(project) is None
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert not (project / ".kiro/settings/cli.json").exists()


def _deep_bounded_json() -> bytes:
    raw = ("[" * 12000 + "0" + "]" * 12000).encode()
    assert len(raw) < projection._PROJECTION_LEASE_MAX_BYTES
    with pytest.raises(RecursionError):
        json.loads(raw)
    return raw


def _projected_alias(project, name="custom"):
    identity = (
        f"{projection.data_home().absolute().as_posix()}\n"
        f"{project.absolute().as_posix()}\n{name}"
    )
    return projection.NATIVE_SKILL_ALIAS_PREFIX + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _write_stale_managed_alias(agents, crew_home_id, index):
    alias = projection.NATIVE_SKILL_ALIAS_PREFIX + f"{index:024x}"
    path = agents / f"{alias}.json"
    raw = json.dumps({"name": alias}).encode()
    path.write_bytes(raw)
    missing_work_dir = agents.parent / f"missing-work-{index}"
    metadata_dir = agents / projection._PROJECTION_METADATA_DIR_NAME
    metadata_dir.mkdir(exist_ok=True)
    metadata = {
        projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
        projection._MANAGED_CREW_HOME: crew_home_id,
        projection._MANAGED_WORK_DIR: missing_work_dir.as_posix(),
        projection._MANAGED_AGENT: f"stale-{index}",
        projection._MANAGED_SOURCE: (missing_work_dir / ".kiro/agents/stale.json").as_posix(),
        projection._MANAGED_ALIAS_SHA256: hashlib.sha256(raw).hexdigest(),
    }
    (metadata_dir / path.name).write_text(json.dumps(metadata), encoding="utf-8")
    return path


def test_deep_lease_json_is_uncertain_instead_of_aborting(native_tree):
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    record = lease_dir / "deep.json"
    holder = lease_dir / "deep.lock"
    record.write_bytes(_deep_bounded_json())
    holder.write_text("", encoding="utf-8")

    assert projection._alias_has_external_lease(agents, "kirocrew-skill-view-any")
    assert record.exists() and holder.exists()


def test_deep_ownership_metadata_is_unowned_and_retained(native_tree):
    _home, agents, _project = native_tree
    alias = projection.NATIVE_SKILL_ALIAS_PREFIX + "deepmetadata000000000001"
    path = agents / f"{alias}.json"
    raw = json.dumps({"name": alias}).encode()
    path.write_bytes(raw)
    metadata_dir = agents / projection._PROJECTION_METADATA_DIR_NAME
    metadata_dir.mkdir()
    (metadata_dir / path.name).write_bytes(_deep_bounded_json())

    assert projection._managed_metadata_for_alias(agents, path, raw) is None
    assert path.exists()


def test_deep_legacy_in_spec_metadata_is_unowned_and_retained(native_tree):
    _home, agents, _project = native_tree
    alias = projection.NATIVE_SKILL_ALIAS_PREFIX + "deeplegacy00000000000001"
    path = agents / f"{alias}.json"
    raw = _deep_bounded_json()
    path.write_bytes(raw)

    assert projection._managed_metadata_for_alias(agents, path, raw) is None
    assert path.exists()


def test_lease_scan_cap_is_uncertain_and_retains_stale_alias(native_tree, monkeypatch):
    home, agents, _project = native_tree
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 2, raising=False)
    stale = _write_stale_managed_alias(agents, (home.parent / "crew").as_posix(), 1)
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    for index in range(3):
        (lease_dir / f"residue-{index}.json").write_text(
            json.dumps({"aliases": [f"other-{index}"]}), encoding="utf-8"
        )
        (lease_dir / f"residue-{index}.lock").write_text("", encoding="utf-8")

    projection._prune_stale_managed_aliases(agents, (home.parent / "crew").as_posix(), keep=set())

    assert stale.exists()
    assert len(tuple(lease_dir.iterdir())) == 2


def test_prune_candidate_cap_bounds_work_and_defers_overflow(native_tree, monkeypatch):
    home, agents, _project = native_tree
    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 2, raising=False)
    crew_home_id = (home.parent / "crew").as_posix()
    candidates = [_write_stale_managed_alias(agents, crew_home_id, index) for index in range(3)]
    stale_checks = 0
    real_is_stale = projection._managed_alias_is_stale

    def count_stale_checks(metadata):
        nonlocal stale_checks
        stale_checks += 1
        return real_is_stale(metadata)

    monkeypatch.setattr(projection, "_managed_alias_is_stale", count_stale_checks)

    projection._prune_stale_managed_aliases(agents, crew_home_id, keep=set())

    assert stale_checks == 4
    assert sum(path.exists() for path in candidates) == 1


def test_fresh_metadata_failure_never_publishes_alias(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    alias = _projected_alias(project)
    alias_path = agents / f"{alias}.json"
    metadata_path = agents / projection._PROJECTION_METADATA_DIR_NAME / f"{alias}.json"
    real_atomic_write = projection.atomic_write

    def fail_metadata(path, *args, **kwargs):
        if path == metadata_path:
            raise OSError("test metadata publication failure")
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", fail_metadata)

    assert projection.prepare_native_skill_projection(project) is None
    assert not alias_path.exists()
    assert not metadata_path.exists()


def test_fresh_alias_failure_leaves_only_hidden_ownership(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    alias = _projected_alias(project)
    alias_path = agents / f"{alias}.json"
    metadata_path = agents / projection._PROJECTION_METADATA_DIR_NAME / f"{alias}.json"
    real_atomic_write = projection.atomic_write

    def fail_alias(path, *args, **kwargs):
        if path == alias_path:
            raise OSError("test alias publication failure")
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", fail_alias)

    assert projection.prepare_native_skill_projection(project) is None
    assert not alias_path.exists()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata[projection._MANAGED_MARKER] == projection._MANAGED_MARKER_VALUE
    assert metadata[projection._MANAGED_ALIAS_SHA256]
    assert projection._MANAGED_STAGED_OWNERSHIP not in metadata


def test_changed_alias_failure_preserves_the_existing_owned_generation(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","description":"old"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, first)
    metadata_path = _metadata_file(agents, first)
    old_raw = alias_path.read_bytes()
    old_identity = (alias_path.stat().st_dev, alias_path.stat().st_ino)
    old_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    source.write_text('{"name":"custom","description":"new"}', encoding="utf-8")
    real_atomic_write = projection.atomic_write

    def fail_alias(path, *args, **kwargs):
        if path == alias_path:
            raise OSError("test replacement publication failure")
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", fail_alias)

    assert projection.prepare_native_skill_projection(project) is None
    assert alias_path.read_bytes() == old_raw
    assert (alias_path.stat().st_dev, alias_path.stat().st_ino) == old_identity
    staged_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    staged = staged_metadata.pop(projection._MANAGED_STAGED_OWNERSHIP)
    assert staged_metadata == old_metadata
    assert (
        staged[projection._MANAGED_ALIAS_SHA256] != old_metadata[projection._MANAGED_ALIAS_SHA256]
    )
    managed = projection._managed_metadata_for_alias(agents, alias_path, old_raw)
    assert managed is not None and managed[0] == old_metadata


def test_successful_replacement_is_metadata_first_and_converges(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","description":"old"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, first)
    metadata_path = _metadata_file(agents, first)
    old_raw = alias_path.read_bytes()
    source.write_text('{"name":"custom","description":"new"}', encoding="utf-8")
    real_atomic_write = projection.atomic_write
    writes = []

    def record_publication(path, *args, **kwargs):
        if path in {alias_path, metadata_path}:
            writes.append(path)
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", record_publication)

    second = projection.prepare_native_skill_projection(project)

    assert second is not None
    assert writes == [metadata_path, alias_path, metadata_path]
    current_raw = alias_path.read_bytes()
    assert current_raw != old_raw
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert projection._MANAGED_STAGED_OWNERSHIP not in metadata
    assert metadata[projection._MANAGED_ALIAS_SHA256] == hashlib.sha256(current_raw).hexdigest()
    managed = projection._managed_metadata_for_alias(agents, alias_path, current_raw)
    assert managed is not None and managed[0] == metadata


@requires_symlinks
@pytest.mark.parametrize("metadata_present", [False, True])
def test_existing_alias_symlink_aborts_without_mutation(native_tree, monkeypatch, metadata_present):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    alias = _projected_alias(project)
    alias_path = agents / f"{alias}.json"
    target_path = agents / "local-alias-target.json"
    target_path.write_bytes(b"operator-owned-target")
    alias_path.symlink_to(target_path.name)
    alias_identity_before = (alias_path.lstat().st_dev, alias_path.lstat().st_ino)
    target_identity_before = (target_path.stat().st_dev, target_path.stat().st_ino)
    link_target_before = alias_path.readlink()
    target_before = target_path.read_bytes()

    metadata_path = agents / projection._PROJECTION_METADATA_DIR_NAME / f"{alias}.json"
    metadata_before = None
    metadata_identity_before = None
    if metadata_present:
        metadata_path.parent.mkdir()
        metadata_path.write_bytes(b"operator-owned-metadata")
        metadata_before = metadata_path.read_bytes()
        metadata_identity_before = (metadata_path.stat().st_dev, metadata_path.stat().st_ino)

    real_safe_read = projection.safe_read_file_bytes

    def reject_linked_alias_read(path):
        if path == str(alias_path):
            pytest.fail("linked alias must not be read")
        return real_safe_read(path)

    monkeypatch.setattr(projection, "safe_read_file_bytes", reject_linked_alias_read)
    real_atomic_write = projection.atomic_write
    pair_writes = []

    def record_pair_writes(path, *args, **kwargs):
        if path in {alias_path, metadata_path}:
            pair_writes.append(path)
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", record_pair_writes)

    assert projection.prepare_native_skill_projection(project) is None
    assert pair_writes == []
    assert alias_path.is_symlink()
    assert (alias_path.lstat().st_dev, alias_path.lstat().st_ino) == alias_identity_before
    assert alias_path.readlink() == link_target_before
    assert target_path.read_bytes() == target_before
    assert (target_path.stat().st_dev, target_path.stat().st_ino) == target_identity_before
    if metadata_before is None:
        assert not metadata_path.exists()
    else:
        assert metadata_path.read_bytes() == metadata_before
        assert (
            metadata_path.stat().st_dev,
            metadata_path.stat().st_ino,
        ) == metadata_identity_before


@pytest.mark.parametrize("metadata_present", [False, True])
def test_existing_alias_directory_aborts_without_mutation(
    native_tree, monkeypatch, metadata_present
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    alias = _projected_alias(project)
    alias_path = agents / f"{alias}.json"
    alias_path.mkdir()
    alias_identity_before = (alias_path.stat().st_dev, alias_path.stat().st_ino)

    metadata_path = agents / projection._PROJECTION_METADATA_DIR_NAME / f"{alias}.json"
    metadata_before = None
    metadata_identity_before = None
    if metadata_present:
        metadata_path.parent.mkdir()
        metadata_path.write_bytes(b"operator-owned-metadata")
        metadata_before = metadata_path.read_bytes()
        metadata_identity_before = (metadata_path.stat().st_dev, metadata_path.stat().st_ino)

    real_atomic_write = projection.atomic_write
    pair_writes = []

    def record_pair_writes(path, *args, **kwargs):
        if path in {alias_path, metadata_path}:
            pair_writes.append(path)
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", record_pair_writes)

    assert projection.prepare_native_skill_projection(project) is None
    assert pair_writes == []
    assert alias_path.is_dir()
    assert (alias_path.stat().st_dev, alias_path.stat().st_ino) == alias_identity_before
    if metadata_before is None:
        assert not metadata_path.exists()
    else:
        assert metadata_path.read_bytes() == metadata_before
        assert (
            metadata_path.stat().st_dev,
            metadata_path.stat().st_ino,
        ) == metadata_identity_before


@pytest.mark.parametrize(
    "ownership_state",
    [
        "missing",
        "malformed",
        "deeply_nested",
        "digest_mismatched",
        "transiently_unreadable",
    ],
)
def test_existing_alias_with_unverifiable_ownership_aborts_without_mutation(
    native_tree, monkeypatch, ownership_state
):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","description":"old"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, first)
    metadata_path = _metadata_file(agents, first)

    if ownership_state == "missing":
        metadata_path.unlink()
    elif ownership_state == "malformed":
        metadata_path.write_bytes(b"{not-json")
    elif ownership_state == "deeply_nested":
        metadata_path.write_bytes(_deep_bounded_json())
    elif ownership_state == "digest_mismatched":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata[projection._MANAGED_ALIAS_SHA256] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    alias_before = alias_path.read_bytes()
    alias_identity_before = (alias_path.stat().st_dev, alias_path.stat().st_ino)
    metadata_before = metadata_path.read_bytes() if metadata_path.exists() else None
    metadata_identity_before = (
        (metadata_path.stat().st_dev, metadata_path.stat().st_ino)
        if metadata_path.exists()
        else None
    )
    source.write_text('{"name":"custom","description":"new"}', encoding="utf-8")

    real_safe_read = projection.safe_read_file_bytes
    if ownership_state == "transiently_unreadable":

        def unreadable_metadata(path):
            if path == str(metadata_path):
                return None
            return real_safe_read(path)

        monkeypatch.setattr(projection, "safe_read_file_bytes", unreadable_metadata)

    real_atomic_write = projection.atomic_write
    pair_writes = []

    def record_pair_writes(path, *args, **kwargs):
        if path in {alias_path, metadata_path}:
            pair_writes.append(path)
        return real_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(projection, "atomic_write", record_pair_writes)

    assert projection.prepare_native_skill_projection(project) is None
    assert pair_writes == []
    assert alias_path.read_bytes() == alias_before
    assert (alias_path.stat().st_dev, alias_path.stat().st_ino) == alias_identity_before
    if metadata_before is None:
        assert not metadata_path.exists()
    else:
        assert metadata_path.read_bytes() == metadata_before
        assert (
            metadata_path.stat().st_dev,
            metadata_path.stat().st_ino,
        ) == metadata_identity_before
        if ownership_state in {"digest_mismatched", "transiently_unreadable"}:
            metadata = json.loads(metadata_before)
            assert projection._MANAGED_STAGED_OWNERSHIP not in metadata
