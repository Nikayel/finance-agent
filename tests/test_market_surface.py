"""The `Market` surface holds nothing but the present.

This file closes the one acceptance criterion of the time gate that the rest of
the suite states but never probes from the inside:

    `Market` has no attribute reachable by `dir()`/`gc`/`__subclasses__`
    walking that exposes future data — the data simply is not in the cell's
    process memory yet.

The claim is deliberately *not* about privacy. A leading underscore is a
convention, and a strategy is untrusted code that has no reason to honour one.
The claim is about **absence**: the host writes one tick to the pipe and then
blocks for the answer, so at the instant a strategy is running there is no
later record anywhere in its address space to find. Four probes, each run as a
real strategy through the real `sbx run`:

* **The reachability walk.** Every name in `dir(market)` and its value,
  `vars(market)`, every object `gc.get_objects()` knows about, and the
  transitive closure of `object.__subclasses__()` — from all of it, every dict
  shaped like a journal record. None is stamped later than the simulated
  present, or the strategy exits 97 and this file fails.
* **The pipe holds nothing.** The strategy reaches the host pipe, puts it in
  non-blocking mode and reads. `BlockingIOError` is the whole proof: the bytes
  of the next tick have not been written, so they cannot be stolen.
* **One record per tick, ever.** A strategy that remembers every record it has
  been shown never accumulates more of them than it has had ticks.
* **The surface is five names.** `now`, `events`, `order`, `ticks`, `random` —
  asserted both in-process and from inside a live cell.

A probe that finds nothing because it is broken proves nothing, so each one
reports how much it actually reached and the tests assert on that too — and
the reachability walk is calibrated against a run with a future record
deliberately planted in the cell, which it has to find.
"""

from __future__ import annotations

import hashlib
import io
import random
import re
from pathlib import Path

import pytest

import journals

from sbx import ledger
from sbx.exits import EXIT_OK
from sbx.runtime import Market

SEED = 11

TRADE_COUNT = 16

# One `error` record (never replayed) and one `gap` (replayed, but carrying no
# timestamp of its own) — the two shapes a seek-ahead probe is most likely to
# trip over.
EXPECTED_TICKS = TRADE_COUNT + 1

# Everything a strategy is documented to be able to name on a `Market`.
# `order` and `ticks` are methods on the class; the other three are the only
# state the instance carries in public.
PUBLIC_SURFACE = ("events", "now", "order", "random", "ticks")
PUBLIC_STATE = ("events", "now", "random")

# The three references the runtime keeps for itself. Named here so that a
# fourth one appearing — a cache of records, a handle on the journal — fails a
# test instead of quietly widening what a strategy can reach.
PRIVATE_STATE = ("_from_host", "_orders", "_to_host")

# A probe announces a breach with 97, the same code `tests/hostile/` and
# `tests/adversarial/` use: reached only when the thing under attack handed
# over what it should not have. It arrives at the user as one of these two
# lines, depending on whether the host was still listening when the cell died.
CLAIMED_BREACH = 97
BREACH_MARKERS = (f"SystemExit: {CLAIMED_BREACH}", f"status {CLAIMED_BREACH}")

BREACH_MEANS = (
    "a probe exited 97: it reached a journal record stamped later than the "
    "simulated present from inside the cell, which means the future was in "
    "the strategy's process memory and the time gate does not hold"
)


# --- the probes, as the plain text a user would write -----------------------

