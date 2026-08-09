"""Component 3, the append-only half — the record of everything sbx has done.

One canonical JSONL record per line at ``~/.sbx/ledger.jsonl``. Records are
only ever appended; nothing here rewrites a byte that has already been written.

**The newline is the commit marker.** A record counts as committed when, and
only when, its terminating ``\\n`` is on disk. A trailing fragment without one
is an append that did not finish — a `kill -9` between the write and the flush,
a full disk — and readers ignore it in silence. Deciding commitment by
parseability instead would be subtly wrong: a half-written record can be
perfectly valid JSON right up to the point where it stops meaning what it says.

Damage to a *complete* line is the opposite case. The ledger already promised
that record was durable, so it raises rather than skipping quietly.

Which is why **the next append discards an unterminated tail** before it
writes. Append-only is a promise about committed records; a fragment is an
append that never happened. Leaving it in place would fuse it with the next
record into a line that *is* newline-terminated and *does* not parse — one
interrupted write and the ledger is bricked for good. Discarding uncommitted
bytes is what keeps the promise honest rather than merely literal. The repair
and the write happen under an exclusive `flock`, so two appending processes
cannot race over the boundary.

Note there are no timestamps in here. Ordering is append order, which is the
only ordering that cannot disagree with itself, and it keeps the ledger free of
ambient nondeterminism.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import canonical, paths
from .errors import LedgerCorruptError, SbxError

LEDGER_KINDS: tuple[str, ...] = ("dataset", "run")

_RUN_ID_DIGITS = 4
_TAIL_SCAN_BYTES = 1 << 20


def _committed_size(handle: int, size: int) -> int:
    """Bytes up to and including the last newline; 0 if there is none."""
    position = size
    while position > 0:
        start = max(0, position - _TAIL_SCAN_BYTES)
        chunk = os.pread(handle, position - start, start)
        index = chunk.rfind(b"\n")
        if index != -1:
            return start + index + 1
        position = start
    return 0


def _discard_torn_tail(handle: int) -> None:
    """Drop an unterminated trailing fragment so the next record starts clean."""
    size = os.lseek(handle, 0, os.SEEK_END)
    committed = _committed_size(handle, size)
    if committed != size:
        os.ftruncate(handle, committed)


def path() -> Path:
    """The ledger file, re-resolved on every call."""
    return paths.ledger_path()


def append(record: Mapping[str, Any]) -> dict[str, Any]:
    """Append one record durably, and return it as stored.

    Encoding happens before the file is touched, so a record that has no
    canonical form leaves the ledger byte-for-byte unchanged.
    """
    if not isinstance(record, Mapping):
        raise SbxError(f"ledger records must be mappings, got {type(record).__name__}")

    stored = dict(record)
    kind = stored.get("kind")
    if kind not in LEDGER_KINDS:
        raise SbxError(
            f"unknown ledger kind {kind!r}; expected one of {', '.join(LEDGER_KINDS)}"
        )

    line = canonical.encode_line(stored)

    ledger = path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # O_RDWR because repairing a torn tail means reading and truncating, not
    # just writing; O_APPEND so every write still lands at the end.
    handle = os.open(ledger, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _discard_torn_tail(handle)
        # One write of the whole line: under O_APPEND that is what keeps
        # concurrent appenders from interleaving into each other's records.
        written = os.write(handle, line)
        os.fsync(handle)
    finally:
        os.close(handle)  # releases the lock

    if written != len(line):
        # A short write cannot have included the terminating newline, since
        # that is the last byte — so the fragment is already uncommitted and
        # readers will skip it. Say so loudly anyway.
        raise SbxError(
            f"short write to {ledger}: {written} of {len(line)} bytes; "
            f"the record did not commit"
        )

    return stored


def entries() -> list[dict[str, Any]]:
    """Every committed record, in append order."""
    ledger = path()
    try:
        raw = ledger.read_bytes()
    except FileNotFoundError:
        return []

    # Everything up to the last newline has committed. Whatever follows it is
    # an append that never finished, and is never parsed — not even to look.
    committed, newline, _torn = raw.rpartition(b"\n")
    if not newline:
        return []

    records = []
    for number, line in enumerate(committed.split(b"\n"), start=1):
        if not line:
            # A bare newline is a committed record of zero bytes. sbx never
            # writes one, so it means bytes went missing between two commit
            # markers — damage, not something to skip politely.
            raise LedgerCorruptError(
                f"{ledger}: line {number} is empty; a committed record cannot "
                f"be zero bytes, so something has eaten this one"
            )
        try:
            records.append(canonical.decode(line))
        except (ValueError, SbxError) as error:
            raise LedgerCorruptError(
                f"{ledger}: line {number} is committed but will not parse: {error}"
            ) from error
    return records


def entries_of(kind: str) -> list[dict[str, Any]]:
    """Every committed record of one kind, in append order."""
    return [entry for entry in entries() if entry.get("kind") == kind]


def next_run_id() -> str:
    """The id the next run will be recorded under.

    Derived from how many runs the ledger already holds, so it is a function of
    state rather than of the clock or a random source.
    """
    return f"run-{len(entries_of('run')) + 1:0{_RUN_ID_DIGITS}d}"
