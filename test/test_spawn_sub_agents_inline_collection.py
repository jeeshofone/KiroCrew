"""A blocking ``spawn_sub_agents`` call's members are never injected into its parent.

A cron run's turn blocked in ``spawn_sub_agents`` must survive its first child
finishing: that child's ``[Subagent completion event]`` is the call's own result,
so injecting it into the parent session would interrupt the very turn waiting on
it. Every parent kind follows the same rule as the dashboard route. These tests
drive the real seams end to end: ``/api/spawn``'s reservation, the tool's closing
``mark-collected`` through the dashboard handler, then the gateway's completion
callback, for each parent kind that injects.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_handlers_messaging_coverage import _info as _run_info
from test_handlers_messaging_coverage import _mgr, _payload
from test_handlers_messaging_coverage import _Req as _CoverageReq
from test_handlers_messaging_coverage import _state

import kiro_crew.dashboard.handlers.messaging as handlers
from kiro_crew.mcp_core import _call_tool
from kiro_crew.subagent_inline_collection import (
    CLAIM_TTL_SECS,
    COLLECTED_TTL_SECS,
    COLLECTION_GRACE_SECS,
    MAX_IDS_PER_PARENT,
    InlineCollections,
)

CRON_PARENT = "cron:job1:run1"
SLACK_PARENT = "C123:1234.567890"


class _Req:
    """Minimal internal request double for the mark-collected handler."""

    def __init__(self, state: Any, body: Any) -> None:
        self.app: dict[str, Any] = {"state": state}
        self._body = body
        self.match_info: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.remote = "127.0.0.1"
        self._extra = {"app": "", "user": "U0OWNER0000"}

    def __contains__(self, key: str) -> bool:
        return key in self._extra

    def __getitem__(self, key: str) -> Any:
        return self._extra[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)

    async def json(self) -> Any:
        return self._body


def _gateway(registry: InlineCollections) -> tuple[Any, Any, Any]:
    """``(orchestrator, on_done, dashboard_state)`` with *registry* on the manager."""
    from test_slack_gateway import (
        _make_orchestrator,
        _mock_context_builder,
        _mock_dashboard_state,
        _mock_sessions,
    )

    orch = _make_orchestrator(slack_enabled=True, owner_id="U1")
    orch.sessions = _mock_sessions()
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.ctx_builder = _mock_context_builder()
    orch.ctx_builder.hooks = MagicMock()
    orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    orch.dashboard_state = _mock_dashboard_state()
    orch.dashboard_state.get_slot = MagicMock(return_value=None)
    orch.slack = MagicMock()
    orch.slack.open_dm = AsyncMock(return_value="D_U1")
    orch.slack.post_message = AsyncMock()
    orch.slack.post_blocks = AsyncMock(return_value="ts")
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            mgr = MagicMock()
            mgr.inline_collections = registry
            mgr.start_reaper = MagicMock()
            mgr.running = []
            mgr.queued_count_for = MagicMock(return_value=0)
            mgr.queued_count_for_async = AsyncMock(return_value=0)
            mgr.has_pending_work_for = MagicMock(return_value=False)
            mgr.has_pending_work_for_async = AsyncMock(return_value=False)
            mgr.running_agents_for = MagicMock(return_value=[])
            # Every member id is one this gateway spawned.
            mgr.get = MagicMock(side_effect=lambda aid: object())
            mgr.notify_injection_failed = MagicMock()
            mgr.settle_queued_delivery = AsyncMock()
            mock_sm.return_value = mgr
            orch._init_subagents()
    on_done = mock_sm.call_args[1]["on_done"]
    # The registry delivers through the manager it is bound to: the gateway's
    # route, its sessions, and the terminal reports still running.
    mgr._on_done = on_done
    mgr._sessions = orch.sessions
    mgr._report_owners = {}
    registry.bind(mgr)
    return orch, on_done, mgr


def _info(agent_id: str, parent: str) -> Any:
    from kiro_crew.subagent import SubagentInfo

    # A real run record: the registry holds only a dataclass it can bound.
    info = SubagentInfo(id=agent_id, task="review a file", parent_session_key=parent)
    info.done = True
    info.result = f"{agent_id} result"
    info.elapsed = 1.0
    info.started = 0.0
    return info


async def _post(orch: Any, mgr: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Drive the real ``mark-collected`` handler the way the tool's ``_post`` reaches it."""
    state = orch.dashboard_state
    state.subagents = mgr
    resp = await handlers.api_spawn_mark_collected(_Req(state, body))
    raw = resp.body
    assert isinstance(raw, (bytes, bytearray))
    return json.loads(raw)


def _assert_parent_untouched(orch: Any, stream: AsyncMock) -> None:
    """Nothing prompted, cancelled or claimed the parent's session."""
    stream.assert_not_awaited()
    orch.sessions.get_or_create.assert_not_awaited()
    orch.sessions.cancel_current.assert_not_awaited()


async def _drain(orch: Any) -> None:
    """Wait (bounded) for every delivery the registry and the gateway started."""
    registry = orch.subagent_mgr.inline_collections
    for _ in range(5):
        await asyncio.sleep(0)
        pending = [*registry._tasks, *orch._background_tasks]
        if not pending:
            return
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10)


