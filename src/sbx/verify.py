"""`sbx verify` — re-execute a recorded run and diff it byte for byte.

The verdict is one of three words, and each of them is a claim about something
different.

**REPRODUCED** — the tuple was re-executed and the canonical result hashed to
the same bytes. **DIVERGED** — it was re-executed and did not, and the first
field that differs is named, because "something changed" is not a finding.
**TAMPERED** — the sealed dataset no longer hashes to what the run recorded, so
nothing was re-executed at all. That last distinction matters: a verify that
re-ran against changed data and reported DIVERGED would be blaming the code for
someone editing the inputs.

Verify also reports whether it ran in the same environment as the original. A
run recorded on another machine that reproduces here still says the environment
differed. Claiming a clean reproduction while quietly omitting that it happened
somewhere else is exactly the kind of small lie this project exists to make
impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import canonical, ledger, runner, store
from .errors import SbxError
from .limits import Limits

VERDICTS = ("REPRODUCED", "DIVERGED", "TAMPERED")

# Searched in this order, so the first divergence reported is the most
# summary one: a position that differs explains a hundred differing fills.
_SCALAR_FIELDS = ("position", "pnl")
_FILL_FIELDS = ("at", "side", "size", "price")


@dataclass(frozen=True)
class Verification:
    """What re-executing one recorded run established."""

    verdict: str
    run_id: str
    recorded_hash: str
    replayed_hash: str | None
    divergence: str | None
    same_environment: bool
    #: What the original run actually enforced. Carried here so a caller can
    #: say whether the result is worth anything without re-reading the ledger
    #: for a record this function already had in its hand.
    containment: tuple[str, ...]


def find_run(run_id: str) -> dict[str, Any]:
    """The ledger record for a run id."""
    for entry in ledger.entries_of("run"):
        if entry.get("run_id") == run_id:
            return entry
    raise SbxError(f"no run {run_id!r} in the ledger")


def first_divergence(
    recorded: dict[str, Any], replayed: dict[str, Any]
) -> str | None:
    """The first field that differs, in a fixed order, as one line."""
    for field in _SCALAR_FIELDS:
        if recorded.get(field) != replayed.get(field):
            return (
                f"{field}: recorded {recorded.get(field)}, "
                f"replayed {replayed.get(field)}"
            )

    was = recorded.get("fills") or []
    now = replayed.get("fills") or []
    for index in range(min(len(was), len(now))):
        for field in _FILL_FIELDS:
            if was[index].get(field) != now[index].get(field):
                return (
                    f"fills[{index}].{field}: recorded {was[index].get(field)}, "
                    f"replayed {now[index].get(field)}"
                )
    if len(was) != len(now):
        return f"fills: recorded {len(was)}, replayed {len(now)}"
    return None


def verify(run_id: str, *, limits: Limits = Limits()) -> Verification:
    """Re-execute a recorded run and say what happened, precisely."""
    record = find_run(run_id)
    containment = tuple(record.get("containment") or ())
    if record.get("result_hash") is None:
        raise SbxError(
            f"{run_id} was stopped before it finished ({record.get('outcome')}), "
            f"so it recorded no result to reproduce"
        )
    recorded_hash = str(record["result_hash"])
    same_environment = record.get("env_fingerprint") == runner.env_fingerprint()

    dataset = store.load(str(record["data"]))
    if not dataset.verify():
        return Verification(
            verdict="TAMPERED",
            run_id=run_id,
            recorded_hash=recorded_hash,
            replayed_hash=None,
            divergence=(
                f"sealed dataset {dataset.short} no longer hashes to what the "
                f"run recorded, so nothing was re-executed"
            ),
            same_environment=same_environment,
            containment=containment,
        )

    strategy = store.code_path(str(record["code"]))
    result = runner.execute(
        strategy, dataset, int(record["seed"]), limits=limits, run_id=run_id
    )

    # Through the canonical encoding, so the replay is compared in the same
    # form the ledger stores: Decimals as their canonical text, not as objects
    # that merely compare equal.
    replayed = canonical.decode(canonical.encode(result.record))
    replayed_hash = str(replayed["result_hash"])

    if replayed_hash == recorded_hash:
        return Verification(
            verdict="REPRODUCED",
            run_id=run_id,
            recorded_hash=recorded_hash,
            replayed_hash=replayed_hash,
            divergence=None,
            same_environment=same_environment,
            containment=containment,
        )

    return Verification(
        verdict="DIVERGED",
        run_id=run_id,
        recorded_hash=recorded_hash,
        replayed_hash=replayed_hash,
        divergence=first_divergence(record, replayed)
        or "the result hash differs while every recorded field matches",
        same_environment=same_environment,
        containment=containment,
    )