# `dir()` -> `gc` -> `object.__subclasses__()`, then every dict that came back.
REACHABILITY_PROBE = '''\
"Walk everything reachable from inside the cell, looking for the future."
import gc
import sys
from datetime import datetime

BREACH = 97

# The upstream dialect names its timestamp field per record type. A dict with a
# `type` and either of these is a journal record, wherever it was found.
TIME_FIELDS = ("executed_at", "event_time")

# The subclass graph is a graph, not a tree: without both a visited set and a
# depth bound, a probe written to prove termination would not terminate.
MAX_DEPTH = 8

# The calibration copy of this file rewrites this line to a fabricated record
# dated after the journal ends. The walk has to trip over it: a search that
# cannot find a planted future record is no evidence that there is none.
PLANTED = None


def descendants_of_object():
    "Every class reachable from `object` by repeated __subclasses__()."
    seen = set()
    classes = []
    frontier = [object]
    for _ in range(MAX_DEPTH):
        following = []
        for cls in frontier:
            try:
                children = cls.__subclasses__()
            except TypeError:
                continue
            for child in children:
                if id(child) in seen:
                    continue
                seen.add(id(child))
                classes.append(child)
                following.append(child)
        if not following:
            break
        frontier = following
    return classes


def unpack(value):
    "A value, plus one level of whatever it holds."
    yield value
    if isinstance(value, dict):
        for item in list(value.values()):
            yield item
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value):
            yield item


def reachable(market):
    "Everything a strategy can lay hands on while one tick is on the wire."
    for name in dir(market):
        yield name
        try:
            value = getattr(market, name)
        except Exception:
            continue
        for item in unpack(value):
            yield item
    for item in unpack(vars(market)):
        yield item
    # A snapshot list, so allocating inside this loop cannot disturb it.
    for obj in gc.get_objects():
        yield obj
    for cls in descendants_of_object():
        # vars() on a class is a mappingproxy; copying it makes unpack's dict
        # branch apply, which is where a class-attribute stash would show up.
        for item in unpack(dict(vars(cls))):
            yield item


def walk(market):
    "The first reachable future record, and how many records were reached."
    now = datetime.fromisoformat(market.now)
    reached = 0
    for value in reachable(market):
        if not isinstance(value, dict) or "type" not in value:
            continue
        reached += 1
        for field in TIME_FIELDS:
            stamp = value.get(field)
            if not isinstance(stamp, str):
                continue
            try:
                moment = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if moment > now:
                return value, reached
    return None, reached


def strategy(market):
    if PLANTED is not None:
        # Calibration only, and on the market object on purpose: it puts the
        # planted record on the dir()/vars() paths as well as gc's.
        market.planted = PLANTED
    ticks = 0
    fewest = None
    for tick in market.ticks():
        ticks += 1
        found, reached = walk(tick)
        if found is not None:
            print("FUTURE=%r" % (found,), file=sys.stderr, flush=True)
            raise SystemExit(BREACH)
        if fewest is None or reached < fewest:
            fewest = reached
    print(
        "WALKED ticks=%d reached=%d" % (ticks, fewest or 0),
        file=sys.stderr,
        flush=True,
    )
'''

# The pipe is reachable. Emptiness, not privacy, is what makes it useless.
PIPE_PROBE = '''\
"Read the host pipe directly while a tick is being answered."
import os
import sys

BREACH = 97


def strategy(market):
    # Underscore or not, a strategy can hold the object the runtime reads its
    # ticks from. What it cannot do is find anything in it.
    pipe = market._from_host
    fd = pipe.fileno()
    ticks = 0
    empty = 0
    for tick in market.ticks():
        ticks += 1
        was_blocking = os.get_blocking(fd)
        try:
            os.set_blocking(fd, False)
            try:
                ahead = os.read(fd, 4096)
            except BlockingIOError:
                ahead = b""
        finally:
            # os.dup would share the open file description, and O_NONBLOCK
            # lives on the description rather than on the descriptor -- so a
            # copy would not isolate the flag, and restoring it here is the
            # only thing keeping the next frame read from coming back empty.
            os.set_blocking(fd, was_blocking)
        if ahead:
            print("AHEAD=%r" % (ahead,), file=sys.stderr, flush=True)
            raise SystemExit(BREACH)
        empty += 1
    print(
        "PIPE ticks=%d empty=%d" % (ticks, empty), file=sys.stderr, flush=True
    )
'''

# Remember everything ever shown, and check the pile never outgrows the ticks.
COUNTING_PROBE = '''\
"Count every distinct journal record this strategy is ever shown."
import json
import sys

BREACH = 97


def identity(record):
    "Records are unhashable; their canonical text is not."
    return json.dumps(record, sort_keys=True)


def strategy(market):
    seen = set()
    ticks = 0
    for tick in market.ticks():
        ticks += 1
        for event in tick.events:
            seen.add(identity(event))
        # One tick, one record, from the first tick to the last. More than that
        # at any point would mean the host had sent something it had no turn to
        # send.
        if len(seen) != ticks:
            print(
                "COUNTS records=%d ticks=%d" % (len(seen), ticks),
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(BREACH)
    print("RECORDS=%d" % len(seen), file=sys.stderr, flush=True)
'''