def _stream() -> Any:
    return patch(
        "kiro_crew.slack.gateway.stream_and_collect",
        new_callable=AsyncMock,
        return_value="llm response",
    )


class TestCronParentBlockedInSpawnSubAgents:
    @pytest.mark.asyncio
    async def test_first_child_finishing_does_not_touch_the_blocked_parent_turn(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        # /api/spawn reserved both members for the call, which now polls them.
        assert registry.reserve(CRON_PARENT, "a1", 600)
        assert registry.reserve(CRON_PARENT, "a2", 600)
        with _stream() as stream:
            # The first member finishes while the parent is still blocked.
            first = _info("a1", CRON_PARENT)
            await on_done(first)
            _assert_parent_untouched(orch, stream)
            # Held, so not delivered: its retention clock has not started.
            assert first._delivery_queued is True

            # The call returns both members inline and ends its collection.
            await _post(
                orch,
                mgr,
                {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": CRON_PARENT},
            )
            # The second member's completion lands after the call returned it.
            await on_done(_info("a2", CRON_PARENT))
            await _drain(orch)
            _assert_parent_untouched(orch, stream)

        orch.dashboard_state.notify.assert_not_called()
        # The held member is settled when the call returned it, exactly once.
        settled = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [[d.agent_id for d in batch] for batch in settled] == [["a1"]]
        # Nothing is left behind that a later completion could trip over.
        assert not registry.hold(CRON_PARENT, "a1", object())
        assert not registry.consume_collected(CRON_PARENT, "a1")
        assert not registry.consume_collected(CRON_PARENT, "a2")

    @pytest.mark.asyncio
    async def test_a_held_completion_resets_the_cron_parent_only_once_it_is_idle(
        self,
    ) -> None:
        """The held member was the parent's last sub-agent, so the idle reset the
        injecting branch would do still runs, but only as the skip-if-busy reset:
        the parent's own turn holds the session, and an unconditional reset
        would tear that turn down."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        orch.sessions.reset = AsyncMock(return_value=False)
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            _assert_parent_untouched(orch, stream)
        orch.sessions.reset.assert_awaited_once_with(CRON_PARENT, skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_a_held_memory_wait_expiry_settles_its_owed_report(self) -> None:
        """A member that expired waiting for memory has no folder, only the
        store's owed report. The hold parked it, so its report task did not
        clear that debt; the call returning it must, or the next start replays
        a failure the tool already returned inline."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        expiry = _info("a1", CRON_PARENT)
        expiry.error = "memory wait expired"
        expiry._report_owed = True
        with _stream() as stream:
            await on_done(expiry)
            await _post(
                orch, mgr, {"ids": ["a1"], "released": ["a1"], "parent_session": CRON_PARENT}
            )
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [(d.agent_id, d.report_owed) for d in batch] == [("a1", True)]

    @pytest.mark.asyncio
    async def test_a_member_the_call_left_running_is_still_injected(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        # The wait expired with a1 still running: released, never collected.
        await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
        stream.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("on_disk", [False, True], ids=["transient", "transcript"])
    async def test_a_long_held_result_is_delivered_as_the_live_run_would_be(
        self, tmp_path: Any, on_disk: bool
    ) -> None:
        """Through the real route. Under the budget the held copy is the whole
        result, with ``result.txt`` (whose path rides along) or without it
        (incognito, temporary), so the delivered text is the result whole."""
        transcript = tmp_path / "result.txt"
        if on_disk:
            transcript.write_text("the whole result", encoding="utf-8")
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        info.result = "head-" + "r" * 600_000 + "-tail"
        info.result_path = str(transcript)
        with _stream() as stream:
            await on_done(info)
            _assert_parent_untouched(orch, stream)
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
            stream.assert_awaited_once()
        # The announce the route built, handed to the context builder as the turn's text.
        (prompt,) = [c.args[0] for c in orch.ctx_builder.build_message.call_args_list]
        assert info.result in prompt
        assert registry._held_bytes == 0

    @pytest.mark.asyncio
    async def test_a_held_member_the_cancelled_call_never_returned_is_delivered(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        registry.reserve(CRON_PARENT, "a2", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            _assert_parent_untouched(orch, stream)
            # Cancelled before returning anything: a1 is delivered as an
            # ordinary completion, once the call has let go of the turn.
            await _post(
                orch, mgr, {"ids": [], "released": ["a1", "a2"], "parent_session": CRON_PARENT}
            )
            await _drain(orch)
            stream.assert_awaited_once()
        settled = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [[d.agent_id for d in batch] for batch in settled] == [["a1"]]

    @pytest.mark.asyncio
    async def test_a_cancelled_calls_held_member_waits_for_the_parents_turn_to_end(
        self,
    ) -> None:
        """The cancel close lands while the parent's turn is still ending: the
        released result is delivered only once that turn has let go of the
        session, as a new turn after it, never as a prompt inside it."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        events: list[str] = []
        busy = iter([True, True, False])

        def _is_busy(key: str) -> bool:
            assert key == CRON_PARENT
            answer = next(busy, False)
            events.append(f"busy={answer}")
            return answer

        orch.sessions.is_busy = MagicMock(side_effect=_is_busy)
        registry.reserve(CRON_PARENT, "a1", 600)
        with (
            _stream() as stream,
            patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0),
        ):
            stream.side_effect = lambda *a, **k: events.append("prompt") or "ok"
            await on_done(_info("a1", CRON_PARENT))
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
        assert events == ["busy=True", "busy=True", "busy=False", "prompt"]

    @pytest.mark.asyncio
    async def test_a_released_result_whose_parent_was_retired_is_not_injected(self) -> None:
        """The parent was retired while the result was held: the registry's
        fence applies, so nothing recreates or reaches the retired conversation,
        and no delivered mark is written."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            registry.retire(CRON_PARENT)
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        mgr.settle_queued_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_parent_retired_during_the_idle_wait_gets_nothing(self) -> None:
        """The close lands while the parent's turn is still ending, and the
        parent is retired during that wait (``!agent default`` removes the
        session). The fence is read at the point of delivery, after the wait,
        so nothing is delivered, nothing is recreated, and an owed report is
        cleared rather than replayed into a later conversation."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        busy = iter([True, True, False])

        def _is_busy(_key: str) -> bool:
            answer = next(busy, False)
            if not answer:
                # The parent's turn ended because the session was removed.
                registry.retire(CRON_PARENT)
            return answer

        orch.sessions.is_busy = MagicMock(side_effect=_is_busy)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        info._report_owed = True
        with (
            _stream() as stream,
            patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0),
        ):
            await on_done(info)
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._admission.taskq_clear_owed_reports.assert_called_once_with(["a1"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", [SLACK_PARENT, CRON_PARENT])
    async def test_a_parent_retired_inside_the_route_gets_nothing(self, parent: str) -> None:
        """The fence passed and the released result is inside the parent's
        route when ``!agent default`` removes the parent, during the route's
        awaited ``session_store_for_turn``. Retirement cancels the delivery
        there, so routing never resumes into ``get_or_create`` or the
        injection, and an owed report is cleared."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(parent, "a1", 600)
        info = _info("a1", parent)
        info._report_owed = True
        entered = asyncio.Event()

        async def _store(*_a: Any, **_k: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()  # parked until retire cancels it

        with (
            _stream() as stream,
            patch("kiro_crew.slack.gateway.session_store_for_turn", side_effect=_store),
        ):
            await on_done(info)
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": parent})
            await asyncio.wait_for(entered.wait(), timeout=5)
            registry.retire(parent)
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._admission.taskq_clear_owed_reports.assert_called_once_with(["a1"])
        assert parent not in registry._records

    @pytest.mark.asyncio
    async def test_a_completion_landing_during_the_closes_settle_is_not_injected(
        self,
    ) -> None:
        """The close settles a held member, and while it awaits that settle the
        second member's completion callback runs. The id the call returned is
        already recorded by then, so the cron turn still blocked on the close
        is never prompted or cancelled."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        registry.reserve(CRON_PARENT, "a2", 600)
        landed: list[Any] = []

        async def _settle(_deliveries: Any) -> None:
            landed.append(await on_done(_info("a2", CRON_PARENT)))

        mgr.settle_queued_delivery.side_effect = _settle
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            await _post(
                orch,
                mgr,
                {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": CRON_PARENT},
            )
            await _drain(orch)
            assert landed, "the settle hook did not run"
            _assert_parent_untouched(orch, stream)

    @pytest.mark.asyncio
    async def test_a_returned_member_whose_record_was_evicted_is_not_redelivered(
        self,
    ) -> None:
        """``evict_completed_agents`` dropped the held member's record before the
        close. The call still returned it, so it is settled, never treated as an
        orphan and injected into the turn blocked on the close."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        mgr.get.side_effect = lambda aid: None if aid == "a1" else object()
        registry.reserve(CRON_PARENT, "a1", 600)
        registry.reserve(CRON_PARENT, "a2", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            await _post(
                orch,
                mgr,
                {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": CRON_PARENT},
            )
            await _drain(orch)
            _assert_parent_untouched(orch, stream)
        settled = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [[d.agent_id for d in batch] for batch in settled] == [["a1"]]

    @pytest.mark.asyncio
    async def test_the_orphan_waits_for_the_original_report_before_releasing_the_hold(
        self,
    ) -> None:
        """The hold is taken inside the run's own terminal report, which reads
        ``_delivery_queued`` once ``_on_done`` returns to decide whether to write
        the ``delivered`` mark. The orphan delivery waits for that report, and
        redelivers on a separate ticket, so the original record's flags are never
        touched and a failed reinjection is never tombstoned."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        release_report = asyncio.Event()
        seen: list[bool] = []

        async def _terminal_report() -> None:
            await on_done(info)
            # The original report is still finishing (e.g. the idle reset).
            await release_report.wait()
            seen.append(info._delivery_queued)

        report = asyncio.get_running_loop().create_task(_terminal_report())
        mgr._report_owners = {report: info}
        with _stream():
            await asyncio.sleep(0)
            assert info._delivery_queued is True
            orch.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no runtime"))
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            for _ in range(5):
                await asyncio.sleep(0)
            # The reinjection has not run: the report still owns the hold.
            orch.sessions.get_or_create.assert_not_awaited()
            release_report.set()
            await asyncio.wait_for(report, timeout=5)
            await _drain(orch)
        assert seen == [True]
        # The failure was recorded on the ticket; the original is untouched.
        assert info._delivery_queued is True
        assert info._report_undelivered is False
        mgr.settle_queued_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_released_result_the_route_failed_to_inject_stays_undelivered(
        self,
    ) -> None:
        """A cron injection that fails marks the run undelivered; the release
        path must leave its folder un-tombstoned for the restart replay."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        info = _info("a1", CRON_PARENT)
        with _stream():
            await on_done(info)
            orch.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no runtime"))
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            await _drain(orch)
        orch.sessions.get_or_create.assert_awaited()
        assert info._delivery_queued is True
        mgr.settle_queued_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_collection_whose_close_never_arrives_delivers_what_it_held(self) -> None:
        now = [0.0]
        registry = InlineCollections(clock=lambda: now[0])
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 60)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            _assert_parent_untouched(orch, stream)
            # The closing mark was lost; the collection outlives its bound.
            now[0] = 60 + COLLECTION_GRACE_SECS + 1
            registry.hold(CRON_PARENT, "other", object())
            await _drain(orch)
            stream.assert_awaited_once()


class TestApiSpawnReservesBeforeTheRunExists:
    def _mgr(self, registry: InlineCollections, *, refuse: bool = False) -> Any:
        mgr = _mgr()
        mgr.inline_collections = registry
        mgr.reserve_inline_member = lambda parent, wait: (
            "a1" if registry.reserve(parent, "a1", wait) else ""
        )
        held_when_spawned: list[bool] = []

        def _spawn(task: str, **kw: Any) -> Any:
            # Only probes the reservation: whatever the hold keeps is the
            # handler's to release, so a refused spawn exercises its release.
            held_when_spawned.append(
                registry.hold(
                    CRON_PARENT, kw["_preassigned_id"], _info(kw["_preassigned_id"], CRON_PARENT)
                )
            )
            if refuse:
                return _run_info(id=kw["_preassigned_id"], done=True, error="cwd not allowed")
            return _run_info(id=kw["_preassigned_id"])

        mgr.spawn.side_effect = _spawn
        mgr.held_when_spawned = held_when_spawned
        return mgr

    def _call(self, mgr: Any) -> Any:
        body = {
            "task": "x",
            "parent_session": CRON_PARENT,
            "inline_collect": True,
            "max_wait": 600,
        }
        with patch.object(
            handlers, "_spawn_request_memory_mode", AsyncMock(return_value="persistent")
        ):
            return asyncio.run(handlers.api_spawn(_CoverageReq(_state(subagents=mgr), body)))

    def test_the_minted_id_is_held_before_spawn_starts_it(self) -> None:
        registry = InlineCollections()
        mgr = self._mgr(registry)
        resp = self._call(mgr)
        assert _payload(resp)["id"] == "a1"
        assert mgr.spawn.call_args.kwargs["_preassigned_id"] == "a1"
        assert mgr.held_when_spawned == [True]

    def test_a_refused_spawn_releases_its_reservation(self) -> None:
        registry = InlineCollections()
        mgr = self._mgr(registry, refuse=True)
        assert self._call(mgr).status == 400
        assert not registry.hold(CRON_PARENT, "a1", object())

    def test_a_spawn_that_raises_releases_its_reservation(self) -> None:
        registry = InlineCollections()
        mgr = self._mgr(registry)
        mgr.spawn.side_effect = RuntimeError("spawn failed")
        with pytest.raises(RuntimeError):
            self._call(mgr)
        assert CRON_PARENT not in registry._records

    def test_a_full_collection_refuses_before_spawning(self) -> None:
        registry = InlineCollections()
        for i in range(MAX_IDS_PER_PARENT):
            registry.reserve(CRON_PARENT, f"x{i}", 600)
        mgr = self._mgr(registry)
        resp = self._call(mgr)
        assert resp.status == 429
        assert _payload(resp)["code"] == "inline_collection_full"
        mgr.spawn.assert_not_called()


class TestOtherParentKinds:
    @pytest.mark.asyncio
    async def test_a_channel_thread_parent_is_held_the_same_way(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(SLACK_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", SLACK_PARENT))
            _assert_parent_untouched(orch, stream)

    @pytest.mark.asyncio
    async def test_a_dashboard_tab_parent_is_held_while_its_slot_is_idle(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "chat-1"
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        parent = "dashboard:chat-1"
        registry.reserve(parent, "a1", 600)
        with (
            patch("kiro_crew.slack.gateway.dashboard_slot_key", return_value="chat-1"),
            patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            await on_done(_info("a1", parent))
            await asyncio.sleep(0)
        # No turn was launched on the tab, and nothing was queued for one.
        assert slot.task is None
        slot.queue_append.assert_not_called()
        run_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_tabs_returned_ids_live_on_the_registry_like_any_parent(self) -> None:
        """One store for "the call returned this id": a tab's ids are consumed
        from the registry, and its synthesis turn is disarmed once none remain."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "chat-1"
        slot._queue = []
        slot._pending_synthesis = True
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        parent = "dashboard:chat-1"
        with (
            patch(
                "kiro_crew.dashboard.handlers.messaging.dashboard_slot_key", return_value="chat-1"
            ),
            patch("kiro_crew.slack.gateway.dashboard_slot_key", return_value="chat-1"),
            patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock) as run_chat,
        ):
            await _post(
                orch, mgr, {"ids": ["a1", "a2"], "released": ["a1", "a2"], "parent_session": parent}
            )
            assert registry.has_collected(parent)
            await on_done(_info("a1", parent))
            assert slot._pending_synthesis is True  # a2 is still to come
            await on_done(_info("a2", parent))
            await asyncio.sleep(0)
        assert slot._pending_synthesis is False
        assert not registry.has_collected(parent)
        slot.queue_append.assert_not_called()
        run_chat.assert_not_awaited()


class TestSpawnSubAgentsTool:
    def test_every_member_is_spawned_for_inline_collection_and_closed_with_the_call(
        self,
    ) -> None:
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
        ):
            mock_post.side_effect = [{"id": "a1"}, {"id": "a2"}, {}, {}]
            mock_get.return_value = {"done": True, "agent": "w", "result": "done"}
            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"}, {"prompt": "y"}]})
        spawns = [c.args[1] for c in mock_post.call_args_list if c.args[0] == "/api/spawn"]
        assert [(b["inline_collect"], b["max_wait"]) for b in spawns] == [(True, 7200.0)] * 2
        claim, commit = mock_post.call_args_list[-2:]
        assert claim.args == (
            "/api/spawn/mark-collected",
            {
                "ids": ["a1", "a2"],
                "released": ["a1", "a2"],
                "parent_session": CRON_PARENT,
                "phase": "claim",
            },
        )
        # A direct call has no dispatcher to report on its response: the result
        # is the caller's as soon as it returns, so the claim commits at once.
        assert commit.args == (
            "/api/spawn/mark-collected",
            {
                "ids": ["a1", "a2"],
                "released": ["a1", "a2"],
                "parent_session": CRON_PARENT,
                "phase": "commit",
            },
        )

    @pytest.mark.parametrize(("delivered", "phase"), [(True, "commit"), (False, "drop")])
    def test_the_claim_settles_only_on_the_dispatchers_word(
        self, delivered: bool, phase: str
    ) -> None:
        """Dispatched, the call claims its results and returns; only the
        dispatcher's report on the response commits or releases the claim."""
        import threading

        from kiro_crew import mcp_shared

        reported = threading.Event()
        mcp_shared._arm_response_outcome(1)
        try:
            with (
                patch("kiro_crew.mcp_core._post") as mock_post,
                patch("kiro_crew.mcp_core._get") as mock_get,
                patch("kiro_crew.mcp_core.sel"),
                patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
            ):

                def _record(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
                    if body.get("phase") in ("commit", "drop"):
                        reported.set()
                    return {"id": "a1"} if path == "/api/spawn" else {}

                mock_post.side_effect = _record
                mock_get.return_value = {"done": True, "agent": "w", "result": "done"}
                _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
                phases = [c.args[1].get("phase") for c in mock_post.call_args_list[1:]]
                assert phases == ["claim"]  # nothing settles while the response is unwritten
                mcp_shared._settle_response_outcome(1, delivered)
                assert reported.wait(timeout=10)
                last = mock_post.call_args_list[-1].args[1]
        finally:
            mcp_shared._settle_response_outcome(1, False)
        assert last == {
            "ids": ["a1"],
            "parent_session": CRON_PARENT,
            "phase": phase,
            "released": ["a1"],
        }

    def test_a_cancelled_call_still_closes_its_collection(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.object(spawn_mod, "is_tool_cancelled", return_value=True),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
        ):
            mock_post.side_effect = [{"id": "a1"}, {}]
            mock_get.return_value = {"done": False}
            with pytest.raises(spawn_mod.ToolCancelled):
                spawn_mod.spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
        closing = mock_post.call_args_list[-1]
        assert closing.args[1] == {
            "ids": [],
            "released": ["a1"],
            "parent_session": CRON_PARENT,
            "phase": "claim",
        }

    def test_a_call_cancelled_after_its_poll_returns_nothing(self) -> None:
        """Every member settled, then the turn was cancelled before the close
        (during the resume hold or the result reads). The results go to no
        turn, so the close names nothing returned and the held results are
        delivered as ordinary completions."""
        from kiro_crew.mcp_tools import spawn as spawn_mod

        checks = iter([False])
        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.object(spawn_mod, "is_tool_cancelled", side_effect=lambda: next(checks, True)),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
        ):
            mock_post.side_effect = [{"id": "a1"}, {}]
            mock_get.return_value = {"done": True, "agent": "w", "result": "done"}
            with pytest.raises(spawn_mod.ToolCancelled):
                spawn_mod.spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
        closing = mock_post.call_args_list[-1]
        assert closing.args[1] == {
            "ids": [],
            "released": ["a1"],
            "parent_session": CRON_PARENT,
            "phase": "claim",
        }

    def test_a_result_the_reply_truncation_cuts_is_not_claimed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Seven ordinary CJK results each under the summary threshold grow
        about sixfold under JSON escaping and pass the response cap, so the
        dispatcher cuts the tail. A member whose block is cut never reached the
        parent: it is released, never claimed."""
        ids = [f"a{i}" for i in range(7)]
        posts: list[dict[str, Any]] = []

        def _record(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            posts.append(body)
            return {"id": ids[len(posts) - 1]} if path == "/api/spawn" else {}

        with (
            patch("kiro_crew.mcp_core._post", side_effect=_record),
            patch("kiro_crew.mcp_core._get") as mock_get,
            patch("kiro_crew.mcp_core.sel"),
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": CRON_PARENT}),
            caplog.at_level("WARNING", logger="kiro_crew.mcp_tools.spawn"),
        ):
            mock_get.return_value = {"done": True, "agent": "w", "result": "汉" * 3000}
            _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"} for _ in ids]})
        claim = next(b for b in posts if b.get("phase") == "claim")
        assert claim["released"] == ids
        assert 0 < len(claim["ids"]) < len(ids)
        assert ids[-1] not in claim["ids"]
        (line,) = [r.getMessage() for r in caplog.records if "truncated" in r.getMessage()]
        assert f"{len(ids) - len(claim['ids'])} result(s)" in line

    def test_an_uncut_reply_claims_every_settled_member(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        assert spawn_mod._carried_in_reply(["x", "x"], {"a1": 0, "a2": 1}) == ["a1", "a2"]

    def test_the_cut_is_judged_by_position_at_the_exact_cap(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod
        from kiro_crew.validation import MAX_RESPONSE_LEN

        first = "a" * (MAX_RESPONSE_LEN - 2 - 10)
        # The second block ends exactly at the cap: kept whole. One more char cuts it.
        assert spawn_mod._carried_in_reply([first, "b" * 10, "c"], {"a1": 0, "a2": 1, "a3": 2}) == [
            "a1",
            "a2",
        ]
        assert spawn_mod._carried_in_reply([first, "b" * 11], {"a1": 0, "a2": 1}) == ["a1"]

    def test_a_failed_close_is_retried(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core.time") as mock_time,
        ):
            mock_post.side_effect = [OSError("refused"), {}]
            spawn_mod._close_collection(CRON_PARENT, ["a1"], ["a1"])
        assert mock_post.call_count == 2
        mock_time.sleep.assert_called_once()

    def test_a_close_answered_with_an_error_body_is_retried(self) -> None:
        from kiro_crew.mcp_tools import spawn as spawn_mod

        with (
            patch("kiro_crew.mcp_core._post") as mock_post,
            patch("kiro_crew.mcp_core.time") as mock_time,
        ):
            mock_post.side_effect = [
                {"error": "HTTP 503"},
                {"error": "connection refused", "transport_error": True},
                {"status": "ok"},
            ]
            spawn_mod._close_collection(CRON_PARENT, ["a1"], ["a1"])
        assert mock_post.call_count == 3
        assert mock_time.sleep.call_count == 2


def _bound(registry: InlineCollections, *, busy: Any = False) -> Any:
    """A manager double the registry delivers through, recording what reached it."""
    mgr = MagicMock()
    mgr._on_done = AsyncMock()
    mgr._sessions.is_busy = MagicMock(return_value=busy)
    mgr._report_owners = {}
    mgr.settle_queued_delivery = AsyncMock()
    registry.bind(mgr)
    return mgr


async def _settled(registry: InlineCollections) -> None:
    while registry._tasks:
        await asyncio.wait_for(asyncio.gather(*registry._tasks), timeout=10)


class TestInlineCollections:
    @pytest.mark.asyncio
    async def test_held_then_collected_is_settled_by_the_registry(self) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve("p", "a1", 60)
        reg.reserve("p", "a2", 60)
        assert reg.hold("p", "a1", _info("a1", "p"))
        reg.finish("p", ["a1", "a2"], ["a1", "a2"])
        mgr.settle_queued_delivery.assert_not_awaited()  # claimed, not settled
        settles = reg.commit("p", ["a1", "a2"], True)
        assert await asyncio.gather(*settles) == ["delivered"]
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [d.agent_id for d in batch] == ["a1"]
        mgr._on_done.assert_not_awaited()
        assert not reg.hold("p", "a2", _info("a2", "p"))

    @pytest.mark.asyncio
    async def test_an_abandoned_collection_expires_and_delivers_what_it_held(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        mgr = _bound(reg)
        reg.reserve("p", "a1", 60)
        reg.reserve("p", "a2", 60)
        held = _info("a1", "p")
        assert reg.hold("p", "a1", held)
        now[0] = 60 + COLLECTION_GRACE_SECS + 1
        assert not reg.hold("p", "a2", _info("a2", "p"))
        await _settled(reg)
        (ticket,) = [c.args[0] for c in mgr._on_done.await_args_list]
        # Redelivered on a separate ticket: the original record is untouched.
        assert ticket is not held and ticket.id == "a1"
        assert held._delivery_queued is False

    def test_a_full_collection_refuses_a_new_member(self) -> None:
        reg = InlineCollections()
        assert all(reg.reserve("p", f"a{i}", 60) for i in range(MAX_IDS_PER_PARENT))
        assert not reg.reserve("p", "late", 60)
        # Re-reserving a member it already holds is not a new member.
        assert reg.reserve("p", "a0", 60)

    def test_collected_ids_per_parent_are_bounded_and_the_overflow_is_counted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reg = InlineCollections()
        ids = [f"b{i}" for i in range(MAX_IDS_PER_PARENT + 5)]
        with caplog.at_level("WARNING", logger="kiro_crew.subagent_inline_collection"):
            assert reg.record_collected("q", ids) == 5
        (line,) = [r.getMessage() for r in caplog.records]
        assert "q" in line and "5 not recorded" in line
        reg.commit("q", ids, True)
        kept = sum(reg.consume_collected("q", aid) for aid in ids)
        assert kept == MAX_IDS_PER_PARENT

    def test_a_returned_id_whose_completion_never_comes_is_forgotten(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        reg.record_collected("p", ["a1", "a2"])
        reg.commit("p", ["a1", "a2"], True)
        assert reg.consume_collected("p", "a1")
        now[0] = COLLECTED_TTL_SECS + 1
        assert not reg.consume_collected("p", "a2")


class TestParentTeardownFencesHeldResults:
    @pytest.mark.asyncio
    async def test_a_held_member_whose_record_was_evicted_is_still_fenced(self) -> None:
        """Completed-run retention can evict a held member's record from
        ``_agents`` while its completion waits on the registry. The fence lives
        on the registry, keyed by parent, so the parent's teardown snapshot still
        drops it and its later release or expiry injects nothing."""
        from overload_fakes import mock_ctx, mock_sessions

        from kiro_crew.subagent import SubagentManager

        mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx())
        mgr._on_done = AsyncMock()
        reg = mgr.inline_collections
        reg.reserve(CRON_PARENT, "a1", 600)
        assert reg.hold(CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        assert "a1" not in mgr._agents
        # Another parent's held member is left alone.
        reg.reserve(SLACK_PARENT, "b1", 600)
        reg.hold(SLACK_PARENT, "b1", _info("b1", SLACK_PARENT))
        mgr.snapshot_teardown_children(CRON_PARENT)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        assert reg.commit(CRON_PARENT, ["a1"], True) == []
        await _settled(reg)
        mgr._on_done.assert_not_awaited()
        assert CRON_PARENT not in reg._records
        assert SLACK_PARENT in reg._records

    @pytest.mark.asyncio
    async def test_a_released_orphan_is_still_fenced_while_it_waits_to_be_delivered(
        self,
    ) -> None:
        """A cancelled call releases a held member whose run record was already
        evicted. Its delivery is waiting for the parent's turn to end when the
        parent is torn down: the fence is read at the point of delivery."""
        from overload_fakes import mock_ctx, mock_sessions

        from kiro_crew.subagent import SubagentManager

        sessions = mock_sessions()
        busy = [True]
        sessions.is_busy = MagicMock(side_effect=lambda _key: busy[0])
        mgr = SubagentManager(sessions=sessions, ctx_builder=mock_ctx())
        mgr._on_done = AsyncMock()
        reg = mgr.inline_collections
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.hold(CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        with patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0):
            reg.finish(CRON_PARENT, ["a1"], [])
            await asyncio.sleep(0)
            mgr.snapshot_teardown_children(CRON_PARENT)
            busy[0] = False
            await _settled(reg)
        mgr._on_done.assert_not_awaited()
        assert CRON_PARENT not in reg._records

    @pytest.mark.asyncio
    async def test_a_returned_result_is_settled_even_if_its_parent_is_retired_first(
        self,
    ) -> None:
        """The call's response carrying the held result was written, then the
        parent was torn down before the settle ran. The result reached the
        parent, so it is marked delivered; leaving it un-tombstoned would make
        restart recovery replay a result the parent already read."""
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.hold(CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        (settle,) = reg.commit(CRON_PARENT, ["a1"], True)
        reg.retire(CRON_PARENT)
        assert await settle == "delivered"
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [d.agent_id for d in batch] == ["a1"]
        mgr._on_done.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_late_completion_of_a_retired_member_is_owned_and_dropped(self) -> None:
        """Torn down while the member still ran: its completion is still owned
        (never injected into a replacement conversation under the same key), and
        an owed report is cleared."""
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.retire(CRON_PARENT)
        late = _info("a1", CRON_PARENT)
        late._report_owed = True
        assert reg.hold(CRON_PARENT, "a1", late)
        mgr._admission.taskq_clear_owed_reports.assert_called_once_with(["a1"])
        assert CRON_PARENT not in reg._records


class TestReleasedOrphansAreTrackedUntilDelivered:
    @pytest.mark.asyncio
    async def test_the_registry_forgets_the_record_when_the_delivery_ends(self) -> None:
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream():
            await on_done(_info("a1", CRON_PARENT))
            await _post(orch, mgr, {"ids": [], "released": ["a1"], "parent_session": CRON_PARENT})
            assert registry._records[CRON_PARENT]["a1"].state == "delivering"
            await _drain(orch)
        assert CRON_PARENT not in registry._records


class TestAClaimSettlesOnlyOnceTheResponseIsWritten:
    """A claim settles only after the dispatcher wrote the response."""

    @pytest.mark.asyncio
    async def test_a_dropped_response_releases_its_held_results_for_ordinary_delivery(
        self,
    ) -> None:
        """spawn.py:1720: cancelled while the close was in flight. The claim is
        released, so the result is delivered as an ordinary completion and its
        delivered mark is written only by the route that took it."""
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        held = _info("a1", CRON_PARENT)
        reg.hold(CRON_PARENT, "a1", held)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        mgr.settle_queued_delivery.assert_not_awaited()
        assert reg.commit(CRON_PARENT, ["a1"], False) == []
        await _settled(reg)
        (ticket,) = [c.args[0] for c in mgr._on_done.await_args_list]
        assert ticket.id == "a1" and ticket is not held
        (batch,) = [c.args[0] for c in mgr.settle_queued_delivery.await_args_list]
        assert [d.agent_id for d in batch] == ["a1"]

    @pytest.mark.asyncio
    async def test_a_dropped_response_whose_completion_is_late_injects_it_normally(self) -> None:
        reg = InlineCollections()
        _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        reg.commit(CRON_PARENT, ["a1"], False)
        assert CRON_PARENT not in reg._records
        late = _info("a1", CRON_PARENT)
        assert not reg.hold(CRON_PARENT, "a1", late)
        assert not reg.consume_collected(CRON_PARENT, "a1")

    @pytest.mark.asyncio
    async def test_a_completion_landing_between_claim_and_commit_is_held_then_settled(
        self,
    ) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        assert reg.has_collected(CRON_PARENT)
        assert reg.hold(CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        (settle,) = reg.commit(CRON_PARENT, ["a1"], True)
        assert await settle == "delivered"
        mgr._on_done.assert_not_awaited()
        assert CRON_PARENT not in reg._records

    @pytest.mark.asyncio
    async def test_a_claim_nobody_confirms_expires_into_ordinary_delivery(self) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.hold(CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        now[0] = CLAIM_TTL_SECS - 1
        reg._expire(CRON_PARENT)
        assert reg._records[CRON_PARENT]["a1"].state == "claimed"
        now[0] = CLAIM_TTL_SECS + 1
        reg._expire(CRON_PARENT)
        await _settled(reg)
        mgr._on_done.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_parent_retired_before_the_commit_settles_nothing(self) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.hold(CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        reg.retire(CRON_PARENT)
        assert reg.commit(CRON_PARENT, ["a1"], True) == []
        await _settled(reg)
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._on_done.assert_not_awaited()

    def test_a_late_completion_of_a_claim_retired_before_its_commit_is_fenced(self) -> None:
        reg = InlineCollections()
        mgr = _bound(reg)
        reg.reserve(CRON_PARENT, "a1", 600)
        reg.finish(CRON_PARENT, ["a1"], ["a1"])
        reg.retire(CRON_PARENT)
        assert reg.commit(CRON_PARENT, ["a1"], True) == []
        # Not consumed as a returned result (no live turn read the response),
        # and not let go either: the registry still owns the late completion
        # and its fence drops it.
        assert not reg.consume_collected(CRON_PARENT, "a1")
        assert reg.hold(CRON_PARENT, "a1", _info("a1", CRON_PARENT))
        assert CRON_PARENT not in reg._records
        mgr.settle_queued_delivery.assert_not_awaited()
        mgr._on_done.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_handler_claims_then_commits(self) -> None:
        """Through the real handler: the claim alone settles nothing; the commit does."""
        registry = InlineCollections()
        orch, on_done, mgr = _gateway(registry)
        settle = mgr.settle_queued_delivery
        registry.reserve(CRON_PARENT, "a1", 600)
        with _stream() as stream:
            await on_done(_info("a1", CRON_PARENT))
            body = {"ids": ["a1"], "released": ["a1"], "parent_session": CRON_PARENT}
            assert (await _post(orch, mgr, {**body, "phase": "claim"}))["status"] == "ok"
            settle.assert_not_awaited()
            commit = {"ids": ["a1"], "parent_session": CRON_PARENT, "phase": "commit"}
            await _post(orch, mgr, commit)
            await _drain(orch)
        settle.assert_awaited_once()
        _assert_parent_untouched(orch, stream)

    @pytest.mark.asyncio
    async def test_the_handler_refuses_an_unknown_phase_and_a_non_object_body(self) -> None:
        registry = InlineCollections()
        orch, _on_done, mgr = _gateway(registry)
        bad = {"ids": ["a1"], "parent_session": CRON_PARENT, "phase": "settle"}
        assert (await _post(orch, mgr, bad))["code"] == "invalid_phase"
        state = orch.dashboard_state
        resp = await handlers.api_spawn_mark_collected(_Req(state, ["a1"]))
        assert resp.status == 400


class TestRetainedRecordsHoldTheirCapacity:
    """Every retained record holds its place at admission."""

    def test_retained_returned_records_count_at_admission(self) -> None:
        reg = InlineCollections()
        ids = [f"r{i}" for i in range(MAX_IDS_PER_PARENT)]
        reg.record_collected(CRON_PARENT, ids)
        reg.commit(CRON_PARENT, ids, True)
        assert not reg.reserve(CRON_PARENT, "late", 60)

    def test_an_admitted_member_keeps_its_place_when_its_reservation_is_claimed(self) -> None:
        reg = InlineCollections()
        ids = [f"a{i}" for i in range(MAX_IDS_PER_PARENT)]
        assert all(reg.reserve(CRON_PARENT, aid, 60) for aid in ids)
        reg.finish(CRON_PARENT, ids, ids)
        reg.commit(CRON_PARENT, ids, True)
        assert all(reg.consume_collected(CRON_PARENT, aid) for aid in ids)
