"""Shared machinery for the determinism suites.

Two modules ask the same question from opposite directions — one pins a
nondeterminism source and shows the result is stable, the other unpins it and
shows the instability was real — and the contrast only means something if both
run through the same machinery. That machinery lives here rather than in one
of the suites, because a test module importing another test module makes the
one that happens to be imported quietly load-bearing.

Sibling of `journals.py`: neither is a test, and neither is collected.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import journals

from sbx import ledger
from sbx.exits import EXIT_OK

SEED = 7

# Short on purpose: the repetition test replays this journal twenty times, and
# every extra record is twenty more round trips across the pipe.
TRADE_COUNT = 15

# What three additions of a tenth leave behind in binary floating point. Named
# rather than inlined, because it is the exact value these milestones exist to
# keep out of the ledger.
FLOAT_TENTHS = "0.30000000000000004"


def trades() -> list[dict]:
    """Trades a second apart, each at its own price."""
    return [
        journals.trade(
            price=f"118{200 + index}.50",
            at=f"2026-07-29T14:30:{index + 1:02d}.000000+00:00",
            sequence=index + 1,
        )
        for index in range(TRADE_COUNT)
    ]


def strategy_file(directory: Path, source: str, name: str = "strategy.py") -> Path:
    """Write a strategy the CLI will hash and the cell will execute."""
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def sha256_of(path: Path) -> str:
    """The digest of a file, computed independently of sbx."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_many(
    sbx_cli, strategy: Path, data: str, *, times: int, seed: int = SEED
) -> list[dict]:
    """Run one tuple `times` times over and return only the records it added.

    Returning the tail rather than the whole ledger is what lets a test run the
    same strategy under two seeds and still talk about each batch separately.
    """
    before = len(ledger.entries_of("run"))
    for _ in range(times):
        result = sbx_cli("run", str(strategy), "--data", data, "--seed", str(seed))
        assert result.returncode == EXIT_OK, result.stderr

    runs = ledger.entries_of("run")
    assert len(runs) == before + times
    return runs[before:]


def hashes_of(runs: list[dict]) -> set[str]:
    return {run["result_hash"] for run in runs}


def positions_of(runs: list[dict]) -> set[str]:
    """The recorded position *text*, which is what the hash is taken over."""
    return {run["position"] for run in runs}