# The same surface assertion the in-process test makes, made where it counts.
SURFACE_PROBE = '''\
"Report the public attribute surface of Market from inside the cell."
import sys


def strategy(market):
    reported = False
    for tick in market.ticks():
        if not reported:
            names = sorted(n for n in dir(tick) if not n.startswith("_"))
            print("SURFACE=" + ",".join(names), file=sys.stderr, flush=True)
            reported = True
'''


# --- the journal the probes are pointed at ----------------------------------


def at_second(second: int) -> str:
    """A sim-time inside one minute, in the upstream dialect's format."""
    return f"2026-07-29T14:30:{second:02d}.000000+00:00"


def probe_records() -> list[dict]:
    """Distinct trades a second apart, salted with the two special records.

    Every record differs from every other, so "how many distinct records has
    this strategy seen" is a question with an unambiguous answer.
    """
    records: list[dict] = [
        journals.trade(
            price=f"118{200 + index}.50", at=at_second(index + 1), sequence=index + 1
        )
        for index in range(TRADE_COUNT)
    ]
    records.insert(2, journals.ERROR)  # a diagnostic: never replayed at all
    records.insert(5, journals.GAP)  # replayed, but carries no time of its own
    return records


# A trade from long after the journal's last record: what a breach would look
# like if the host ever leaked one. Planted in the cell to calibrate the walk.
FUTURE_RECORD = journals.trade(price="119999.00", at=at_second(59), sequence=99)


