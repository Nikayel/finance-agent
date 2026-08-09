"""What each verb does.

Handlers are thin on purpose: they translate parsed arguments into library
calls and library results into text. Anything that needs a test of its own
belongs in the component modules, not here.
"""

from __future__ import annotations

import argparse

from . import ledger, store
from .errors import SbxError
from .exits import EXIT_OK

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _human_bytes(count: int) -> str:
    """Display only — the ledger stores the exact integer, never this."""
    size = float(count)
    for unit in _BYTE_UNITS:
        if size < 1024 or unit == _BYTE_UNITS[-1]:
            return f"{count} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def seal(args: argparse.Namespace) -> int:
    """Snapshot a journal into the content-addressed store."""
    dataset, created = store.seal(args.journal)
    if created:
        ledger.append(
            {
                "kind": "dataset",
                "sha256": dataset.sha256,
                "bytes": dataset.bytes,
                "records": dataset.records,
            }
        )
    print(f"{'sealed' if created else 'already sealed'} {dataset.sha256}")
    print(f"  {dataset.records} records, {_human_bytes(dataset.bytes)}")
    print(f"  {dataset.directory}")
    return EXIT_OK


def ls(args: argparse.Namespace) -> int:
    """List sealed datasets and recorded runs, re-checking every dataset.

    The integrity check is done here rather than being trusted from the
    manifest: a store that only reports what it was told is not a store you can
    audit. Re-hashing on every listing is the demo, and it is why tampering
    with a sealed byte shows up in one command.
    """
    del args

    print("DATASETS")
    datasets = store.all_datasets()
    if not datasets:
        print("  (none)")
    for dataset in datasets:
        status = "ok" if dataset.verify() else "TAMPERED"
        print(
            f"  {dataset.short}  {dataset.records:>7} records  "
            f"{_human_bytes(dataset.bytes):>9}  {status}"
        )

    print()
    print("RUNS")
    runs = ledger.entries_of("run")
    if not runs:
        print("  (none)")
    for run in runs:
        print(f"  {run['run_id']}  {run['strategy']}  {run['data'][:12]}  seed {run['seed']}")

    return EXIT_OK


def pending(milestone: int):
    """A handler for a verb whose implementation is still ahead of us."""

    def handler(args: argparse.Namespace) -> int:
        raise SbxError(f"{args.verb}: not implemented yet (milestone {milestone})")

    return handler
