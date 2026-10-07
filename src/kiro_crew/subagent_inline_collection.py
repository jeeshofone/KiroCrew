"""Which sub-agent results a blocking ``spawn_sub_agents`` call returns inline.

``spawn_sub_agents`` spawns its members, polls them, and hands their results back
as the tool call's own return value. Each member's ``[Subagent completion event]``
must therefore never reach the parent: the parent's turn is still blocked inside
that tool call, and an injected prompt either cancels the turn (the prompt-busy
retry interrupts it) or plays later as a redundant turn about a result the model
already read.

This registry is the ONE owner of that delivery, per PARENT session key, so every
parent kind (dashboard tab, cron run, channel thread, task runner) is held to the
same rule. Each member is a record whose state moves one way::

    collecting --hold--> held --close, not returned / expiry--> delivering
         |                 |                                       |
         \\--close, returned--+--> claimed --response dropped / expiry--/
                                    |
                                    \\--response written--> settled (completion seen)
                                                          \\-> returned --completion--> consumed

* **collecting**: ``reserve`` records the member when ``/api/spawn`` mints its run
  id, before the run can start, so a member that finishes at once, or one that
  waits behind the concurrency cap, is covered.
* **held**: the member completed while collecting. Its completion is kept here,
  undelivered, and never injected.
* **claimed**: the call's close (``finish``) named the member in its result, but
  that result has not yet reached the parent: the dispatcher writes the tool's
  response only after the call returns, and a cancel can still drop it. Nothing
  is settled. ``commit`` settles the claim once the response is written, and
  releases it for ordinary delivery when the response was dropped or never
  reported (expiry). So an unsettled result is always one that can still be
  delivered. A written commit settles a collecting or held member it names
  too: its claim was lost or has not landed yet, and a late claim then
  changes nothing, so a result the response carried is never redelivered.
* **returned**: the response carried the member before its completion was seen
  (the poll can read ``done`` before the completion callback runs). Its
  completion is consumed, never injected.
* **delivering**: a released held or claimed completion. ``_deliver`` is the one
  function that takes it to a terminal outcome, whichever path released it.

Every record counts against the parent's one capacity until it leaves the
registry, whatever its state, so admission never lets a member in that a later
state could not keep.

The retired-parent fence lives here too. A parent's teardown calls ``retire``
synchronously, which marks every record of that parent, so a fenced result is
recognised even after completed-run retention evicted its run record. ``_deliver``
reads the mark at the point of delivery, after every wait.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Added to the call's ``max_wait`` before a collection is treated as abandoned.
#: Covers ``_hold_for_parent_resume`` (bounded by the same deadline) and the
#: final result reads that follow the wait.
COLLECTION_GRACE_SECS = 300.0
#: Upper bound on one collection's lifetime, whatever ``max_wait`` claims:
#: the tool clamps its own wait to two hours.
MAX_COLLECTION_TTL_SECS = 7200.0 + COLLECTION_GRACE_SECS
#: Per-parent cap on the ids each store keeps. The ONE bound for this
#: population: the dashboard slot's collected set uses it too, and a member that
#: would exceed it is refused at spawn rather than silently left unheld.
MAX_IDS_PER_PARENT = 1000
#: How long a returned id waits for its completion before it is forgotten. A
#: completion normally follows within seconds of the run being read as done.
COLLECTED_TTL_SECS = 3600.0
#: How long a claim waits for the dispatcher's word on the call's response
#: before it is released for ordinary delivery. The tool reports it right
#: after the response is written; a lost report costs a duplicate, never a
#: lost result.
CLAIM_TTL_SECS = 300.0
#: Added to the injection timeout to bound how long a released result waits
#: for the run's own terminal report to return before it is delivered anyway.
#: The report's longest legitimate wait is a held cron parent's idle reset,
#: which the injection timeout bounds.
TERMINAL_REPORT_GRACE_SECS = 60.0
#: How often a released result re-checks that its parent's turn ended.
_ORPHAN_IDLE_POLL_SECS = 0.5
#: The most text a held completion keeps in any string field but ``result``,
#: ``error`` and ``result_path``, the ellipsis included. Task and tool-title
#: text have no cap of their own, and a held record outlives the run record's
#: own eviction, so the registry bounds what it retains itself.
HELD_TEXT_MAX_CHARS = 512
#: The most ``error`` text a held completion keeps, the ellipsis included. The
#: route prints it whole (``Error: ...``), so it gets room for a real
#: traceback tail rather than the generic cap.
HELD_ERROR_MAX_CHARS = 8192
#: The most a failed member's ``error`` keeps once the budget has no room: the
#: note ``_cut_error_to_bytes`` writes, for an error up to HELD_ERROR_MAX_CHARS.
_HELD_ERROR_NOTE_MAX_BYTES = len(f"[error text not retained ({HELD_ERROR_MAX_CHARS} chars)]")
#: Text fields kept whole: the route points at ``result_path`` as written.
_HELD_UNCAPPED_TEXT = frozenset({"result", "result_path"})
#: Every held completion's ``result``, ``error`` and ``result_path``, across
#: every parent, counted in UTF-8 bytes. The one excess: a failed member keeps
#: at least a ``_HELD_ERROR_NOTE_MAX_BYTES`` note in ``error`` so it never reads
#: as completed. A result is kept whole while it fits. One that does not is still held (the
#: parent's turn is blocked inside the call, so routing it anywhere else would
#: inject into that turn): it keeps what fits, cut the way ``completion_keep``
#: cuts, and its release reads the full result back from the live run, or says
#: in the result itself how much was not retained.
HELD_BYTES_BUDGET = 8 * 1024 * 1024
#: The only non-scalar run fields a held copy keeps, each read by the completion
#: route and bounded: one batch's deliveries. Every other field that holds an
#: object (the follow-up queue ``spawn_steer`` appends to without a cap, the
#: in-flight tool tracker, futures) is reset on the copy to its dataclass
#: default, so the live run's own value is untouched.
_HELD_KEPT_OBJECTS = frozenset({"_digest_settle_deliveries"})
#: Text buffers the run fills while live and the route does not read.
_HELD_DROPPED_TEXT = ("streaming_text",)
_SCALARS = (str, int, float, bool, type(None))

COLLECTING = "collecting"
HELD = "held"
CLAIMED = "claimed"
RETURNED = "returned"
DELIVERING = "delivering"
#: Terminal outcomes of ``_deliver``. The record leaves the registry with one.
DELIVERED = "delivered"
UNDELIVERED = "undelivered"
#: The parent's route parked the announce on its slot queue, whose drain
#: settles it, so delivering it is the queue's job, not this registry's.
HANDED_OFF = "handed_off"


@dataclass(eq=False)
class _Record:
    aid: str
    parent: str
    state: str
    deadline: float
    completion: Any = None
    retired: bool = False
    #: The fence has dropped this result (once, whichever path saw it first).
    fenced: bool = False
    #: The delivery of an unreturned result, cancelled when its parent retires.
    task: asyncio.Task[str] | None = None
    #: What this record's result text charges to ``HELD_BYTES_BUDGET``.
    held_bytes: int = 0
    #: The result's length in chars when the budget made ``hold`` keep only
    #: part of it, else 0. A release then restores it (``_restored_result``).
    result_cut_from: int = 0


class InlineCollections:
    """Per-parent owner of the members a blocking call collects inline."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # parent -> {agent id: record}
        self._records: dict[str, dict[str, _Record]] = {}
        # Strong references to the deliveries in flight.
        self._tasks: set[asyncio.Task[str]] = set()
        # Bytes of held result text, against ``HELD_BYTES_BUDGET``.
        self._held_bytes = 0
        # The manager whose route, sessions, terminal reports and store a
        # delivery uses. Bound by ``SubagentManager`` at construction.
        self._manager: Any = None

    def bind(self, manager: Any) -> None:
        self._manager = manager

    # ── spawn side ──

    def reserve(self, parent: str, aid: str, max_wait: float) -> bool:
        """Record that a live call collects *aid* for *parent*; False when full."""
        if not parent or not aid:
            return False
        self._expire(parent)
        records = self._records.setdefault(parent, {})
        ttl = min(max(float(max_wait), 0.0) + COLLECTION_GRACE_SECS, MAX_COLLECTION_TTL_SECS)
        existing = records.get(aid)
        if existing is not None:
            if existing.state == COLLECTING and not existing.retired:
                existing.deadline = self._clock() + ttl
                return True
            return False
        # Every retained record holds its place, whatever its state: a member
        # admitted here must still fit when its reservation becomes a claim.
        if len(records) >= MAX_IDS_PER_PARENT:
            self._prune(parent)
            return False
        records[aid] = _Record(aid, parent, COLLECTING, self._clock() + ttl)
        self._schedule_expiry(parent, ttl)
        return True

    def finish(self, parent: str, released: Iterable[str], returned: Iterable[str]) -> None:
        """End collection of *released*; *returned* is named in the call's result.

        Synchronous. A member the result names is CLAIMED, in place, so it keeps
        the capacity its reservation took: nothing is settled until ``commit``
        hears the response reached the parent. A held member the result does not
        name is delivered as an ordinary completion in the background, since
        that waits for the parent's turn, which is blocked on the caller.
        """
        returned_set = set(returned)
        records = self._records.get(parent, {})
        for aid in set(released) | returned_set:
            rec = records.get(aid)
            if rec is None or rec.state not in (COLLECTING, HELD):
                continue
            if aid in returned_set:
                self._claim(rec)
            else:
                self._end_collection(rec)
        self._prune(parent)

    def commit(
        self,
        parent: str,
        ids: Iterable[str],
        delivered: bool,
        *,
        released: Iterable[str] = (),
    ) -> list[asyncio.Task[str]]:
        """Settle or release *ids*: the call's response was written or dropped.

        Synchronous up to the returned tasks, which settle the results whose
        completion is in hand; the caller awaits them. A written response is
        the stronger fact, so it settles *ids* whether or not their claim
        landed first: a claim that is late, or lost, then finds them settled
        and changes nothing. A result whose completion has not arrived becomes
        ``returned`` (written), so that completion is consumed, or leaves the
        registry (dropped), so it takes the ordinary route. A dropped result
        whose completion is in hand is delivered as an ordinary completion.
        *released* ends the collection of the call's other members, as the
        claim would have.
        """
        records = self._records.get(parent, {})
        named = set(ids)
        settles: list[asyncio.Task[str]] = []
        for aid in set(released) - named:
            rec = records.get(aid)
            if rec is not None and rec.state in (COLLECTING, HELD):
                self._end_collection(rec)
        for aid in named:
            rec = records.get(aid)
            if rec is None or rec.state not in (COLLECTING, HELD, CLAIMED):
                continue
            if rec.retired:
                # The parent ended before the response was confirmed: no live
                # turn read it, so nothing is settled; restart recovery keeps it.
                # One whose completion has not arrived stays until its deadline,
                # so ``hold`` still fences that completion.
                if rec.completion is not None:
                    self._parent_retired(rec)
                    self._remove(rec)
            elif not delivered and rec.state != CLAIMED:
                self._end_collection(rec)  # its claim never landed: as a close
            elif rec.completion is None:
                if delivered:
                    rec.state = RETURNED
                    rec.deadline = self._clock() + COLLECTED_TTL_SECS
                else:
                    self._remove(rec)
            else:
                task = self._release(rec, returned=delivered)
                if task is not None and delivered:
                    settles.append(task)
        self._prune(parent)
        return settles

    def _end_collection(self, rec: _Record) -> None:
        """The call is over without returning *rec*: forget it, or deliver what it held."""
        if rec.state == COLLECTING:
            self._remove(rec)
        else:
            self._release(rec, returned=False)

    def discard(self, parent: str, aid: str) -> None:
        """Forget *aid*: its spawn was refused, so nothing ran under the id."""
        rec = self._records.get(parent, {}).get(aid)
        if rec is not None and rec.state in (COLLECTING, HELD):
            self._remove(rec)

    def record_collected(self, parent: str, ids: Iterable[str]) -> int:
        """Claim ids a call returned that no reservation covers; the count dropped.

        A member spawned without a reservation (an older tool) is claimed the
        same way, within the same capacity. One the capacity cannot keep is
        counted and named once in the log, and its completion is delivered as
        an ordinary one.
        """
        now = self._clock()
        for other in list(self._records):
            self._drop_expired_returns(other, now)
        records = self._records.setdefault(parent, {})
        dropped = 0
        for aid in ids:
            if aid in records:
                continue  # owned already
            if len(records) >= MAX_IDS_PER_PARENT:
                dropped += 1
                continue
            rec = _Record(aid, parent, COLLECTING, now)
            records[aid] = rec
            self._claim(rec)
        if dropped:
            logger.warning(
                "inline-collected ids for %s at their cap of %d; %d not recorded, "
                "so their completions are delivered as ordinary ones",
                parent,
                MAX_IDS_PER_PARENT,
                dropped,
            )
        self._prune(parent)
        return dropped

    # ── gateway side ──

    def hold(self, parent: str, aid: str, completion: Any) -> bool:
        """Keep *completion* undelivered if a live call collects *aid*."""
        self._expire(parent)
        rec = self._records.get(parent, {}).get(aid)
        if rec is None or rec.state not in (COLLECTING, CLAIMED) or rec.completion is not None:
            return False
        snap = _held_snapshot(completion)
        if snap is None:
            # Its fields cannot be enumerated, so a bounded copy cannot be
            # built: delivered as an ordinary completion instead of retained.
            logger.warning(
                "Subagent %s: completion is a %s, not a run record; not holding it",
                aid,
                type(completion).__name__,
            )
            return False
        # Everything the snapshot keeps whole or near-whole is charged, so the
        # budget bounds all held text. ``error`` is charged first and never
        # cut: the outcome is derived from it being non-empty, and only content
        # may degrade, never the outcome. Then the pointer, which is what makes
        # a cut result recoverable. Then ``result``, which absorbs the pressure.
        room = max(0, HELD_BYTES_BUDGET - self._held_bytes)
        charge = 0
        for name in ("error", "result_path"):
            value = getattr(snap, name, "")
            if not isinstance(value, str) or not value:
                continue
            if name == "error":
                kept_field = _cut_error_to_bytes(value, room)
            else:
                # A partial path points nowhere, so it is kept whole or not at all.
                kept_field = value if len(value.encode("utf-8")) <= room else ""
            if kept_field != value:
                logger.warning(
                    "Subagent %s: held results take %d of %d bytes, so its %s is held as %d of %d bytes",
                    aid,
                    self._held_bytes,
                    HELD_BYTES_BUDGET,
                    name,
                    len(kept_field.encode("utf-8")),
                    len(value.encode("utf-8")),
                )
                setattr(snap, name, kept_field)
            used = len(kept_field.encode("utf-8"))
            charge += used
            room = max(0, room - used)
        result = getattr(completion, "result", "")
        result = result if isinstance(result, str) else ""
        result_bytes = len(result.encode("utf-8"))
        if result_bytes > room:
            # Still held: the parent's turn is blocked inside the call, so the
            # ordinary route would inject into it. Only the content degrades.
            kept = _cut_to_bytes(result, room, self._completion_keep_mode())
            logger.warning(
                "Subagent %s: held results already take %d of %d bytes, so its "
                "%d-byte result is held as %d bytes; a release restores it from "
                "the live run or says how much was not retained",
                aid,
                self._held_bytes + charge,
                HELD_BYTES_BUDGET,
                result_bytes,
                len(kept.encode("utf-8")),
            )
            rec.result_cut_from = len(result)
            result = kept
            result_bytes = len(kept.encode("utf-8"))
        charge += result_bytes
        snap.result = result
        rec.completion = snap
        rec.held_bytes = charge
        self._held_bytes += charge
        if rec.retired:
            # Its parent ended while it ran: owned, and dropped by the fence.
            self._parent_retired(rec)
            self._remove(rec)
            return True
        if rec.state == COLLECTING:
            rec.state = HELD
        return True

    def has_collected(self, parent: str) -> bool:
        """True while any claimed or returned id for *parent* still awaits its completion."""
        now = self._clock()
        return any(
            r.state in (CLAIMED, RETURNED) and r.completion is None and r.deadline > now
            for r in self._records.get(parent, {}).values()
        )

    def consume_collected(self, parent: str, aid: str) -> bool:
        """True (and forget it) when a call already returned *aid* inline."""
        rec = self._records.get(parent, {}).get(aid)
        if rec is None or rec.state != RETURNED:
            return False
        self._remove(rec)
        return rec.deadline > self._clock()

    # ── teardown ──

    def retire(self, parent: str) -> None:
        """Fence every record of *parent*: its conversation has ended.

        Synchronous, so it is in place before the teardown's first await. A held
        completion is dropped now. The delivery of one the call did not return is
        cancelled now, wherever it is suspended, including inside the parent's
        route after the fence was read, so it can never resume into the
        conversation's allocation or injection. A collecting or claimed member's
        late completion is dropped by ``hold``, and a claim's ``commit`` settles
        nothing and leaves the record in place until its deadline, so that
        completion is still fenced. A result whose response was written already reached the parent,
        so its settle is left to finish.
        """
        records = self._records.get(parent)
        if not records:
            return
        for rec in list(records.values()):
            rec.retired = True
            held = rec.state in (HELD, CLAIMED) and rec.completion is not None
            if held or rec.task is not None:
                # Dropped here, not by the cancelled task: a task cancelled
                # before its first step never runs its body.
                self._parent_retired(rec)
                self._remove(rec)
            if rec.task is not None:
                rec.task.cancel()
                rec.task = None

    # ── the one delivery ──

    def _claim(self, rec: _Record) -> None:
        rec.state = CLAIMED
        rec.deadline = self._clock() + CLAIM_TTL_SECS
        self._schedule_expiry(rec.parent, CLAIM_TTL_SECS)

    def _release(self, rec: _Record, *, returned: bool) -> asyncio.Task[str] | None:
        if rec.retired:
            self._parent_retired(rec)
            self._remove(rec)
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("inline result %s released with no running loop; kept held", rec.aid)
            return None
        rec.state = DELIVERING
        task = loop.create_task(self._deliver(rec, returned=returned))
        if not returned:
            rec.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _deliver(self, rec: _Record, *, returned: bool) -> str:
        """Take a released held completion to its terminal outcome.

        The ONE delivery for every path that releases a held completion: the
        call's close, the response's commit (written or dropped) and expiry. A
        result whose response was written is settled at once, even if its parent
        is retired before the settle runs: the response already reached it. One it did not
        return waits for the run's own terminal report (the hold was taken inside
        it) and for the parent's turn to end, then takes the ordinary route on a separate
        ticket, so the original record's ``_delivery_queued`` is never touched
        and its report never writes a mark this delivery owns. The fence is read
        after every wait, at the point of delivery, and a teardown that lands
        later, inside the route, cancels the delivery (``retire``).
        """
        outcome = UNDELIVERED
        try:
            outcome = await self._deliver_once(rec, returned=returned)
        except asyncio.CancelledError:
            if not rec.retired:
                raise  # shutdown: the folder stays for restart recovery
            # ``retire`` cancelled it, and has already dropped it.
        except Exception:
            logger.exception("Subagent %s: delivering a held inline result failed", rec.aid)
        finally:
            rec.task = None
            self._remove(rec)
        return outcome

    async def _deliver_once(self, rec: _Record, *, returned: bool) -> str:
        info = rec.completion
        mgr = self._manager
        if returned:
            # The written response carried it to the parent before any later
            # teardown, so it is delivered whatever happens to the parent now.
            await self._settle([info])
            return DELIVERED
        await self._await_terminal_report(info)
        await self._await_parent_idle(rec.parent)
        ticket = _ticket(info)
        if rec.result_cut_from:
            ticket.result = await self._restored_result(rec, ticket)
        if self._parent_retired(rec):
            return UNDELIVERED
        on_done = mgr._on_done if mgr is not None else None
        if on_done is None:
            return UNDELIVERED  # restart recovery delivers the folder
        logger.info(
            "Subagent %s: spawn_sub_agents ended without returning it; delivering it to %s",
            rec.aid,
            rec.parent,
        )
        try:
            await on_done(ticket)
        except Exception:
            logger.exception("Subagent %s: delivering a released inline result failed", rec.aid)
            return UNDELIVERED
        if rec.retired:
            # Retired inside the route, which swallowed the cancel: ``retire``
            # already dropped it, and what the route did reached no live parent.
            return UNDELIVERED
        # The route has returned: what it did reached the parent before any
        # teardown, so a later ``retire`` must not cancel the settle below.
        rec.task = None
        if ticket._report_undelivered:
            # The route gave up (e.g. the parent's allocation failed): the
            # folder stays un-tombstoned and an owed report owed, for restart.
            return UNDELIVERED
        if ticket._delivery_queued:
            return HANDED_OFF
        await self._settle([ticket])
        return DELIVERED

    def _completion_keep_mode(self) -> str:
        mode = getattr(self._manager, "_completion_keep", "head")
        return mode if mode in ("head", "tail", "both") else "head"

    async def _restored_result(self, rec: _Record, ticket: Any) -> str:
        """The released result of a record the budget held only in part.

        The live run's own result when the manager still has it (the full
        completion copy). Otherwise what was kept, followed by a note that
        says how much was not, and where the full text is when its transcript
        is on disk, in ``result`` itself, so every branch of the route shows it.
        """
        get = getattr(self._manager, "get", None)
        live = None
        if callable(get):
            try:
                live = get(rec.aid)
            except Exception:
                logger.debug("Could not read the live run %s", rec.aid, exc_info=True)
        live_result = getattr(live, "result", None)
        if isinstance(live_result, str) and live_result:
            return live_result
        kept = ticket.result if isinstance(ticket.result, str) else ""
        path = getattr(ticket, "result_path", "")
        if await asyncio.to_thread(_transcript_on_disk, path):
            note = (
                f"[result was not retained in full ({rec.result_cut_from} chars); "
                f"full result: {path}]"
            )
        else:
            note = f"[result was not retained ({rec.result_cut_from} chars)]"
        return f"{kept}\n\n{note}" if kept else note

    def _parent_retired(self, rec: _Record) -> bool:
        """True, with the result's delivery dropped, when its parent was retired.

        The terminal report's own statement: no injection, which would recreate
        the retired conversation or reach its replacement, and no delivered mark,
        so restart reconciliation still finds the folder. An owed memory-wait
        report is owed only to the parent that ended, so it is cleared.
        """
        if not rec.retired:
            return False
        if rec.fenced:
            return True
        rec.fenced = True
        info = rec.completion
        if info is not None and getattr(info, "_report_owed", False) is True:
            try:
                self._manager._admission.taskq_clear_owed_reports([rec.aid])
            except Exception:
                logger.debug("Could not clear the owed report of %s", rec.aid, exc_info=True)
        logger.info("Subagent %s: held inline result dropped -- its parent ended", rec.aid)
        return True

    async def _settle(self, completions: list[Any]) -> None:
        deliveries = settle_deliveries(completions)
        if not deliveries or self._manager is None:
            return
        try:
            await self._manager.settle_queued_delivery(deliveries)
        except Exception:
            logger.debug("Could not settle held inline deliveries", exc_info=True)

    async def _await_terminal_report(self, info: Any) -> None:
        # The hold was taken inside the run's terminal report, which may still be
        # finishing (a held cron parent's idle reset awaits). Take over only once
        # it has returned, so the two never decide the same delivery.
        mgr = self._manager
        if mgr is None:
            return
        from kiro_crew.subagent import INJECTION_TIMEOUT

        # The record keeps a snapshot, so the report is found by the run's id.
        aid = getattr(info, "id", None)
        pending = [
            task for task, owner in mgr._report_owners.items() if getattr(owner, "id", None) == aid
        ]
        if not pending:
            return
        bound = INJECTION_TIMEOUT + TERMINAL_REPORT_GRACE_SECS
        try:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(t) for t in pending), return_exceptions=True),
                bound,
            )
        except asyncio.TimeoutError:
            # A hung report must not keep the result, and its capacity, here
            # for good. The hold parked the report's own delivery, so it never
            # decides this one: the result goes out on its separate ticket.
            logger.warning(
                "Subagent %s: its terminal report is still running after %.0fs; "
                "delivering the released result without waiting further",
                getattr(info, "id", "?"),
                bound,
            )

    async def _await_parent_idle(self, parent: str) -> None:
        # A cancelled call closes its collection while the turn that ran it is
        # still ending: wait (bounded) for that turn to let go, so the result
        # plays as a new turn after it, never as a prompt inside it. The one
        # piece of this registry that is a busy-parent wait: once the completion
        # route fences a busy parent itself, the route owns that wait and this
        # method is deleted (docs/system-specs/modules/subagent.md names it).
        from kiro_crew.subagent import INJECTION_TIMEOUT

        sessions = getattr(self._manager, "_sessions", None)
        if sessions is None:
            return
        deadline = time.monotonic() + INJECTION_TIMEOUT
        while sessions.is_busy(parent) and time.monotonic() < deadline:
            await asyncio.sleep(_ORPHAN_IDLE_POLL_SECS)

    # ── internals ──

    def _expire(self, parent: str) -> None:
        records = self._records.get(parent)
        if not records:
            return
        now = self._clock()
        expired = [
            r
            for r in records.values()
            if r.state in (COLLECTING, HELD, CLAIMED) and r.deadline <= now
        ]
        if expired:
            held = [r for r in expired if r.completion is not None and not r.retired]
            if held:
                logger.warning(
                    "spawn_sub_agents collection for %s never reported back; "
                    "delivering %d held result(s) as ordinary completions",
                    parent,
                    len(held),
                )
            for rec in expired:
                # A claim nobody confirmed is released like a dropped response:
                # a duplicate turn is recoverable, a lost result is not.
                if rec.completion is not None:
                    self._release(rec, returned=False)
                else:
                    self._discharge(rec)
                    del records[rec.aid]
        self._drop_expired_returns(parent, now)
        self._prune(parent)

    def _drop_expired_returns(self, parent: str, now: float) -> None:
        records = self._records.get(parent, {})
        for aid in [a for a, r in records.items() if r.state == RETURNED and r.deadline <= now]:
            self._discharge(records[aid])
            del records[aid]
        self._prune(parent)

    def _schedule_expiry(self, parent: str, ttl: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): the next reserve/hold call expires it
        loop.call_later(ttl + 1.0, self._expire, parent)

    def _discharge(self, rec: _Record) -> None:
        """Return what *rec*'s held completion charged to ``HELD_BYTES_BUDGET``."""
        self._held_bytes -= rec.held_bytes
        rec.held_bytes = 0

    def _remove(self, rec: _Record) -> None:
        self._discharge(rec)
        records = self._records.get(rec.parent)
        if records is not None and records.get(rec.aid) is rec:
            del records[rec.aid]
        self._prune(rec.parent)

    def _prune(self, parent: str) -> None:
        if parent in self._records and not self._records[parent]:
            del self._records[parent]


