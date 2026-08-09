"""Component 1 — the host-side feeder and the simulated clock.

The host owns time. :func:`replay` turns a sealed journal into a sequence of
ticks, and a tick reveals exactly one record at exactly one simulated instant.
The invariant the whole product rests on is that **no tick ever contains an
event whose own timestamp is later than that tick's `now`** — and it holds by
construction here, because `now` *is* the timestamp of the record being
revealed.

Two details of the upstream dialect shape this module.

**The timestamp field's name differs per record type**, and two types have no
timestamp at all. There is no single canonical time field to read.

**Exchange clocks are not monotonic.** A frame received later can carry an
earlier event time, so the simulated clock is clamped: it is the maximum of
where it was and where the record says it is. Time in a simulation may stand
still; it may not run backwards, because a strategy that saw `now = T` must
never afterwards be told it is `T - 1`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .errors import SbxError

# Which field carries each replayed type's timestamp.
TIMESTAMP_FIELDS = {"trade": "executed_at", "book_update": "event_time"}

# Diagnostics, not events: `raw` is the unparsed wire frame and `error` is a
# note that something upstream could not be parsed. Neither is market data.
SKIPPED_TYPES = frozenset({"raw", "error"})

# Control records. They matter — a disconnect invalidates book state — but they
# carry no time of their own, so they happen at whatever the clock already says.
UNTIMED_TYPES = frozenset({"gap", "disconnect"})

REPLAYED_TYPES = frozenset(TIMESTAMP_FIELDS) | UNTIMED_TYPES

# What the clock reads before any timestamped record has arrived. A journal can
# open with a `disconnect` — the feed was already down when the session started
# — and that is exactly the event a strategy must not miss, so the record is
# replayed rather than dropped. It needs *some* instant, and the only one that
# cannot have run ahead of whatever follows is the beginning of time.
EPOCH = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class Tick:
    """One simulated instant, and everything revealed at it."""

    now: str
    events: tuple[dict[str, Any], ...]


def _instant(record: dict[str, Any], kind: str, number: int) -> datetime:
    field = TIMESTAMP_FIELDS[kind]
    stamp = record.get(field)
    if not isinstance(stamp, str) or not stamp:
        raise SbxError(f"journal record {number}: {kind} has no {field}")
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError as error:
        raise SbxError(
            f"journal record {number}: {field} is not an ISO-8601 instant: {stamp!r}"
        ) from error
    if moment.tzinfo is None:
        raise SbxError(
            f"journal record {number}: {field} has no timezone, so it names no "
            f"instant: {stamp!r}"
        )
    return moment


def replay(records: Iterable[dict[str, Any]]) -> Iterator[Tick]:
    """Turn journal records into ticks, in file order, on a monotonic clock.

    A generator on purpose: a journal is arbitrarily long, and materialising it
    would put the whole future in the host's memory at once — which is not a
    correctness bug, but is the exact shape of thinking this project exists to
    avoid.
    """
    now_text = EPOCH
    now_moment = datetime.fromisoformat(EPOCH)

    for number, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise SbxError(
                f"journal record {number} is a {type(record).__name__}, not a record"
            )

        kind = record.get("type")
        if not isinstance(kind, str):
            raise SbxError(
                f"journal record {number}: record type must be a string, got {kind!r}"
            )
        if kind in SKIPPED_TYPES:
            continue
        if kind not in REPLAYED_TYPES:
            raise SbxError(f"journal record {number}: unknown record type {kind!r}")

        if kind not in UNTIMED_TYPES:
            moment = _instant(record, kind, number)
            if moment > now_moment:
                # The clock only moves forward, and it moves to a string the
                # journal actually wrote. Re-rendering it through isoformat()
                # would drop the microseconds on a whole second, so one instant
                # would reach the ledger as two different strings — and
                # result_hash is over the strings.
                now_moment = moment
                now_text = record[TIMESTAMP_FIELDS[kind]]

        yield Tick(now=now_text, events=(dict(record),))
