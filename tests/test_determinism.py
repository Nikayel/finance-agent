"""Milestone 6 — the determinism budget, and what is deliberately outside it.

`sbx verify` is only worth running if a re-execution of the same tuple is
*expected* to produce the same bytes. That expectation is not a hope about
CPython; it is a short list of pinned decisions, and this file is where each of
them is held to account:

* **Repetition.** Twenty consecutive runs of one (code, data, seed) agree on
  ``result_hash`` — twenty, because two runs agree by luck often enough that
  the assertion would not be evidence of anything.
* **Hash randomisation.** The cell runs with ``PYTHONHASHSEED`` pinned, so a
  set of strings iterates the same way in every process. Without it, a
  strategy that walks a set is reproducible only within one interpreter's
  lifetime — and the divergence appears months later, in someone else's
  process, for no reason anyone can see.
* **The seed.** The host injects it, and the cell seeds both the generator the
  strategy is handed and the module-level one that a careless ``import
  random`` reaches for. Same seed, same numbers; different seed, different
  numbers — asserted on what the strategy *did*, not merely on the hash, which
  carries the seed itself and would differ either way.
* **Decimal end to end.** Three orders of a tenth leave a position of exactly
  ``0.3``. The same three additions in binary floating point leave
  ``0.30000000000000004``, and a ledger recording that would be recording the
  accumulation order rather than the trade.

And one thing that is *not* pinned, on purpose: a strategy can still call
``time.time()`` or ``os.urandom``. sbx does not stop it, and a sandbox that
claimed to would be promising something no one can keep. What sbx promises is
that such a run **declares itself** — two executions produce two different
``result_hash`` values, so `sbx verify` says DIVERGED instead of quietly
blessing a result nobody can reproduce. Detection is the guarantee.

Everything runs through the installed console script and is read back from the
ledger, because the recorded ``result_hash`` — not an in-process return value —
is the thing `sbx verify` will be comparing against months from now.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from harness import (
    FLOAT_TENTHS,
    SEED,
    hashes_of,
    positions_of,
    run_many,
    strategy_file,
)

from sbx import ledger

REPEATS = 20

# A fresh interpreter per run is where a per-process hash salt would show up,
# so what matters here is the number of interpreters, not the number of ticks.
SALT_REPEATS = 4

# Three is enough to tell "different every time" from "different once".
AMBIENT_REPEATS = 3

# Measured and recorded, and deliberately outside `result_hash`.
RESOURCE_KEYS = ("utime_us", "stime_us", "max_rss_bytes")


# --- strategies, as the plain text a user would write -----------------------

TRIVIAL_STRATEGY = """\
def strategy(market):
    seen = 0
    for tick in market.ticks():
        seen += len(tick.events)
"""

# The size is a fingerprint of two things that move when the hash salt moves:
# the order a set of strings iterates in, and the raw hash values themselves.
# A dict would prove nothing — dicts iterate in insertion order whatever the
# salt is — so the set is the load-bearing part of this strategy.
SALTED_STRATEGY = """\
from decimal import Decimal

KEYS = tuple("key-%04d" % number for number in range(500))


def strategy(market):
    walked = "|".join(set(KEYS))
    fingerprint = abs(hash(walked)) + sum(abs(hash(key)) for key in KEYS)
    placed = False
    for tick in market.ticks():
        if not placed:
            market.order("BUY", Decimal(fingerprint % 100000 + 1) * Decimal("0.00001"))
            placed = True
"""

# Reads the generator the runtime hands the strategy. Three samples rather than
# one: the sum is a fingerprint of the stream's start, not of a single draw.
MARKET_RANDOM_STRATEGY = """\
from decimal import Decimal


def strategy(market):
    placed = 0
    for tick in market.ticks():
        if placed < 3:
            # str() of a float is its shortest round-tripping form, so this
            # reads the number exactly instead of re-rounding it. The whole
            # unit keeps the size positive if random() ever returns 0.0.
            market.order("BUY", Decimal(1) + Decimal(str(market.random.random())))
            placed += 1
"""

# The same strategy reaching for the module-level generator instead — which is
# what people actually write, and what the runtime therefore has to seed too.
GLOBAL_RANDOM_STRATEGY = """\
import random
from decimal import Decimal


def strategy(market):
    placed = 0
    for tick in market.ticks():
        if placed < 3:
            market.order("BUY", Decimal(1) + Decimal(str(random.random())))
            placed += 1
"""

TENTHS_STRATEGY = """\
from decimal import Decimal


def strategy(market):
    placed = 0
    for tick in market.ticks():
        if placed < 3:
            market.order("BUY", Decimal("0.1"))
            placed += 1
"""

CLOCK_STRATEGY = """\
import time
from decimal import Decimal


def strategy(market):
    placed = False
    for tick in market.ticks():
        if not placed:
            nanoseconds = time.time_ns() % 1_000_000_000
            market.order("BUY", Decimal(nanoseconds + 1) * Decimal("0.000000001"))
            placed = True
"""

URANDOM_STRATEGY = """\
import os
from decimal import Decimal


def strategy(market):
    placed = False
    for tick in market.ticks():
        if not placed:
            drawn = int.from_bytes(os.urandom(8), "big") % 1_000_000_000
            market.order("BUY", Decimal(drawn + 1) * Decimal("0.000000001"))
            placed = True
