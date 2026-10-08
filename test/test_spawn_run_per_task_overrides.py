"""Per-task overrides in ``spawn_run`` ``tasks[]``.

A ``tasks`` entry may be an object ``{task, model?, reasoning_effort?}``
whose fields win over the call's batch-wide value for that task only, so one
wave can run the same prompt on two models and deliver the results together.
Plain string entries keep their exact old behaviour.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.validation import SPAWN_RUN_SCHEMA, ValidationError, validate_tool_args

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _run_tool(args: dict[str, Any], responses: list[dict] | None = None) -> tuple[list[dict], str]:
    """Run spawn_run against a fake gateway; return (POSTed bodies, result text)."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []
    answers = iter(responses or [])

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            return next(answers, {"id": f"a{len(bodies)}"})
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool_inner("spawn_run", args)
    return bodies, result


class TestSchema:
    def test_object_entry_is_accepted_and_cleaned(self):
        cleaned = validate_tool_args(
            {"tasks": ["plain", {"task": "p", "model": "claude-haiku-4.5"}]}, SPAWN_RUN_SCHEMA
        )
        assert cleaned["tasks"] == ["plain", {"task": "p", "model": "claude-haiku-4.5"}]

    def test_unknown_key_in_object_is_refused_with_its_path(self):
        with pytest.raises(ValidationError) as exc:
            validate_tool_args({"tasks": [{"task": "p", "cwd": "/tmp"}]}, SPAWN_RUN_SCHEMA)
        assert exc.value.field == "tasks[0].cwd"

    def test_object_without_task_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            validate_tool_args({"tasks": [{"model": "claude-haiku-4.5"}]}, SPAWN_RUN_SCHEMA)
        assert exc.value.field == "tasks[0].task"

    @pytest.mark.parametrize(
        "item",
        [{"task": "p", "reasoning_effort": "ultra"}, {"task": "p", "model": "bad model!"}],
    )
    def test_object_fields_use_the_top_level_rules(self, item):
        with pytest.raises(ValidationError):
            validate_tool_args({"tasks": [item]}, SPAWN_RUN_SCHEMA)

    @pytest.mark.parametrize("bad", [1, None, ["x"], True])
    def test_other_item_types_are_still_refused(self, bad):
        with pytest.raises(ValidationError, match="expected str or dict"):
            validate_tool_args({"tasks": ["ok", bad]}, SPAWN_RUN_SCHEMA)


class TestForwarding:
    def test_per_task_model_overrides_batch_wide_in_one_wave(self):
        bodies, _ = _run_tool(
            {
                "tasks": [{"task": "review", "model": "gpt-6"}, "review"],
                "model": "claude-opus-5.5",
            }
        )
        assert [b["model"] for b in bodies] == ["gpt-6", "claude-opus-5.5"]
        # One wave: both members share a batch id, so one digest delivers both.
        assert bodies[0]["batch_id"] and bodies[0]["batch_id"] == bodies[1]["batch_id"]
        assert all(b["batch_total"] == 2 for b in bodies)

    def test_per_task_effort_overrides_and_agents_still_apply(self):
        bodies, _ = _run_tool(
            {
                "tasks": [{"task": "a", "reasoning_effort": "max"}, "b"],
                "agents": ["kirocrew", ""],
                "reasoning_effort": "low",
            }
        )
        assert [b["reasoning_effort"] for b in bodies] == ["max", "low"]
        assert [b["agent"] for b in bodies] == ["kirocrew", ""]

    def test_unset_override_falls_back_and_omits_when_nothing_set(self):
        bodies, _ = _run_tool({"tasks": [{"task": "a"}, {"task": "b", "model": "gpt-6"}]})
        assert "model" not in bodies[0]
        assert bodies[1]["model"] == "gpt-6"

    def test_string_entries_are_unchanged(self):
        bodies, _ = _run_tool({"tasks": ["t1", "t2"], "model": "m1"})
        assert [(b["task"], b["model"]) for b in bodies] == [("t1", "m1"), ("t2", "m1")]

    def test_agent_is_not_a_task_object_field(self):
        with pytest.raises(ValidationError) as exc:
            validate_tool_args({"tasks": [{"task": "a", "agent": "kirocrew"}]}, SPAWN_RUN_SCHEMA)
        assert exc.value.field == "tasks[0].agent"

    def test_effort_verdict_names_each_tasks_own_level(self):
        _, result = _run_tool(
            {"tasks": [{"task": "a", "reasoning_effort": "max"}, "b"], "reasoning_effort": "low"},
            responses=[
                {"id": "s1", "effort_dropped": "model auto"},
                {"id": "s2", "effort_dropped": "model auto"},
            ],
        )
        assert "reasoning_effort='max' dropped for s1: model auto" in result
        assert "reasoning_effort='low' dropped for s2: model auto" in result
