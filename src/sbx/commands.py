"""What each verb does.

Handlers are thin on purpose: they translate parsed arguments into library
calls and library results into text. Anything that needs a test of its own
belongs in the component modules, not here.
"""

from __future__ import annotations

import argparse
import sys

from . import canonical, ledger, runner, store
from . import verify as verification
from .errors import SbxError
from .exits import EXIT_ERROR, EXIT_OK

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")

# Without these two, a strategy can open the sealed journal and read its own
# future. The run still completes and still hashes; what makes the difference
# visible is saying so everywhere a run is shown.
FULL_CONTAINMENT = frozenset({"network", "filesystem"})


def _uncontained(containment: object) -> bool:
    if not isinstance(containment, (list, tuple)):
        return True
    return not FULL_CONTAINMENT.issubset(set(containment))


def _warn_uncontained(run_id: str) -> None:
    print(
        f"sbx: {run_id} ran without network or filesystem confinement, so the "
        f"strategy could have read the sealed data directly. Treat the result "
        f"as unverified.",
        file=sys.stderr,
    )


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
    for entry in runs:
        mark = "  UNCONTAINED" if _uncontained(entry.get("containment")) else ""
        print(
            f"  {entry['run_id']}  {entry['outcome']:<16}  "
            f"data {entry['data'][:12]}  code {entry['code'][:12]}  "
            f"seed {entry['seed']}  {entry['ticks']} ticks  "
            f"pnl {entry['pnl']}{mark}"
        )

    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    """Execute a strategy against a sealed dataset and record what happened."""
    dataset = store.resolve(args.data)
    result = runner.execute(args.strategy, dataset, args.seed)
    record = ledger.append(result.record)

    print(f"{record['run_id']}  {record['outcome']}")
    print(
        f"  data {dataset.short}  code {record['code'][:12]}  seed {record['seed']}"
    )
    print(
        f"  {len(record['fills'])} fills, "
        f"pnl {canonical.decimal_text(record['pnl'])}, "
        f"position {canonical.decimal_text(record['position'])}"
    )
    print(f"  {record['ticks']} ticks")
    print(f"  result {record['result_hash']}")

    if _uncontained(record["containment"]):
        _warn_uncontained(record["run_id"])

    if result.succeeded:
        return EXIT_OK
    # A contained strategy is a successful containment and a failed run. Say so
    # on stderr, having already written the run to the ledger: a run that
    # failed is still something sbx did.
    reason = result.failure or result.report.detail
    print(f"sbx: {record['run_id']} did not complete: {reason}", file=sys.stderr)
    return EXIT_ERROR


def verify(args: argparse.Namespace) -> int:
    """Re-execute a recorded run and report whether it came back the same."""
    result = verification.verify(args.run_id)

    print(f"{result.verdict}  {result.run_id}")
    print(f"  recorded {result.recorded_hash}")
    if result.replayed_hash is not None:
        print(f"  replayed {result.replayed_hash}")
    if not result.same_environment:
        # Never silently: a result from another machine is a different claim.
        print("  environment differs from the recorded run")
    if result.divergence is not None:
        print(f"  first divergence: {result.divergence}")

    if _uncontained(verification.find_run(result.run_id).get("containment")):
        _warn_uncontained(result.run_id)

    return EXIT_OK if result.verdict == "REPRODUCED" else EXIT_ERROR


def pending(milestone: int):
    """A handler for a verb whose implementation is still ahead of us."""

    def handler(args: argparse.Namespace) -> int:
        raise SbxError(f"{args.verb}: not implemented yet (milestone {milestone})")

    return handler
