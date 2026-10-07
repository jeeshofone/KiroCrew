"""A result ``spawn_sub_agents`` returned inline reaches its parent exactly once.

Each test is one ordering an independent review found where a result the call
returned could be lost or delivered a second time:

* a member the reply's truncation cut is never committed;
* a torn response frame is never reported as written;
* a written response settles its members even when its claim arrives late or
  is lost, so the result is not redelivered when the claim later expires;
* a hook registered after the dispatcher dropped the response hears the drop,
  never the direct-call commit;
* a claim retired before its commit keeps its late completion fenced;
* a run's hung terminal report cannot keep a released result forever.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import mcp_shared
from kiro_crew.mcp_core import _call_tool
from kiro_crew.subagent_inline_collection import (
    CLAIM_TTL_SECS,
    COLLECTION_GRACE_SECS,
    InlineCollections,
)
from kiro_crew.validation import build_tool_response

PARENT = "cron:job1:run1"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _registry() -> tuple[InlineCollections, _Clock, Any]:
    clock = _Clock()
    reg = InlineCollections(clock=clock)
    mgr = MagicMock()
    mgr._on_done = AsyncMock()
    mgr._sessions = MagicMock()
    mgr._sessions.is_busy = MagicMock(return_value=False)
    mgr._report_owners = {}
    mgr.settle_queued_delivery = AsyncMock()
    reg.bind(mgr)
    return reg, clock, mgr


def _info(aid: str) -> Any:
    from kiro_crew.subagent import SubagentInfo

    # A real run record: the registry holds only a dataclass it can bound.
    info = SubagentInfo(id=aid, task="t", parent_session_key=PARENT)
    info.done = True
    info.elapsed = 1.0
    info._delivery_queued = True
    return info


async def _drain(reg: InlineCollections) -> None:
    while reg._tasks:
        await asyncio.wait_for(asyncio.gather(*reg._tasks), timeout=10)


def _settled_ids(mgr: Any) -> list[str]:
    return [d.agent_id for call in mgr.settle_queued_delivery.await_args_list for d in call.args[0]]


def test_a_member_cut_by_response_truncation_is_not_committed() -> None:
    n = 7
    cjk = "漢" * 3000  # at the completion keep threshold, so not summarised
    spawned = iter(f"a{i}" for i in range(1, n + 1))
    posts: list[dict[str, Any]] = []

    def _post(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        posts.append(body)
        return {"id": next(spawned)} if path == "/api/spawn" else {}

    def _get(path: str, **_kw: Any) -> dict[str, Any]:
        aid = path.rsplit("/", 1)[1]
        return {"done": True, "agent": aid, "result": f"{aid}:{cjk}"}

    with (
        patch("kiro_crew.mcp_core._post", side_effect=_post),
        patch("kiro_crew.mcp_core._get", side_effect=_get),
        patch("kiro_crew.mcp_core.sel"),
        patch.dict("os.environ", {"KIROCREW_SESSION_KEY": PARENT}),
    ):
        text = _call_tool("spawn_sub_agents", {"agents": [{"prompt": "x"}] * n})
    sent = build_tool_response(text)["content"][0]["text"]  # what the dispatcher writes
    committed = next(b for b in reversed(posts) if b.get("phase") == "commit")["ids"]
    missing = [aid for aid in committed if f'"agent": "{aid}"' not in sent]
    assert not missing, f"committed but cut from the written response: {missing}"
    assert len(committed) < n  # the cut member is released, not claimed


def test_a_torn_frame_is_not_reported_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    def _torn(fd: int, payload: bytes) -> int:
        exc = BrokenPipeError(32, "pipe closed")
        exc.bytes_written = 10  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(mcp_shared, "_stdout_fd", 99)
    monkeypatch.setattr(mcp_shared, "_write_all", _torn)
    outcome = mcp_shared.respond(1, {"content": [{"type": "text", "text": "x"}]})
    assert outcome is False


@pytest.mark.asyncio
async def test_a_commit_that_overtakes_its_claim_is_not_redelivered() -> None:
    reg, clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    assert reg.hold(PARENT, "a1", _info("a1"))
    # The response naming a1 was written; its commit lands first ...
    reg.commit(PARENT, ["a1"], True, released=["a1"])
    # ... and the slow claim after it.
    reg.finish(PARENT, ["a1"], ["a1"])
    clock.now += CLAIM_TTL_SECS + 1
    reg._expire(PARENT)
    await _drain(reg)
    mgr._on_done.assert_not_awaited()
    assert _settled_ids(mgr) == ["a1"]


@pytest.mark.asyncio
async def test_a_written_commit_after_a_lost_claim_settles_and_is_not_redelivered() -> None:
    reg, clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 7200)
    assert reg.hold(PARENT, "a1", _info("a1"))
    # Every claim attempt failed; the response was written; the commit arrives.
    reg.commit(PARENT, ["a1"], True, released=["a1"])
    assert PARENT not in reg._records or reg._records[PARENT]["a1"].state != "held"
    clock.now += 7200 + COLLECTION_GRACE_SECS + 1
    reg._expire(PARENT)
    await _drain(reg)
    mgr._on_done.assert_not_awaited()
    assert _settled_ids(mgr) == ["a1"]


@pytest.mark.asyncio
async def test_a_written_commit_before_the_completion_consumes_it() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    reg.commit(PARENT, ["a1"], True, released=["a1"])  # the claim was lost
    reg.finish(PARENT, ["a1"], ["a1"])  # a late claim changes nothing
    assert not reg.hold(PARENT, "a1", _info("a1"))
    assert reg.consume_collected(PARENT, "a1")
    await _drain(reg)
    mgr._on_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dropped_commit_after_a_lost_claim_delivers_the_held_result() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    assert reg.reserve(PARENT, "a2", 60)
    assert reg.hold(PARENT, "a1", _info("a1"))
    assert reg.hold(PARENT, "a2", _info("a2"))
    reg.commit(PARENT, ["a1"], False, released=["a1", "a2"])
    await _drain(reg)
    # Both go out by the ordinary route; neither is settled as returned.
    assert sorted(c.args[0].id for c in mgr._on_done.await_args_list) == ["a1", "a2"]


def test_a_late_registration_after_a_dropped_response_does_not_commit() -> None:
    from kiro_crew.mcp_tools import spawn as spawn_mod

    mcp_shared._arm_response_outcome(1)
    mcp_shared._settle_response_outcome(1, False)  # EOF: dropped before the tool returned
    with patch("kiro_crew.mcp_core._post", return_value={}) as post:
        spawn_mod._commit_when_answered(PARENT, ["a1"], ["a1"])
    phases = [c.args[1]["phase"] for c in post.call_args_list]
    assert phases == ["drop"]


def test_a_direct_call_in_a_process_that_never_dispatched_commits() -> None:
    from kiro_crew.mcp_tools import spawn as spawn_mod

    with patch("kiro_crew.mcp_core._post", return_value={}) as post:
        spawn_mod._commit_when_answered(PARENT, ["a1"], ["a1"])
    assert [c.args[1]["phase"] for c in post.call_args_list] == ["commit"]


def test_retire_then_commit_leaves_the_late_completion_fenced() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    reg.finish(PARENT, ["a1"], ["a1"])  # claimed, completion not yet seen
    reg.retire(PARENT)
    reg.commit(PARENT, ["a1"], True)
    assert reg.hold(PARENT, "a1", _info("a1"))  # owned, and dropped by the fence
    mgr._on_done.assert_not_called()
    mgr.settle_queued_delivery.assert_not_called()


@pytest.mark.asyncio
async def test_a_hung_terminal_report_does_not_keep_a_released_result() -> None:
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    info = _info("a1")
    assert reg.hold(PARENT, "a1", info)
    never = asyncio.Event()
    report = asyncio.get_running_loop().create_task(never.wait())
    mgr._report_owners = {report: info}
    try:
        with (
            patch("kiro_crew.subagent.INJECTION_TIMEOUT", 0),
            patch("kiro_crew.subagent_inline_collection.TERMINAL_REPORT_GRACE_SECS", 0.01),
        ):
            reg.finish(PARENT, ["a1"], [])  # not returned: released for ordinary delivery
            await _drain(reg)
        mgr._on_done.assert_awaited_once()
        assert PARENT not in reg._records
        assert not report.done()  # the hung report itself is never cancelled
    finally:
        report.cancel()


@pytest.mark.parametrize("answer", ["written", "unanswered"])
def test_the_outcome_report_carries_the_calls_caller_identity(answer: str) -> None:
    """A pooled backend's only credential is the per-call caller block, so the
    commit or drop the hook sends from its own thread must still carry it."""
    import threading

    from kiro_crew import mcp_core
    from kiro_crew.mcp_caller import CallerContext, set_current_caller
    from kiro_crew.mcp_tools import spawn as spawn_mod

    caller = CallerContext(session_key=PARENT, from_gateway=True, session_token="forwarded-token")
    sent: list[tuple[str, dict[str, str]]] = []
    reported = threading.Event()

    def _post(path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        sent.append((body["phase"], mcp_core._session_token_header()))
        reported.set()
        return {}

    def _tool_thread() -> None:
        # The dispatcher's worker: the caller is set for the call and cleared
        # after it, before the loop settles the response.
        set_current_caller(caller)
        mcp_shared._arm_response_outcome(1)
        try:
            spawn_mod._commit_when_answered(PARENT, ["a1"], ["a1"])
        finally:
            set_current_caller(None)

    with patch("kiro_crew.mcp_core._post", side_effect=_post):
        worker = threading.Thread(target=_tool_thread)
        worker.start()
        worker.join(timeout=10)
        if answer == "written":
            mcp_shared._settle_response_outcome(1, True)
        else:
            mcp_shared._drop_unsettled_arms()  # the loop ended unanswered: a drop
        assert reported.wait(timeout=10)
    from kiro_crew.session_token_sig import session_token_header

    expected = session_token_header("forwarded-token")
    assert expected, "the header helper produced nothing to compare against"
    assert sent == [("commit" if answer == "written" else "drop", expected)]


def _real_info(aid: str, result: str, result_path: str) -> Any:
    from kiro_crew.subagent import SubagentInfo

    info = SubagentInfo(id=aid, task="t", parent_session_key=PARENT)
    info.done = True
    info.result = result
    info.result_path = result_path
    return info


@pytest.mark.asyncio
async def test_a_result_whose_transcript_is_on_disk_is_kept_whole(tmp_path) -> None:
    """``completion_keep_chars=0`` leaves the run's result uncapped. While it
    fits the budget the held copy keeps all of it, so a release carries what
    ordinary delivery would. The run object itself is not kept."""
    import gc
    import weakref

    transcript = tmp_path / "result.txt"
    transcript.write_text("the whole result", encoding="utf-8")
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    text = "x" * 5_000_000 + "THE-END"
    info = _real_info("a1", text, str(transcript))
    run = weakref.ref(info)
    assert reg.hold(PARENT, "a1", info)
    del info
    gc.collect()
    assert run() is None, "the registry kept the run object alive"
    kept = reg._records[PARENT]["a1"].completion
    assert kept.result == text and kept.result_truncated is False
    assert kept.result_path == str(transcript)
    assert reg._held_bytes == len(text) + len(
        str(transcript).encode("utf-8")
    )  # the pointer is charged too
    reg.finish(PARENT, ["a1"], [])  # not returned: the route gets the bounded copy
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.result == text
    assert reg._held_bytes == 0, "the delivered record still charges the budget"


@pytest.mark.asyncio
async def test_a_transient_5mb_result_is_kept_whole_and_delivered_whole() -> None:
    """Incognito and temporary runs write no ``result.txt``, so the held text is
    the only copy: it is kept and released in full, charged to the budget."""
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    text = "h" * 1_000 + "x" * 5_000_000 + "THE-END"
    info = _real_info("a1", text, "")
    info.memory_mode = "incognito"
    assert reg.hold(PARENT, "a1", info)
    kept = reg._records[PARENT]["a1"].completion
    assert kept.result == text and kept.result_truncated is False
    assert reg._held_bytes == len(text)
    reg.finish(PARENT, ["a1"], [])
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.result == text
    assert reg._held_bytes == 0


def test_a_result_over_the_budget_is_held_in_part(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One that would exceed ``HELD_BYTES_BUDGET`` is still held, so it is never
    routed into the blocked turn: it keeps what fits, charges only that, and is
    flagged. Freed budget is held again."""
    import logging

    from kiro_crew.subagent_inline_collection import HELD_BYTES_BUDGET

    reg, _clock, _mgr = _registry()
    for aid in ("a1", "a2"):
        assert reg.reserve(PARENT, aid, 60)
    big = "y" * (HELD_BYTES_BUDGET // 2 + 1)
    assert reg.hold(PARENT, "a1", _real_info("a1", big, ""))
    second = _real_info("a2", big, "")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_inline_collection"):
        assert reg.hold(PARENT, "a2", second) is True
    assert second.result == big, "a partial hold cut the run's own result"
    rec = reg._records[PARENT]["a2"]
    assert rec.result_cut_from == len(big)
    assert 0 < rec.held_bytes <= HELD_BYTES_BUDGET - len(big)
    assert reg._held_bytes == len(big) + rec.held_bytes <= HELD_BYTES_BUDGET
    assert any("is held as" in r.message for r in caplog.records)
    reg.retire(PARENT)  # drops both held results and their charges
    assert reg._held_bytes == 0


def test_a_result_path_naming_a_deleted_file_is_transient(tmp_path) -> None:
    transcript = tmp_path / "result.txt"
    transcript.write_text("gone soon", encoding="utf-8")
    transcript.unlink()
    reg, _clock, _mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    text = "z" * 50_000
    assert reg.hold(PARENT, "a1", _real_info("a1", text, str(transcript)))
    kept = reg._records[PARENT]["a1"].completion
    assert kept.result == text and kept.result_truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize("result_path", ["", "/no/transcript/was/written/result.txt"])
async def test_a_held_result_with_no_transcript_is_released_whole(result_path: str) -> None:
    """Incognito and temporary runs write no ``result.txt``, so the held text is
    the only copy: a dropped reply releases all of it, not a preview."""
    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    text = "".join(f"line {n}\n" for n in range(2_000))  # about 17,000 chars
    assert reg.hold(PARENT, "a1", _real_info("a1", text, result_path))
    reg.finish(PARENT, ["a1"], [])
    await _drain(reg)
    (ticket,) = mgr._on_done.await_args.args
    assert ticket.result == text and ticket.result_truncated is False


@pytest.mark.asyncio
async def test_a_completion_copy_already_cut_keeps_its_flag() -> None:
    """A run whose completion copy was cut keeps ``result_truncated``, so the
    route still sends the summary pointing at ``result_path``."""
    reg, _clock, _mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    info = _real_info("a1", "head of the result", "/r/result.txt")
    info.result_truncated = True
    assert reg.hold(PARENT, "a1", info)
    kept = reg._records[PARENT]["a1"].completion
    assert kept.result == "head of the result" and kept.result_truncated is True


@pytest.mark.asyncio
async def test_a_released_result_still_waits_for_its_own_terminal_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record keeps a snapshot, so the run's report is found by its id: the
    bounded wait runs (and says so when it gives up) instead of being skipped."""
    import logging

    reg, _clock, mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    info = _info("a1")
    assert reg.hold(PARENT, "a1", info)
    assert reg._records[PARENT]["a1"].completion is not info
    never = asyncio.Event()
    report = asyncio.get_running_loop().create_task(never.wait())
    mgr._report_owners = {report: info}
    try:
        with (
            caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_inline_collection"),
            patch("kiro_crew.subagent.INJECTION_TIMEOUT", 0),
            patch("kiro_crew.subagent_inline_collection.TERMINAL_REPORT_GRACE_SECS", 0.01),
        ):
            reg.finish(PARENT, ["a1"], [])
            await _drain(reg)
        assert any("terminal report is still running" in r.message for r in caplog.records)
        mgr._on_done.assert_awaited_once()
    finally:
        report.cancel()


@pytest.mark.asyncio
async def test_a_held_result_drops_the_live_runs_follow_up_queue() -> None:
    """``spawn_steer(mode="follow_up")`` appends without a count cap, so a held
    copy keeps none of it, and the live run's own queue is left as it was."""
    reg, _clock, _mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    info = _real_info("a1", "done", "/r")
    queue = [f"follow-up {n}" for n in range(10_000)]
    info.pending_followups = queue
    info.streaming_text = "s" * 40_000
    assert reg.hold(PARENT, "a1", info)
    kept = reg._records[PARENT]["a1"].completion
    assert kept.pending_followups == [] and kept.streaming_text == ""
    assert kept.delegation == {} and kept.allowed_tools == []
    assert info.pending_followups is queue and len(queue) == 10_000


def test_every_object_a_held_copy_keeps_is_named_bounded() -> None:
    """Every object-valued run field is reset on the held copy to its default,
    except the kept set; a field added later is reset without being listed."""
    import dataclasses

    from kiro_crew.subagent import SubagentInfo
    from kiro_crew.subagent_inline_collection import (
        _HELD_KEPT_OBJECTS,
        _SCALARS,
        _held_snapshot,
    )

    assert _HELD_KEPT_OBJECTS == {"_digest_settle_deliveries"}
    info = _real_info("a1", "done", "/r")
    info._tool_tracker.dispatch("t1", MagicMock(), {"content": "z" * 100_000})
    assert info._tool_tracker.any_active
    live_tracker = info._tool_tracker
    snap = _held_snapshot(info)
    for f in dataclasses.fields(SubagentInfo):
        value = getattr(snap, f.name, None)
        if f.name in _HELD_KEPT_OBJECTS or isinstance(value, _SCALARS):
            continue
        assert value is not getattr(info, f.name), f"{f.name} is shared with the live run"
    assert snap._tool_tracker is not live_tracker
    assert info._tool_tracker is live_tracker  # the live run keeps its tracker


def test_every_text_field_a_held_copy_keeps_is_capped() -> None:
    """Every text field but ``result``, ``error`` and ``result_path`` (task, tool
    title) is cut to the held cap, so a text field added later is bounded
    unlisted. ``error`` has its own larger cap; ``result_path`` is kept whole."""
    import dataclasses

    from kiro_crew.subagent import SubagentInfo
    from kiro_crew.subagent_inline_collection import (
        HELD_ERROR_MAX_CHARS,
        HELD_TEXT_MAX_CHARS,
        _held_snapshot,
    )

    assert HELD_TEXT_MAX_CHARS == 512
    info = _real_info("a1", "done", "")
    text_fields = [
        f.name for f in dataclasses.fields(SubagentInfo) if isinstance(getattr(info, f.name), str)
    ]
    assert {"task", "error", "last_tool", "_raw_task", "result_path"} <= set(text_fields)
    for name in text_fields:
        setattr(info, name, "w" * 100_000)
    snap = _held_snapshot(info)
    for name in text_fields:
        assert len(getattr(info, name)) == 100_000, f"{name} was cut on the live run"
        if name in ("result", "result_path"):
            assert getattr(snap, name) == "w" * 100_000, name
            continue
        cap = HELD_ERROR_MAX_CHARS if name == "error" else HELD_TEXT_MAX_CHARS
        assert len(getattr(snap, name)) <= cap, name
        kept = getattr(snap, name)
        assert kept == "" or kept.endswith("\u2026"), name  # emptied, or cut
    assert snap.last_tool == "w" * (HELD_TEXT_MAX_CHARS - 1) + "\u2026"


def _deep_chars(value: Any, seen: set[int] | None = None) -> int:
    """Text and container entries reachable from *value*, counted once each."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    if isinstance(value, (str, bytes)):
        return len(value)
    if isinstance(value, dict):
        return len(value) + sum(
            _deep_chars(k, seen) + _deep_chars(v, seen) for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) + sum(_deep_chars(v, seen) for v in value)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _deep_chars(vars(value), seen)
    return 0


def test_a_held_record_stays_small_after_a_large_tool_call_follow_ups_and_a_long_stream() -> None:
    reg, _clock, _mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    info = _real_info("a1", "x" * 2_000_000, "")
    info._tool_tracker.dispatch("t1", MagicMock(), {"content": "z" * 1_000_000})
    info.pending_followups = [f"follow-up {n}" * 50 for n in range(5_000)]
    info.streaming_text = "s" * 1_000_000
    info.task = "t" * 500_000
    info.error = "e" * 500_000
    assert _deep_chars(info) > 5_000_000
    assert reg.hold(PARENT, "a1", info)
    kept = reg._records[PARENT]["a1"].completion
    assert not kept._tool_tracker.any_active
    assert kept.result == info.result  # no transcript: kept whole, charged to the budget
    kept.result = ""
    size = _deep_chars(kept)
    assert size < 16_000, size


@pytest.mark.parametrize("completion", [MagicMock(), object(), {"id": "a1"}, "a1"])
def test_a_completion_that_is_not_a_run_record_is_not_held(
    completion: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Its fields cannot be enumerated, so it is never copied whole into the
    registry: the hold refuses, and the route delivers it as an ordinary one."""
    import logging

    reg, _clock, _mgr = _registry()
    assert reg.reserve(PARENT, "a1", 60)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_inline_collection"):
        assert reg.hold(PARENT, "a1", completion) is False
    rec = reg._records[PARENT]["a1"]
    assert rec.completion is None and rec.state == "collecting"
    assert any("not a run record; not holding it" in r.message for r in caplog.records)
