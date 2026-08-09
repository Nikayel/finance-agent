"""Milestone 4 — `sbx run`, driven through the console script.

This is the milestone's user-visible edge: a strategy file, a sealed dataset, a
seed, and one line of summary. What these tests pin is not the arithmetic — the
gate and the fill rule are tested where they live — but the four promises the
command makes to whoever types it:

* **A run is evidence.** Exactly one ``run`` record is appended per run, its
  keys are exactly the documented set, and the run then shows up in `sbx ls`.
  A run that happened but left no record is indistinguishable from one that
  never ran.
* **The deterministic part is hashed; the ambient part is not.** Two runs of
  the same strategy, dataset and seed agree on ``result_hash`` and on every
  other field except the run id and the resource numbers, which are recorded
  precisely because they are *not* allowed to influence the hash.
* **A fill comes from the future the strategy has not seen.** Every recorded
  fill lands on a real trade at or after the sim-time the order was placed at,
  and never at the price the ordering tick itself carried.
* **Failure is a message, not a crash.** An unknown dataset, a missing
  strategy, a strategy that raises and a strategy that never yields control all
  exit 1 with one line on stderr and no traceback.

The CLI is exercised as the installed console script throughout: the exit code
and the stderr line are the contract, and calling ``main()`` in-process would
test neither.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import journals

import sbx
from sbx import ledger
from sbx.exits import EXIT_ERROR, EXIT_OK

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

# Canonical decimal text: positional notation, trailing zeros intact, never
# scientific. `pnl`, `position` and every fill number are written this way.
DECIMAL_TEXT = re.compile(r"\A-?\d+(\.\d+)?\Z")

# Everything a run record is allowed to say, and nothing else.
RUN_KEYS = {
    "kind",
    "run_id",
    "data",
    "code",
    "seed",
    "sbx_version",
    "env_fingerprint",
    "outcome",
    "containment",
    # How far the run actually got. In the record *and* in result_hash: a
    # strategy that returns after 200 of 200,000 ticks, stopping on a peak,
    # would otherwise be byte-indistinguishable from one that ran the journal
    # out.
    "ticks",
    "fills",
    "pnl",
    "position",
    "result_hash",
    "utime_us",
    "stime_us",
    "max_rss_bytes",
}

FILL_KEYS = {"at", "side", "size", "price"}

# Measured, recorded, and deliberately outside `result_hash`: identical inputs
# are entitled to differ here and still be the same result.
RESOURCE_KEYS = {"utime_us", "stime_us", "max_rss_bytes"}

SEED = 7
ORDER_SIZE = Decimal("0.001")

TRADE_COUNT = 12


# --- strategies, as the plain text a user would write -----------------------

TRIVIAL_STRATEGY = """\
def strategy(market):
    seen = 0
    for tick in market.ticks():
        seen += len(tick.events)
"""

BUYING_STRATEGY = """\
from decimal import Decimal


def strategy(market):
    placed = False
    for tick in market.ticks():
        if not placed:
            market.order("BUY", Decimal("0.001"))
            placed = True
"""

RAISING_STRATEGY = """\
def strategy(market):
    for tick in market.ticks():
        raise RuntimeError("boom")
"""

# Never advances the iterator again, so it never yields control back to the
# host. Only the cell's caps can end this run.
SPINNING_STRATEGY = """\
def strategy(market):
    for tick in market.ticks():
        while True:
            pass
"""


def at_second(second: int) -> str:
    """A sim-time inside one minute, in the upstream dialect's format."""
    return f"2026-07-29T14:30:{second:02d}.000000+00:00"


def replay_records() -> list[dict]:
    """Trades a second apart, salted with the records replay treats specially.

    Prices are distinct, so a fill can be traced back to exactly one trade.
    """
    records: list[dict] = [
        journals.trade(
            price=f"118{200 + index}.50", at=at_second(index + 1), sequence=index + 1
        )
        for index in range(TRADE_COUNT)
    ]
    records.insert(3, journals.ERROR)  # not an event: skipped entirely
    records.insert(7, journals.GAP)  # an event with no timestamp of its own
    return records


PRICE_AT = {
    record["executed_at"]: record["price"]
    for record in replay_records()
    if record["type"] == "trade"
}

# The first record is a trade, so the first tick's `now` is its timestamp — and
# the price a strategy ordering on that tick has already seen.
FIRST_NOW = at_second(1)
FIRST_PRICE = PRICE_AT[FIRST_NOW]


def moment(text: str) -> datetime:
    """Parse a sim-time, so two of them compare as times rather than as text."""
    return datetime.fromisoformat(text)