def sha256_of(path: Path) -> str:
    """The digest the sealed dataset is named by, computed without sbx."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe_file(directory: Path, source: str, name: str) -> Path:
    """Write a probe the CLI will hash and the cell will execute."""
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def only_run() -> dict:
    """The single run record in the ledger."""
    runs = ledger.entries_of("run")
    assert len(runs) == 1
    return runs[0]


def assert_no_breach_was_claimed(result) -> None:
    """The probe's own verdict on itself, in every form it can reach the user.

    97 is the only way a probe can say it succeeded, and it is checked on the
    process exit code *and* in both captured streams: the cell's exit status
    and the runtime's report of a `SystemExit` are two different sentences for
    the same event, and either one arriving is the guarantee coming apart.
    """
    assert result.returncode != CLAIMED_BREACH, BREACH_MEANS
    for marker in BREACH_MARKERS:
        assert marker not in result.stderr, BREACH_MEANS
        assert marker not in result.stdout, BREACH_MEANS


def assert_the_run_completed(result) -> dict:
    """Exit 0, no traceback, and a ledger record saying it ran the data out."""
    assert result.returncode == EXIT_OK
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
    record = only_run()
    assert record["outcome"] == "completed"
    assert record["ticks"] == EXPECTED_TICKS
    return record


@pytest.fixture
def sealed_data(sbx_cli, sbx_home: Path, tmp_path: Path) -> str:
    """Seal the probe journal through the CLI and return its digest."""
    source = journals.write_journal(tmp_path / "market.jsonl", probe_records())
    result = sbx_cli("seal", str(source))
    assert result.returncode == EXIT_OK
    return sha256_of(source)


# ---------------------------------------------------------------------------
# the reachability walk — dir(), vars(), gc, and the subclass closure
# ---------------------------------------------------------------------------


def test_nothing_reachable_from_inside_the_cell_is_stamped_after_now(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """The walk that would find the future, run at every tick, finding none."""
    probe = probe_file(tmp_path, REACHABILITY_PROBE, "strategy.py")

    result = sbx_cli(
        "run", str(probe), "--data", sealed_data, "--seed", str(SEED)
    )

    assert_no_breach_was_claimed(result)
    assert_the_run_completed(result)

    # A probe that reached nothing would pass vacuously, so it reports what it
    # touched: the tick it is answering is itself a journal record, and a walk
    # that cannot even find that one is not evidence of anything.
    walked = re.search(r"WALKED ticks=(\d+) reached=(\d+)", result.stdout)
    assert walked is not None, "the walk never reported; it did not run"
    assert int(walked.group(1)) == EXPECTED_TICKS
    assert int(walked.group(2)) >= 1, (
        "the walk reached no journal record at all on some tick, so finding no "
        "future record there proves nothing about the gate"
    )


def test_the_same_walk_finds_a_future_record_that_is_planted_for_it(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """Calibration for the test above, sharing its walk line for line.

    The previous test says a walk found nothing. That is only worth reading if
    the walk can find something, so here the identical probe is handed one
    record from after the end of the journal and has to come back with it. A
    change that quietly breaks the search fails here first.
    """
    calibrated = REACHABILITY_PROBE.replace(
        "PLANTED = None", f"PLANTED = {FUTURE_RECORD!r}"
    )
    assert calibrated != REACHABILITY_PROBE, "the plant did not take"
    probe = probe_file(tmp_path, calibrated, "strategy.py")

    result = sbx_cli(
        "run", str(probe), "--data", sealed_data, "--seed", str(SEED)
    )

    said = result.stdout + result.stderr
    assert any(marker in said for marker in BREACH_MARKERS), (
        "the walk did not notice a record planted directly on the market "
        "object, so it could not have noticed a real one either"
    )
    assert "Traceback" not in result.stderr
    # The breach is the probe's verdict on the cell, not on sbx: a run that
    # ends in a strategy's SystemExit is still recorded, like any other.
    assert only_run()["outcome"] != "completed"


# ---------------------------------------------------------------------------
# the pipe — empty because nothing has been written to it, not because it is private
# ---------------------------------------------------------------------------


def test_the_host_pipe_holds_nothing_while_a_tick_is_unanswered(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """A non-blocking read of the wire itself comes back empty-handed."""
    probe = probe_file(tmp_path, PIPE_PROBE, "strategy.py")

    result = sbx_cli(
        "run", str(probe), "--data", sealed_data, "--seed", str(SEED)
    )

    assert_no_breach_was_claimed(result)
    # The probe rewrote the flags on the fd the protocol reads from. That the
    # run completed at all is the other half of what this test says: the
    # restore worked, and every remaining tick was still delivered.
    record = assert_the_run_completed(result)
    assert record["fills"] == []

    pipe = re.search(r"PIPE ticks=(\d+) empty=(\d+)", result.stdout)
    assert pipe is not None, "the pipe probe never reported; it did not run"
    assert int(pipe.group(1)) == EXPECTED_TICKS
    assert int(pipe.group(2)) == EXPECTED_TICKS, (
        "a read of the host pipe returned bytes mid-tick, which would be the "
        "next tick sitting in the cell's address space before its turn"
    )


# ---------------------------------------------------------------------------
# counting — one record per tick, from the first tick to the last
# ---------------------------------------------------------------------------


def test_a_strategy_is_never_shown_more_records_than_it_has_had_ticks(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """Everything ever shown, kept and counted, tick by tick."""
    probe = probe_file(tmp_path, COUNTING_PROBE, "strategy.py")

    result = sbx_cli(
        "run", str(probe), "--data", sealed_data, "--seed", str(SEED)
    )

    assert_no_breach_was_claimed(result)
    assert_the_run_completed(result)

    # The `error` record is a diagnostic and is never replayed, so the journal
    # has one more record in it than the strategy will ever be shown.
    assert f"RECORDS={EXPECTED_TICKS}" in result.stdout
    assert len(probe_records()) == EXPECTED_TICKS + 1


# ---------------------------------------------------------------------------
# the surface — five public names, in-process and in the cell
# ---------------------------------------------------------------------------


def test_market_offers_exactly_the_documented_public_names() -> None:
    """No sixth attribute has crept on to the object a strategy holds."""
    market = Market(io.BytesIO(), io.BytesIO(), random.Random(SEED))

    assert tuple(sorted(n for n in dir(market) if not n.startswith("_"))) == (
        PUBLIC_SURFACE
    )
    # And the whole instance dictionary, private names included. `dir()` alone
    # would not notice a cache of records added under an underscore, and an
    # underscore is not a barrier to a strategy that goes looking.
    assert tuple(sorted(vars(market))) == tuple(sorted(PUBLIC_STATE + PRIVATE_STATE))


def test_the_cell_sees_the_same_five_names(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """Asserted where it matters: in the process the strategy runs in.

    The in-process check reads a `Market` built by the test. This one reads the
    one the runtime actually handed to a strategy, in a cell with no
    site-packages and no way to import sbx.
    """
    probe = probe_file(tmp_path, SURFACE_PROBE, "strategy.py")

    result = sbx_cli(
        "run", str(probe), "--data", sealed_data, "--seed", str(SEED)
    )

    assert_the_run_completed(result)
    assert f"SURFACE={','.join(PUBLIC_SURFACE)}" in result.stdout
