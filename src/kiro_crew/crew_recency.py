"""When the user last chatted with each crew -- the Crewmates list's source.

The Crewmates page lists a crew only once the user has actually talked to it,
newest first, and reopens the most recent one after a restart. Neither question
can be answered from the transcripts or the crew log: both also move when a
crew works in the background (a cron, a wake, a patrol, a sub-agent, a
conductor-dispatched worker, an app's own session). So the one place that
knows a PERSON typed a message -- ``POST /api/chat`` without an app token,
cron attestation -- records it here, keyed by crew name.

One small JSON file under the data home, ``{"version": 1, "crews": {name: ts}}``.
Durable across restarts by construction. Best-effort throughout: a read fault
reads as "nothing recorded", and a write fault never fails the send.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

FILE_NAME = "crew_recency.json"

#: A send within this many seconds of the recorded one is not re-written: the
#: list orders by minutes, and a burst of sends must not fsync per message.
_WRITE_GRANULARITY_S = 30.0

_lock = threading.Lock()


def recency_path() -> Path:
    return data_home() / FILE_NAME


class _Unreadable(Exception):
    """The file exists but cannot be read as a record. Unreadable is not
    absent: every writer refuses rather than replace what it could not read."""


def _read_unlocked() -> tuple[dict[str, float], bool]:
    """The recorded crews and whether the one-time seed has run.

    Raises :class:`_Unreadable` for a file that is there but is not a record.
    """
    try:
        raw = read_bytes_with_retry(recency_path()).decode("utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return {}, False
    except (OSError, UnicodeError, ValueError) as exc:
        raise _Unreadable() from exc
    if not isinstance(data, dict):
        raise _Unreadable()
    crews = data.get("crews")
    out: dict[str, float] = {}
    for name, ts in (crews.items() if isinstance(crews, dict) else ()):
        # bool is an int subclass: never an ordering key.
        if not isinstance(name, str) or isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        if math.isfinite(ts) and ts > 0:
            out[name] = float(ts)
    return out, data.get("seeded") is True


def _write_unlocked(crews: dict[str, float], seeded: bool) -> None:
    payload = {"version": 1, "seeded": seeded, "crews": crews}
    atomic_write(recency_path(), json.dumps(payload, ensure_ascii=False), fsync=True)


def read_recency() -> dict[str, float]:
    """``{crew name: epoch seconds}`` of the user's last message to each crew."""
    try:
        with _lock:
            return _read_unlocked()[0]
    except _Unreadable:
        logger.warning("crew recency file is unreadable; treating it as empty", exc_info=True)
        return {}


def needs_seed() -> bool:
    """True until :func:`seed` has run once on this data home."""
    try:
        with _lock:
            return not _read_unlocked()[1]
    except _Unreadable:
        return False


def seed(found: dict[str, float]) -> dict[str, float]:
    """Fold the one-time backfill *found* into the file and mark it seeded.

    Called by the roster read with what the Crewmates DM threads already show
    the user typed before this record existed, so an upgrade does not start
    from an empty list. A live record wins over a seed for the same crew.
    Returns the merged map. Never raises.
    """
    try:
        with _lock:
            crews, seeded = _read_unlocked()
            if seeded:
                return crews
            for name, ts in found.items():
                if isinstance(name, str) and ts > crews.get(name, 0.0):
                    crews[name] = float(ts)
            _write_unlocked(crews, True)
            return crews
    except Exception:
        # Unreadable included: the backfill is served, never written over it.
        logger.warning("could not seed crew recency", exc_info=True)
        return dict(found)


def record_user_chat(name: str, ts: float | None = None) -> bool:
    """Record that the user just sent *name* a message. Returns True on a write.

    ``""`` is the default crew (a chat with no crew picked); the roster read
    resolves it. Blocking file IO: call off the event loop. Never raises.
    """
    if not isinstance(name, str):
        return False
    now = time.time() if ts is None else float(ts)
    try:
        with _lock:
            crews, seeded = _read_unlocked()
            held = crews.get(name, 0.0)
            if held and abs(now - held) < _WRITE_GRANULARITY_S:
                return False
            crews[name] = max(now, held)
            _write_unlocked(crews, seeded)
            return True
    except Exception:
        logger.warning("could not record crew recency for %r", name, exc_info=True)
        return False
