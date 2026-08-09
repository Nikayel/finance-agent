"""What `sbx run` actually does: feed a sealed journal to a sealed cell.

The host reads the journal, decides what the simulated present is, and sends
exactly one tick at a time. It then blocks until the cell answers that tick.
That blocking is the gate: the next record is not written to the pipe, so it is
not in the cell's memory, so there is nothing there for a strategy to find.

The cell receives the encoder and the protocol as *files in its working
directory*, because it has no site-packages and cannot import the installed
package. They are the same modules the host uses — one definition of the wire,
not two that drift.

Everything arriving from the cell is re-validated here. The far end is
untrusted code, and an order is only an order once the host says so.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__, canonical, cell, gate, ledger, portfolio, protocol, sandbox
from .cell import CellReport
from .errors import ProtocolError, SbxError
from .limits import Limits
from .store import SealedDataset

CELL_PACKAGE = "_sbx"
CELL_ENTRY = "main.py"

# What the cell needs to speak: the error types, the canonical encoder, the
# framing, and the runtime that wraps them in a Market.
_CELL_MODULES = ("__init__.py", "errors.py", "canonical.py", "protocol.py", "runtime.py")

_RECORD_KEYS = (
    "kind",
    "run_id",
    "data",
    "code",
    "seed",
    "sbx_version",
    "env_fingerprint",
    "outcome",
    "containment",
    "fills",
    "pnl",
    "position",
    "result_hash",
    "utime_us",
    "stime_us",
    "max_rss_bytes",
)


@dataclass(frozen=True)
class RunResult:
    """One execution: what the ledger will say, and why it says it."""

    record: dict[str, Any]
    report: CellReport
    failure: str | None

    @property
    def succeeded(self) -> bool:
        return self.record["outcome"] == "completed"


def cell_attachments() -> dict[str, Path]:
    """The files the cell needs, keyed by where they go in its workdir."""
    here = Path(__file__).parent
    files: dict[str, Path] = {
        f"{CELL_PACKAGE}/{name}": here / name for name in _CELL_MODULES
    }
    files[CELL_ENTRY] = here / "cell_entry.py"
    return files


def env_fingerprint() -> str:
    """What about this machine could make two runs of one tuple differ."""
    return canonical.hash_object(
        {
            "containment": list(sandbox.mechanisms()),
            "implementation": platform.python_implementation(),
            "machine": platform.machine(),
            "platform": sys.platform,
            "python": platform.python_version(),
        }
    )


def journal_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream a sealed journal, one record per committed line.

    Same commit rule as the ledger: a final line with no newline is a write
    that never finished, and is not a record.
    """
    with open(path, "rb") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.endswith(b"\n"):
                break  # a torn tail; upstream never finished writing it
            line = line.strip()
            if not line:
                continue
            try:
                record = canonical.decode(line)
            except Exception as error:
                raise SbxError(f"{path}: line {number}: {error}") from error
            if not isinstance(record, dict):
                raise SbxError(f"{path}: line {number} is not a journal record")
            yield record


class Feeder:
    """The host half of the conversation: one tick out, one answer back."""

    def __init__(self, records: Iterator[dict[str, Any]]) -> None:
        self._records = records
        self.portfolio = portfolio.Portfolio()
        self.ticks_sent = 0
        self.failure: str | None = None

    def talk(self, to_cell: BinaryIO, from_cell: BinaryIO) -> None:
        """Entry point for the cell's dialogue thread; never raises."""
        try:
            self._converse(to_cell, from_cell)
        except (ProtocolError, SbxError) as error:
            self.failure = str(error)
        except (BrokenPipeError, ValueError, OSError) as error:
            # The watchdog killed the cell mid-sentence, which is a containment
            # success, not a protocol bug. The report says which.
            self.failure = f"the cell stopped listening: {error}"

    def _converse(self, to_cell: BinaryIO, from_cell: BinaryIO) -> None:
        opening = protocol.read_frame(from_cell)
        if opening is None:
            self.failure = "the cell never spoke"
            return
        if opening["m"] == "error":
            self.failure = str(opening.get("reason", "the cell reported an error"))
            return
        if opening["m"] != "ready":
            raise ProtocolError(f"cell opened with {opening['m']!r}, expected 'ready'")

        for tick in gate.replay(self._records):
            # Fill first: an order placed at an earlier tick fills at *this*
            # trade, which the strategy has not been shown yet.
            for event in tick.events:
                self.portfolio.observe(event)

            protocol.write_frame(
                to_cell, {"m": "tick", "now": tick.now, "events": list(tick.events)}
            )
            self.ticks_sent += 1

            reply = protocol.read_frame(from_cell)
            if reply is None:
                self.failure = "the cell stopped answering"
                return
            kind = reply["m"]
            if kind == "done":
                break
            if kind == "error":
                self.failure = str(reply.get("reason", "the strategy failed"))
                return
            if kind != "decision":
                raise ProtocolError(f"cell sent {kind!r} where a decision was due")

            orders = reply.get("orders", [])
            if not isinstance(orders, list):
                raise ProtocolError("a decision's orders must be a list")
            for index, raw in enumerate(orders, start=1):
                self.portfolio.submit(portfolio.parse_order(raw, index))

        protocol.write_frame(to_cell, {"m": "stop"})


def execute(
    strategy_path: str | Path,
    dataset: SealedDataset,
    seed: int,
    *,
    limits: Limits = Limits(),
) -> RunResult:
    """Run one strategy against one sealed dataset and build its ledger record."""
    strategy = Path(strategy_path)
    if strategy.is_dir():
        raise SbxError(f"{strategy} is a directory, not a strategy")
    if not strategy.is_file():
        raise SbxError(f"no such strategy: {strategy}")

    code_hash = canonical.hash_file(strategy)
    feeder = Feeder(journal_records(dataset.data_path))
    report = cell.run(
        strategy,
        limits=limits,
        attachments=cell_attachments(),
        entry=CELL_ENTRY,
        dialogue=feeder.talk,
    )

    book = feeder.portfolio
    fills = [fill.as_record() for fill in book.fills]

    # The identity of a result is the result, not the run. Timings and memory
    # differ between two runs of identical inputs, so they are recorded and
    # deliberately left out of the hash.
    result_hash = canonical.hash_object(
        {
            "code": code_hash,
            "data": dataset.sha256,
            "fills": fills,
            "pnl": book.pnl,
            "position": book.position,
            "seed": seed,
        }
    )

    if report.outcome != "completed":
        outcome = report.outcome
    elif feeder.failure is not None:
        outcome = "failed"
    else:
        outcome = "completed"

    record = {
        "kind": "run",
        "run_id": ledger.next_run_id(),
        "data": dataset.sha256,
        "code": code_hash,
        "seed": seed,
        "sbx_version": __version__,
        "env_fingerprint": env_fingerprint(),
        "outcome": outcome,
        "containment": list(report.containment),
        "fills": fills,
        "pnl": book.pnl,
        "position": book.position,
        "result_hash": result_hash,
        "utime_us": report.utime_us,
        "stime_us": report.stime_us,
        "max_rss_bytes": report.max_rss_bytes,
    }
    assert set(record) == set(_RECORD_KEYS), "run record shape drifted"

    return RunResult(record=record, report=report, failure=feeder.failure)
