"""Milestone 6's exit gate, as a test: every run a ledger holds reproduces.

The per-run tests prove `sbx verify` works on the run in front of it. That is
not the same claim. The gate is that a whole ledger — several strategies,
several seeds, orders and no orders, one journal — comes back REPRODUCED
without exception, because a reproducibility tool that works on the example
you picked is not a reproducibility tool.
"""

from __future__ import annotations

from pathlib import Path

import journals

from sbx import ledger, verify
from sbx.exits import EXIT_OK

# One that trades constantly, one that trades on a condition, one that never
# trades at all. The third matters: an empty fill list is a result too, and it
# is the one a broken hash is most likely to collide on.
STRATEGIES = {
    "eager.py": """
from decimal import Decimal


def strategy(market):
    for tick in market.ticks():
        for event in tick.events:
            if event.get("type") == "trade":
                market.order("BUY", Decimal("0.001"))
""",
    "picky.py": """
from decimal import Decimal


def strategy(market):
    previous = None
    for tick in market.ticks():
        for event in tick.events:
            if event.get("type") != "trade":
                continue
            price = Decimal(event["price"])
            if previous is not None and price > previous:
                market.order("SELL", Decimal("0.002"))
            previous = price
""",
    "idle.py": """
def strategy(market):
    for _tick in market.ticks():
        pass
""",
}

SEEDS = (1, 42)


def _journal(tmp_path: Path) -> Path:
    records = [
        journals.trade(
            price=f"{118200 + (index * 7) % 53}.{index % 100:02d}",
            at=f"2026-07-29T14:30:{index % 60:02d}.{index:06d}+00:00",
            sequence=index,
        )
        for index in range(1, 31)
    ]
    return journals.write_journal(tmp_path / "events.jsonl", records)


def test_every_recorded_run_reproduces(sbx_cli, sbx_home: Path, tmp_path: Path) -> None:
    sealed = sbx_cli("seal", str(_journal(tmp_path)))
    assert sealed.returncode == EXIT_OK
    data = sealed.stdout.split()[1]

    for name, source in STRATEGIES.items():
        strategy = tmp_path / name
        strategy.write_text(source)
        for seed in SEEDS:
            result = sbx_cli("run", str(strategy), "--data", data, "--seed", str(seed))
            assert result.returncode == EXIT_OK, result.stderr

    runs = ledger.entries_of("run")
    assert len(runs) == len(STRATEGIES) * len(SEEDS)

    for entry in runs:
        checked = verify.verify(entry["run_id"])
        assert checked.verdict == "REPRODUCED", (
            f"{entry['run_id']} ({entry['ticks']} ticks, "
            f"{len(entry['fills'])} fills) did not reproduce: {checked.divergence}"
        )
        assert checked.same_environment


def test_the_sweep_is_not_vacuous(sbx_cli, sbx_home: Path, tmp_path: Path) -> None:
    """The runs above must differ from each other, or the sweep proves nothing.

    Six runs that all produced the same empty result would pass the gate above
    while establishing only that zero equals zero.
    """
    sealed = sbx_cli("seal", str(_journal(tmp_path)))
    data = sealed.stdout.split()[1]

    for name, source in STRATEGIES.items():
        strategy = tmp_path / name
        strategy.write_text(source)
        sbx_cli("run", str(strategy), "--data", data, "--seed", str(SEEDS[0]))

    runs = ledger.entries_of("run")
    assert len({entry["result_hash"] for entry in runs}) == len(STRATEGIES)
    assert {len(entry["fills"]) for entry in runs} != {0}