def strategy_file(directory: Path, source: str, name: str = "strategy.py") -> Path:
    """Write a strategy the CLI will hash and the cell will execute."""
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def sha256_of(path: Path) -> str:
    """The digest the run record must carry, computed independently of sbx."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def only_run() -> dict:
    """The single run record in the ledger."""
    runs = ledger.entries_of("run")
    assert len(runs) == 1
    return runs[0]


def assert_clean_failure(result) -> None:
    """Exit 1, one line of explanation, and nothing that reads as a crash."""
    assert result.returncode == EXIT_ERROR
    assert result.stderr.strip() != ""
    assert len(result.stderr.strip().splitlines()) == 1
    # A traceback is sbx admitting it did not expect its own failure mode.
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


def assert_contained_run_was_recorded() -> None:
    """A run that started and was stopped is still a run, and still evidence.

    ``outcome`` and ``containment`` exist precisely so a run that did not
    finish can be told apart from one that did. A killed run that left no
    record would be indistinguishable from a run nobody ever started, which is
    the one thing a tamper-evident ledger may not allow.
    """
    record = only_run()
    assert set(record) == RUN_KEYS
    assert record["run_id"] == "run-0001"
    assert record["outcome"] != "completed"
    assert isinstance(record["containment"], list)
    assert HEX64.match(record["result_hash"])


@pytest.fixture
def sealed_data(sbx_cli, sbx_home: Path, tmp_path: Path) -> str:
    """Seal the replay journal through the CLI and return its digest."""
    source = journals.write_journal(tmp_path / "market.jsonl", replay_records())
    result = sbx_cli("seal", str(source))
    assert result.returncode == EXIT_OK
    return sha256_of(source)


# ---------------------------------------------------------------------------
# naming the dataset — a full digest, or enough of one
# ---------------------------------------------------------------------------


def test_a_run_against_a_sealed_dataset_exits_zero(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    result = sbx_cli(
        "run", str(strategy), "--data", sealed_data, "--seed", str(SEED)
    )

    assert result.returncode == EXIT_OK
    assert "Traceback" not in result.stderr
    # "Prints a short human summary" — and a summary that does not name the run
    # leaves the user with nothing to type into `sbx verify`.
    assert result.stdout.strip() != ""
    assert "run-0001" in result.stdout


def test_the_dataset_can_be_named_by_an_unambiguous_prefix(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    result = sbx_cli(
        "run", str(strategy), "--data", sealed_data[:12], "--seed", str(SEED)
    )

    assert result.returncode == EXIT_OK
    # The prefix is a convenience for typing; the record keeps the full digest,
    # which is the only form that stays unambiguous as the store grows.
    assert only_run()["data"] == sealed_data


# ---------------------------------------------------------------------------
# failures — one line, no traceback, exit 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make_args", "mentioned"),
    [
        pytest.param(
            lambda data, strategy: (str(strategy), "--data", "0" * 64),
            "0" * 12,
            id="unknown-dataset",
        ),
        pytest.param(
            lambda data, strategy: (str(strategy.parent / "absent.py"), "--data", data),
            "absent.py",
            id="missing-strategy",
        ),
    ],
)
def test_a_run_that_cannot_start_fails_cleanly(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str, make_args, mentioned: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    result = sbx_cli("run", *make_args(sealed_data, strategy), "--seed", str(SEED))

    assert_clean_failure(result)
    # One line is only enough if it names what was wrong with the invocation.
    assert mentioned in result.stderr
    # Nothing was executed, so there is nothing to attest to.
    assert ledger.entries_of("run") == []


def test_a_strategy_that_raises_fails_cleanly(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, RAISING_STRATEGY)

    result = sbx_cli(
        "run", str(strategy), "--data", sealed_data, "--seed", str(SEED)
    )

    # The strategy's own traceback belongs to the cell; the host reports that
    # the run did not complete and nothing else.
    assert_clean_failure(result)
    assert_contained_run_was_recorded()
    assert sbx_cli("ls").returncode == EXIT_OK


def test_a_strategy_that_never_yields_control_is_contained(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, SPINNING_STRATEGY)

    # If containment fails this never returns and the fixture's own timeout
    # ends the test — which is the correct verdict, loudly.
    result = sbx_cli(
        "run", str(strategy), "--data", sealed_data, "--seed", str(SEED)
    )

    assert_clean_failure(result)
    assert_contained_run_was_recorded()
    # It never got as far as an order, so there is nothing to fill.
    assert only_run()["fills"] == []
    assert sbx_cli("ls").returncode == EXIT_OK


# ---------------------------------------------------------------------------
# the ledger record — one per run, with exactly the documented shape
# ---------------------------------------------------------------------------


def test_exactly_one_run_record_is_appended(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    result = sbx_cli(
        "run", str(strategy), "--data", sealed_data, "--seed", str(SEED)
    )

    assert result.returncode == EXIT_OK
    # The sealing and the run, in that order, and nothing else: a run must not
    # re-record the dataset it read.
    assert [entry["kind"] for entry in ledger.entries()] == ["dataset", "run"]


def test_the_run_record_says_exactly_what_it_is_documented_to_say(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    result = sbx_cli(
        "run", str(strategy), "--data", sealed_data, "--seed", str(SEED)
    )
    assert result.returncode == EXIT_OK

    record = only_run()
    assert set(record) == RUN_KEYS

    assert record["kind"] == "run"
    assert record["run_id"] == "run-0001"
    assert record["data"] == sealed_data
    # The code hash names the exact source text that ran, so a strategy edited
    # after the fact cannot claim this result.
    assert record["code"] == sha256_of(strategy)
    assert record["seed"] == SEED
    assert isinstance(record["seed"], int)
    assert record["sbx_version"] == sbx.__version__
    assert isinstance(record["env_fingerprint"], str)
    assert record["env_fingerprint"] != ""
    assert record["outcome"] == "completed"
    assert isinstance(record["containment"], list)
    assert all(isinstance(mechanism, str) for mechanism in record["containment"])
    assert record["fills"] == []  # this strategy never orders
    assert DECIMAL_TEXT.match(record["pnl"])
    assert DECIMAL_TEXT.match(record["position"])
    assert Decimal(record["position"]) == 0
    assert HEX64.match(record["result_hash"])
    for key in sorted(RESOURCE_KEYS):
        assert isinstance(record[key], int)
        assert record[key] >= 0


def test_run_ids_are_allocated_in_ledger_order(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)
    arguments = ("run", str(strategy), "--data", sealed_data, "--seed", str(SEED))

    first = sbx_cli(*arguments)
    second = sbx_cli(*arguments)

    assert first.returncode == second.returncode == EXIT_OK
    assert [entry["run_id"] for entry in ledger.entries_of("run")] == [
        "run-0001",
        "run-0002",
    ]


# ---------------------------------------------------------------------------
# determinism — the same inputs, the same result, whatever the machine did
# ---------------------------------------------------------------------------


def test_identical_inputs_produce_an_identical_result_hash(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, BUYING_STRATEGY)
    arguments = ("run", str(strategy), "--data", sealed_data, "--seed", str(SEED))

    assert sbx_cli(*arguments).returncode == EXIT_OK
    assert sbx_cli(*arguments).returncode == EXIT_OK

    first, second = ledger.entries_of("run")
    assert HEX64.match(first["result_hash"])
    assert first["result_hash"] == second["result_hash"]

    # The other half of the claim: the timings and the resident size are still
    # recorded, and are free to differ between the two runs without making them
    # different results.
    for key in sorted(RESOURCE_KEYS):
        assert isinstance(first[key], int)
        assert isinstance(second[key], int)

    for key in sorted(RUN_KEYS - RESOURCE_KEYS - {"run_id"}):
        assert first[key] == second[key], key


def test_a_different_seed_is_recorded_as_its_own_run(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)

    for seed in (SEED, SEED + 1):
        result = sbx_cli(
            "run", str(strategy), "--data", sealed_data, "--seed", str(seed)
        )
        assert result.returncode == EXIT_OK

    runs = ledger.entries_of("run")
    assert [entry["seed"] for entry in runs] == [SEED, SEED + 1]
    assert [entry["run_id"] for entry in runs] == ["run-0001", "run-0002"]
    # Whether the two hashes agree is a question about *this* strategy, which
    # ignores the seed entirely. Asserting either way would pin a property of
    # the fixture rather than of sbx, so only well-formedness is claimed.
    assert all(HEX64.match(entry["result_hash"]) for entry in runs)


# ---------------------------------------------------------------------------
# fills — priced from data the strategy had not been shown
# ---------------------------------------------------------------------------


def test_a_buying_strategy_fills_at_a_later_trade(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, BUYING_STRATEGY)

    result = sbx_cli(
        "run", str(strategy), "--data", sealed_data, "--seed", str(SEED)
    )
    assert result.returncode == EXIT_OK

    record = only_run()
    fills = record["fills"]
    assert fills != []

    for fill in fills:
        assert set(fill) == FILL_KEYS
        assert fill["side"] == "BUY"
        assert DECIMAL_TEXT.match(fill["size"])
        assert DECIMAL_TEXT.match(fill["price"])
        assert Decimal(fill["size"]) == ORDER_SIZE
        # The order went in on the first tick, so no fill may be dated before
        # it — an earlier fill would be a trade the strategy never lived to see.
        assert moment(fill["at"]) >= moment(FIRST_NOW)
        # And the price has to belong to the trade it claims to have filled on.
        assert fill["price"] == PRICE_AT[fill["at"]]
        # The ordering tick's own price was already on the strategy's screen;
        # filling there would be the look-ahead this milestone forbids.
        assert fill["price"] != FIRST_PRICE

    assert Decimal(record["position"]) == ORDER_SIZE * len(fills)


# ---------------------------------------------------------------------------
# ls — the run is visible from the outside
# ---------------------------------------------------------------------------


def test_a_completed_run_appears_under_runs_in_ls(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, TRIVIAL_STRATEGY)
    run = sbx_cli("run", str(strategy), "--data", sealed_data, "--seed", str(SEED))
    assert run.returncode == EXIT_OK

    result = sbx_cli("ls")

    assert result.returncode == EXIT_OK
    _datasets, marker, runs = result.stdout.partition("RUNS")
    assert marker == "RUNS"
    assert "run-0001" in runs
    assert "(none)" not in runs
