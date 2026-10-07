"""A held result degrades its CONTENT under pressure, never its routing.

The parent's turn is blocked inside ``spawn_sub_agents`` while a member is
collected, so a completion handed to the ordinary route is injected into that
turn and then returned inline as well. Over ``HELD_BYTES_BUDGET`` the registry
therefore still owns the member and keeps less of its text. Under the budget it
keeps the text whole, so a released result carries exactly what ordinary
delivery would, in every branch of the route. The budget's accounting is
checked over seeded interleavings of every path that charges or discharges it.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import subagent_inline_collection as sic
from kiro_crew.slack.gateway import inline_collection_owns
from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_inline_collection import InlineCollections

PARENT = "cron:job-1"


def _manager(delivered: list[Any], busy: bool = False) -> Any:
    mgr = MagicMock()
    mgr._report_owners = {}
    mgr._sessions.is_busy = lambda _p: busy

    async def _on_done(ticket: Any) -> None:
        delivered.append(ticket)

    mgr._on_done = _on_done
    mgr.settle_queued_delivery = AsyncMock()
    return mgr


def _registry(delivered: list[Any], busy: bool = False) -> InlineCollections:
    reg = InlineCollections()
    mgr = _manager(delivered, busy)
    mgr.inline_collections = reg
    reg.bind(mgr)
    return reg


def _info(aid: str, result: str, result_path: str = "", **kw: Any) -> SubagentInfo:
    info = SubagentInfo(id=aid, task="t")
    info.parent_session_key = PARENT
    info.done = True
    info.result = result
    info.result_path = result_path
    for k, v in kw.items():
        setattr(info, k, v)
    return info


@pytest.fixture(autouse=True)
def _no_idle_poll(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(sic, "_ORPHAN_IDLE_POLL_SECS", 0)


async def _drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# --- over budget: still owned, so never injected into the blocked turn ------


@pytest.mark.asyncio
async def test_an_over_budget_completion_while_collecting_is_owned_not_injected(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion over HELD_BYTES_BUDGET is still owned: answering False would
    let the route inject it into the parent turn blocked inside
    spawn_sub_agents, and the same call returns it inline too."""
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 1000)
    delivered: list[Any] = []
    reg = _registry(delivered, busy=True)
    assert reg.reserve(PARENT, "a1", 60)
    info = _info("a1", "x" * 5000)  # no result_path: kept whole, over budget

    owned = inline_collection_owns(reg._manager, None, info)
    injected = 0 if owned else 1  # False -> the gateway route injects it now
    # The call polls a1 as done and returns its result inline.
    reg.finish(PARENT, {"a1"}, {"a1"})
    settles = reg.commit(PARENT, ["a1"], True)
    await asyncio.gather(*settles)
    inline = 1
    assert owned, "over-budget completion handed to the ordinary route while the parent is busy"
    assert injected + inline == 1


@pytest.mark.asyncio
async def test_an_over_budget_completion_after_the_claim_is_delivered_once(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 1000)
    delivered: list[Any] = []
    reg = _registry(delivered, busy=True)
    assert reg.reserve(PARENT, "a1", 60)
    reg.finish(PARENT, {"a1"}, {"a1"})  # claimed: the response names a1
    owned = inline_collection_owns(reg._manager, None, _info("a1", "y" * 5000))
    reg.commit(PARENT, ["a1"], True)  # the response was written
    rec = reg._records[PARENT]["a1"]
    # Never a RETURNED record waiting an hour for a completion already out.
    assert owned, "claimed result injected AND returned inline"
    assert rec.state != sic.RETURNED or not reg.has_collected(PARENT)


# --- under budget: released whole, in every branch of the route -------------