def _held_snapshot(info: Any) -> Any | None:
    """What a held record keeps of *info*, bar ``result``: a copy whose every
    other field is bounded, or None when *info* is not a run record whose
    fields can be enumerated.

    The registry never holds the run object itself, which the manager evicts on
    its own schedule. Every object-valued field except ``_HELD_KEPT_OBJECTS`` is
    reset to its default, the live text buffers are emptied, ``error`` is cut
    to ``HELD_ERROR_MAX_CHARS``, and every other string field but ``result``
    and ``result_path`` is cut to ``HELD_TEXT_MAX_CHARS``, each ending on an
    ellipsis. ``result`` is left for ``InlineCollections.hold``, which charges
    it, ``error`` and ``result_path`` to ``HELD_BYTES_BUDGET``.
    """
    if not dataclasses.is_dataclass(info) or isinstance(info, type):
        return None
    snap: Any = copy.copy(info)
    for f in dataclasses.fields(snap):
        if f.name in _HELD_KEPT_OBJECTS or f.name in _HELD_UNCAPPED_TEXT:
            continue
        value = getattr(snap, f.name, None)
        if isinstance(value, str):
            cap = HELD_ERROR_MAX_CHARS if f.name == "error" else HELD_TEXT_MAX_CHARS
            if f.name in _HELD_DROPPED_TEXT:
                setattr(snap, f.name, "")
            elif len(value) > cap:
                setattr(snap, f.name, value[: cap - 1] + "\u2026")
            continue
        if isinstance(value, _SCALARS):
            continue
        if f.default_factory is not dataclasses.MISSING:
            setattr(snap, f.name, f.default_factory())
        elif f.default is not dataclasses.MISSING:
            setattr(snap, f.name, f.default)
    return snap