"""

AMBIENT_STRATEGIES = [
    pytest.param(CLOCK_STRATEGY, id="clock"),
    pytest.param(URANDOM_STRATEGY, id="urandom"),
]

SEEDED_STRATEGIES = [
    pytest.param(MARKET_RANDOM_STRATEGY, id="market-random"),
    pytest.param(GLOBAL_RANDOM_STRATEGY, id="module-random"),
]


# ---------------------------------------------------------------------------
# repetition — twenty runs of one tuple, one result
# ---------------------------------------------------------------------------


def test_twenty_runs_of_the_same_tuple_agree_on_one_result_hash(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    runs = run_many(sbx_cli, strategy, sealed_data, times=REPEATS)

    # Twenty separate executions, not one execution counted twenty times.
    assert len({run["run_id"] for run in runs}) == REPEATS
    assert len(hashes_of(runs)) == 1


def test_the_resource_numbers_are_recorded_and_hold_no_sway_over_the_hash(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    runs = run_many(sbx_cli, strategy, sealed_data, times=REPEATS)

    assert len(hashes_of(runs)) == 1
    # Whether two runs on *this* machine happened to burn the same number of
    # microseconds is a fact about the machine, not about sbx, so nothing is
    # asserted about it. The claim is the other way round: these numbers are
    # measured and written down, and the single hash above is true regardless
    # of what they say.
    for run in runs:
        for key in RESOURCE_KEYS:
            assert isinstance(run[key], int)
            assert run[key] >= 0


# ---------------------------------------------------------------------------
# hash randomisation — the salt is pinned, so a set iterates the same way twice
# ---------------------------------------------------------------------------


def test_a_strategy_that_walks_a_set_of_strings_reproduces(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, SALTED_STRATEGY)

    runs = run_many(sbx_cli, strategy, sealed_data, times=SALT_REPEATS)

    assert len(hashes_of(runs)) == 1
    # Every run is a fresh interpreter, so an unpinned salt would give each of
    # them its own set ordering and its own hash values — and therefore its own
    # order size. That the size is the same one every time is the whole claim.
    assert len(positions_of(runs)) == 1
    # A fingerprint that collapsed to a constant would satisfy the lines above
    # while proving nothing, so check the strategy actually traded.
    assert Decimal(runs[0]["position"]) > 0
    assert runs[0]["fills"] != []


# ---------------------------------------------------------------------------
# the seed — injected by the host, honoured by both generators in the cell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", SEEDED_STRATEGIES)
def test_the_same_seed_hands_the_strategy_the_same_numbers(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str, source: str
) -> None:
    strategy = strategy_file(tmp_path, source)

    runs = run_many(sbx_cli, strategy, sealed_data, times=2)

    # Unseeded, `random` draws its state from the OS at import, so two runs
    # would disagree here. Agreeing is the evidence that the host sent a seed
    # and the cell applied it — to the module-level generator as well as to the
    # one the strategy is handed.
    assert len(hashes_of(runs)) == 1
    assert len(positions_of(runs)) == 1
    assert Decimal(runs[0]["position"]) > 0


@pytest.mark.parametrize("source", SEEDED_STRATEGIES)
def test_a_different_seed_gives_the_strategy_different_numbers(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str, source: str
) -> None:
    strategy = strategy_file(tmp_path, source)

    [first] = run_many(sbx_cli, strategy, sealed_data, times=1, seed=SEED)
    [second] = run_many(sbx_cli, strategy, sealed_data, times=1, seed=SEED + 1)

    # The position is the load-bearing assertion. `seed` is itself inside
    # `result_hash`, so the two hashes would differ even if the cell threw the
    # number away and seeded from the clock — only what the strategy *did* can
    # tell those two implementations apart.
    assert first["position"] != second["position"]
    assert first["result_hash"] != second["result_hash"]


# ---------------------------------------------------------------------------
# Decimal end to end — a tenth is a tenth, from the strategy to the ledger
# ---------------------------------------------------------------------------


def test_three_orders_of_a_tenth_leave_exactly_three_tenths(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TENTHS_STRATEGY)

    [run] = run_many(sbx_cli, strategy, sealed_data, times=1)

    assert [fill["size"] for fill in run["fills"]] == ["0.1", "0.1", "0.1"]
    # The exact text, not a numeric comparison: `result_hash` is taken over the
    # canonical string, so "0.3" and "0.300" are different results even though
    # they are the same quantity.
    assert run["position"] == "0.3"
    # The same three additions, done in the way sbx refuses to do them. If this
    # ever stops being the float answer, the contrast above has gone stale
    # rather than the requirement having gone away.
    assert str(0.1 + 0.1 + 0.1) == FLOAT_TENTHS
    assert run["position"] != FLOAT_TENTHS


# ---------------------------------------------------------------------------
# the mutation test — ambient nondeterminism is caught, never hidden
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", AMBIENT_STRATEGIES)
def test_a_strategy_that_reaches_for_the_machine_diverges_every_run(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str, source: str
) -> None:
    """The mutation test for everything above it.

    sbx does not stop a strategy calling the clock or the entropy pool, and
    claiming otherwise would be a guarantee no sandbox can keep. The guarantee
    is *detection*: a run that depends on ambient state produces a different
    ``result_hash`` every time, so `sbx verify` reports DIVERGED rather than
    blessing a result nobody can reproduce.

    It earns its place by being the assertion that fails if `result_hash` ever
    stops depending on what the strategy actually did — at which point every
    REPRODUCED verdict in this file would be worthless and every other test
    here would still pass.
    """
    strategy = strategy_file(tmp_path, source)

    runs = run_many(sbx_cli, strategy, sealed_data, times=AMBIENT_REPEATS)

    assert len(positions_of(runs)) == AMBIENT_REPEATS
    assert len(hashes_of(runs)) == AMBIENT_REPEATS