@pytest.mark.asyncio
async def test_a_released_result_keeps_its_tail(tmp_path) -> None:
    """``info.result`` is already the completion copy ``completion_keep`` trimmed
    (tail, or more than 3000 chars). A release under the budget carries all of
    it, so the final answer the ordinary route would send is there."""
    path = tmp_path / "result.txt"
    path.write_text("full transcript")
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    text = "H" * 4000 + "FINAL ANSWER"
    assert inline_collection_owns(reg._manager, None, _info("a1", text, str(path)))
    reg.finish(PARENT, {"a1"}, set())  # call ended without returning a1
    await _drain()
    assert len(delivered) == 1
    assert "FINAL ANSWER" in delivered[0].result


@pytest.mark.asyncio
async def test_a_user_stopped_partial_is_released_whole(tmp_path) -> None:
    """The route's user_stopped and error+partial branches print info.result
    verbatim, so a released partial is the run's own text, uncut."""
    path = tmp_path / "result.txt"
    path.write_text("full transcript")
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    text = "P" * 4000 + "TAIL"
    info = _info("a1", text, str(path), user_stopped=True)
    assert inline_collection_owns(reg._manager, None, info)
    reg.finish(PARENT, {"a1"}, set())
    await _drain()
    assert delivered and delivered[0].result == text


# --- a transcript deleted while held ------------------------------------------


@pytest.mark.asyncio
async def test_a_transcript_removed_after_the_hold_loses_nothing(tmp_path) -> None:
    path = tmp_path / "result.txt"
    path.write_text("full transcript")
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    text = "Z" * 5000
    assert inline_collection_owns(reg._manager, None, _info("a1", text, str(path)))
    path.unlink()  # deleted (manual delete, cleanup) while held
    reg.finish(PARENT, {"a1"}, set())
    await _drain()
    t = delivered[0]
    assert t.result == text or (
        t.result_path and (tmp_path / "result.txt").exists()
    ), "delivered part of the result plus a pointer to a file that no longer exists"


# --- error text ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_text_is_delivered_whole() -> None:
    delivered: list[Any] = []
    reg = _registry(delivered)
    assert reg.reserve(PARENT, "a1", 60)
    err = "E" * 2000
    assert inline_collection_owns(reg._manager, None, _info("a1", "r", error=err))
    reg.finish(PARENT, {"a1"}, set())
    await _drain()
    assert delivered[0].error == err


# --- HELD_BYTES_BUDGET accounting, seeded --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(60))
async def test_budget_is_discharged_on_every_path(
    seed: int, _floor_monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 20_000)
    rng = random.Random(seed)
    now = [0.0]
    delivered: list[Any] = []
    reg = InlineCollections(clock=lambda: now[0])
    mgr = _manager(delivered, busy=False)
    mgr.inline_collections = reg
    reg.bind(mgr)
    path = tmp_path / "r.txt"
    path.write_text("x")
    ids = [f"a{i}" for i in range(12)]
    for _ in range(80):
        aid = rng.choice(ids)
        op = rng.randrange(8)
        if op == 0:
            reg.reserve(PARENT, aid, rng.choice([0, 60]))
        elif op == 1:
            res = "q" * rng.choice([10, 4000, 9000])
            inline_collection_owns(mgr, None, _info(aid, res, rng.choice(["", str(path)])))
        elif op == 2:
            some = set(rng.sample(ids, 3))
            reg.finish(PARENT, some, {i for i in some if rng.random() < 0.5})
        elif op == 3:
            await asyncio.gather(*reg.commit(PARENT, [aid], rng.random() < 0.5, released=[aid]))
        elif op == 4:
            reg.retire(PARENT)
        elif op == 5:
            now[0] += rng.choice([1, 400, 4000, 8000])
            reg._expire(PARENT)
        elif op == 6:
            reg.consume_collected(PARENT, aid)
        else:
            reg.discard(PARENT, aid)
        await _drain()
        live = sum(r.held_bytes for recs in reg._records.values() for r in recs.values())
        assert reg._held_bytes == live >= 0
    now[0] += 1e6
    reg._expire(PARENT)
    await _drain()
    assert reg._held_bytes == 0