def _cut_to_bytes(text: str, budget: int, mode: str) -> str:
    """*text* cut to at most *budget* UTF-8 bytes, keeping what ``mode`` keeps.

    The head, tail or both cut ``apply_completion_keep`` makes, sized on the
    encoded bytes: a cut that lands inside a character drops only that
    character's bytes (``errors="ignore"``), so the result is at most *budget*
    and at least *budget* - 3 bytes whatever the script.
    """
    from kiro_crew.context_management import _COMPLETION_BOTH_MARKER

    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    if budget <= 0:
        return ""
    if mode == "tail":
        return raw[-budget:].decode("utf-8", "ignore")
    marker = _COMPLETION_BOTH_MARKER.encode("utf-8")
    if mode == "both" and budget > len(marker) + 2:
        head = raw[: (budget - len(marker)) // 2].decode("utf-8", "ignore")
        # The tail takes whatever the head's own cut left over.
        tail_room = budget - len(marker) - len(head.encode("utf-8"))
        return head + _COMPLETION_BOTH_MARKER + raw[-tail_room:].decode("utf-8", "ignore")
    return raw[:budget].decode("utf-8", "ignore")


def _cut_error_to_bytes(error: str, budget: int) -> str:
    """*error* whole when it fits *budget* UTF-8 bytes, otherwise a fixed note.

    Never empty: a run with an empty ``error`` reads as ``completed``
    (``SubagentInfo.outcome``), which would announce a failure as a success and
    settle a memory-wait expiry's owed report as a delivered mark. So ``error``
    is never cut. It is kept whole, or replaced by
    ``[error text not retained (N chars)]``, the one text a held record keeps
    past a full budget, at most ``_HELD_ERROR_NOTE_MAX_BYTES``.
    """
    if len(error.encode("utf-8")) <= budget:
        return error
    return f"[error text not retained ({len(error)} chars)]"


def _transcript_on_disk(path: Any) -> bool:
    """Whether *path* names a readable ``result.txt`` a released result can point at.

    One ``stat`` and one access check, run off the loop at release, so the
    note never points at a file that is already gone.
    """
    if not isinstance(path, str) or not path:
        return False
    try:
        return os.path.isfile(path) and os.access(path, os.R_OK)
    except (OSError, ValueError):
        return False


def _ticket(info: Any) -> Any:
    """A separate record for one redelivery of *info*'s completion.

    *info* is the held snapshot, already bounded, never the live run. The route
    writes its routing flags on what it is handed. Those decide this delivery's
    outcome, so they are written on a copy that lives for that one delivery:
    the held record's flags stay what its own terminal report left them.
    """
    ticket = copy.copy(info)
    ticket._delivery_queued = False
    ticket._report_undelivered = False
    return ticket


def settle_deliveries(completions: Iterable[Any]) -> list[Any]:
    """The deliveries to settle for held *completions* that have now reached the parent.

    A completed run settles its ``delivered`` mark. A memory-wait expiry has no
    folder: what it owes is the store's report, which was not cleared when its
    report task returned because the hold had parked it (``_delivery_queued``),
    so it is settled as an owed report instead.
    """
    from kiro_crew.subagent import SubagentDelivery

    out: list[Any] = []
    for info in completions:
        if info.outcome == "completed":
            out.append(SubagentDelivery(info.id, info.elapsed, info.credits))
        elif getattr(info, "_report_owed", False) is True:
            out.append(SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True))
    return out