# --- over budget: what a release delivers -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "kept_end"),
    [("head", "start"), ("tail", "end"), ("both", "both")],
)
async def test_an_over_budget_cut_follows_completion_keep(
    mode: str, kept_end: str, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 400)
    delivered: list[Any] = []
    reg = _registry(delivered)
    reg._manager._completion_keep = mode
    reg._manager.get = lambda _aid: None  # the run record was evicted
    assert reg.reserve(PARENT, "a1", 60)
    text = "S" + "m" * 5000 + "E"
    assert inline_collection_owns(reg._manager, None, _info("a1", text))
    rec = reg._records[PARENT]["a1"]
    assert rec.result_cut_from == len(text)
    assert rec.held_bytes == reg._held_bytes <= 400
    kept = rec.completion.result
    assert kept.startswith("S") == (kept_end in ("start", "both"))
    assert kept.endswith("E") == (kept_end in ("end", "both"))
    reg.finish(PARENT, {"a1"}, set())
    await asyncio.wait_for(asyncio.gather(*reg._tasks), timeout=10)  # off-loop stat
    assert delivered[0].result.startswith(kept)
    assert delivered[0].result.endswith(f"[result was not retained ({len(text)} chars)]")
    assert reg._held_bytes == 0


@pytest.mark.asyncio
async def test_an_over_budget_release_reads_the_full_result_from_the_live_run(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 100)
    delivered: list[Any] = []
    reg = _registry(delivered)
    text = "R" * 5000 + "TAIL"
    live = _info("a1", text)
    reg._manager.get = lambda aid: live if aid == "a1" else None
    assert reg.reserve(PARENT, "a1", 60)
    assert inline_collection_owns(reg._manager, None, _info("a1", text))
    assert reg._held_bytes <= 100
    reg.finish(PARENT, {"a1"}, set())
    await _drain()
    assert delivered[0].result == text


@pytest.mark.asyncio
async def test_an_over_budget_release_with_no_live_run_points_at_its_transcript(
    tmp_path, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "result.txt"
    path.write_text("full transcript")
    # Room for the pointer, which is charged first, and none for the result.
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", len(str(path).encode("utf-8")))
    delivered: list[Any] = []
    reg = _registry(delivered)
    reg._manager.get = lambda _aid: None
    assert reg.reserve(PARENT, "a1", 60)
    info = _info("a1", "x" * 900, str(path), user_stopped=True)
    assert inline_collection_owns(reg._manager, None, info)
    assert reg._records[PARENT]["a1"].completion.result == ""
    reg.finish(PARENT, {"a1"}, set())
    await asyncio.wait_for(asyncio.gather(*reg._tasks), timeout=10)  # off-loop stat
    assert delivered[0].result == (
        f"[result was not retained in full (900 chars); full result: {path}]"
    )


@pytest.mark.asyncio
async def test_an_over_budget_commit_settles_without_redelivery(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A written response carried the whole result from the live run, so the
    held part is only settled, never delivered again."""
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 10)
    delivered: list[Any] = []
    reg = _registry(delivered, busy=True)
    assert reg.reserve(PARENT, "a1", 60)
    assert inline_collection_owns(reg._manager, None, _info("a1", "w" * 500))
    reg.finish(PARENT, {"a1"}, {"a1"})
    await asyncio.gather(*reg.commit(PARENT, ["a1"], True))
    assert delivered == []
    assert PARENT not in reg._records and reg._held_bytes == 0


def test_a_result_path_longer_than_the_text_cap_is_kept_whole() -> None:
    from kiro_crew.subagent_inline_collection import HELD_TEXT_MAX_CHARS, _held_snapshot

    path = "/r/" + "d" * (HELD_TEXT_MAX_CHARS * 2) + "/result.txt"
    assert _held_snapshot(_info("a1", "r", path)).result_path == path


def test_error_text_is_capped_at_its_own_bound() -> None:
    from kiro_crew.subagent_inline_collection import HELD_ERROR_MAX_CHARS, _held_snapshot

    assert HELD_ERROR_MAX_CHARS == 8192
    snap = _held_snapshot(_info("a1", "r", error="e" * 100_000))
    assert snap.error == "e" * (HELD_ERROR_MAX_CHARS - 1) + "\u2026"


# --- the cut is sized on bytes, and every kept field is charged ---------------


def test_an_ascii_result_under_pressure_keeps_what_fits() -> None:
    """Sized at four bytes per char, the cut would keep about a quarter of the room for ASCII."""
    reg = InlineCollections()
    assert reg.reserve(PARENT, "a1", 60) and reg.reserve(PARENT, "a2", 60)
    assert reg.hold(PARENT, "a1", _info("a1", "x" * (sic.HELD_BYTES_BUDGET // 2)))
    room = sic.HELD_BYTES_BUDGET - reg._held_bytes
    assert reg.hold(PARENT, "a2", _info("a2", "y" * sic.HELD_BYTES_BUDGET))
    kept = reg._records[PARENT]["a2"].held_bytes
    assert room - 3 <= kept <= room, f"kept {kept} of {room} bytes of room"


@pytest.mark.parametrize("mode", ["head", "tail", "both"])
@pytest.mark.parametrize("unit", ["a", "\u00e9", "\u65e5", "\U0001f47b"])
@pytest.mark.parametrize("budget", [1, 2, 3, 5, 40, 41, 1000])
def test_the_byte_cut_fills_the_room_to_within_one_character(
    mode: str, unit: str, budget: int
) -> None:
    text = "S" + unit * 2000 + "E"
    kept = sic._cut_to_bytes(text, budget, mode)
    assert budget - 3 <= len(kept.encode("utf-8")) <= budget
    if budget >= 40:
        assert kept.startswith("S") == (mode in ("head", "both"))
        assert kept.endswith("E") == (mode in ("tail", "both"))


def test_a_text_that_fits_is_returned_whole() -> None:
    assert sic._cut_to_bytes("\u65e5" * 10, 30, "both") == "\u65e5" * 10


def test_error_and_result_path_are_charged_at_the_member_cap() -> None:
    """At MAX_IDS_PER_PARENT members, each with a full-size ``error`` and a long
    ``result_path``, the held text passes the budget unless every kept field is
    charged. Each record's charge equals the bytes its snapshot keeps, the total
    stays inside the budget, and retiring discharges all of it."""
    reg = InlineCollections()
    cap = sic.MAX_IDS_PER_PARENT
    path = "/r/" + "d" * 300 + "/result.txt"
    err = "e" * (sic.HELD_ERROR_MAX_CHARS * 2)
    for i in range(cap):
        assert reg.reserve(PARENT, f"a{i}", 60)
        assert reg.hold(PARENT, f"a{i}", _info(f"a{i}", "r" * 10, path, error=err))
    assert len(reg._records[PARENT]) == cap
    for rec in reg._records[PARENT].values():
        snap = rec.completion
        kept = sum(
            len(getattr(snap, f).encode("utf-8")) for f in ("result", "error", "result_path")
        )
        assert rec.held_bytes == kept
    assert reg._held_bytes == sum(r.held_bytes for r in reg._records[PARENT].values())
    # Uncharged, 1000 x (8 KiB error + 314-byte path) would be 8.5 MB. The only
    # excess is the short failure note a member keeps once the budget is full.
    assert reg._held_bytes <= sic.HELD_BYTES_BUDGET + cap * sic._HELD_ERROR_NOTE_MAX_BYTES
    # Every member still reads as failed: none is held as completed.
    assert all(r.completion.outcome == "failed" for r in reg._records[PARENT].values())
    first = reg._records[PARENT]["a0"].completion
    assert first.error == err[: sic.HELD_ERROR_MAX_CHARS - 1] + "\u2026"
    assert first.result_path == path
    # The last member found the budget full: its pointer is whole or absent, never cut.
    assert reg._records[PARENT][f"a{cap - 1}"].completion.result_path in ("", path)
    reg.retire(PARENT)
    assert reg._held_bytes == 0


@pytest.mark.asyncio
async def test_a_failed_member_held_on_a_full_budget_is_released_as_failed(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One result cut to all the remaining room leaves none, so a later failed
    member's ``error`` would be cut to nothing and the run would read as
    completed. It keeps the fixed note instead, and is delivered as failed."""
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 1000)
    delivered: list[Any] = []
    reg = _registry(delivered)
    reg._manager.get = lambda _aid: None
    assert reg.reserve(PARENT, "a1", 60) and reg.reserve(PARENT, "a2", 60)
    assert inline_collection_owns(reg._manager, None, _info("a1", "x" * 5000))
    assert reg._held_bytes == sic.HELD_BYTES_BUDGET  # the cut filled the room
    err = "Traceback: boom " * 40
    assert inline_collection_owns(reg._manager, None, _info("a2", "", error=err))
    held = reg._records[PARENT]["a2"].completion
    assert held.error == f"[error text not retained ({len(err)} chars)]"
    assert held.outcome == "failed"
    reg.finish(PARENT, {"a1", "a2"}, set())
    await asyncio.wait_for(asyncio.gather(*reg._tasks), timeout=10)
    (failed,) = [t for t in delivered if t.id == "a2"]
    assert failed.outcome == "failed"
    assert reg._held_bytes == 0


def test_a_memory_wait_expiry_held_on_a_full_budget_still_owes_its_report(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memory-wait expiry fails with an ``error`` and owes the store's report.
    Read as completed, its commit would settle a delivered mark instead, and
    the owed report would stay owed for restart replay."""
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", 100)
    reg = InlineCollections()
    assert reg.reserve(PARENT, "a1", 60) and reg.reserve(PARENT, "a2", 60)
    assert reg.hold(PARENT, "a1", _info("a1", "x" * 500))
    expiry = _info("a2", "", error="memory wait expired " * 20)
    expiry._report_owed = True
    assert reg.hold(PARENT, "a2", expiry)
    (delivery,) = sic.settle_deliveries([reg._records[PARENT]["a2"].completion])
    assert delivery.report_owed is True


@pytest.mark.parametrize("seed", range(300))
def test_a_held_completion_keeps_its_live_outcome(
    seed: int, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over random budgets, fills and run shapes, the held copy's outcome is the
    live run's: only content degrades under budget pressure, never the outcome."""
    rng = random.Random(seed)
    _floor_monkeypatch.setattr(sic, "HELD_BYTES_BUDGET", rng.choice([0, 1, 40, 200, 5000]))
    reg = InlineCollections()
    for i in range(6):
        aid = f"a{i}"
        assert reg.reserve(PARENT, aid, 60)
        live = _info(
            aid,
            "r" * rng.choice([0, 10, 3000]),
            rng.choice(["", "/r/" + "d" * rng.choice([5, 300])]),
            error=rng.choice(["", "e", "E" * rng.choice([30, 9000])]),
            user_stopped=rng.random() < 0.2,
        )
        assert reg.hold(PARENT, aid, live)
        assert reg._records[PARENT][aid].completion.outcome == live.outcome


@pytest.mark.parametrize("budget", [0, 5, 40, 41, 100, 10_000])
def test_an_error_that_does_not_fit_is_replaced_never_emptied(budget: int) -> None:
    err = "\u65e5" * 3000
    kept = sic._cut_error_to_bytes(err, budget)
    if len(err.encode("utf-8")) <= budget:
        assert kept == err
    else:
        assert kept == f"[error text not retained ({len(err)} chars)]"
        assert len(kept.encode("utf-8")) <= sic._HELD_ERROR_NOTE_MAX_BYTES
