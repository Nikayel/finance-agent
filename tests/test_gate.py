"""Milestone 4 — the time gate: the host owns the clock.

`gate.replay` is the only thing in sbx that decides *when* a strategy learns
something. Everything else in the milestone — the pipe, the cell, the fill
model — exists to carry that decision across a process boundary intact, so the
rules pinned here are the ones the product is actually sold on:

* **A tick never reveals an event stamped later than its own sim-time.** This
  is look-ahead, stated as an invariant rather than as a policy, and it is
  asserted over a few hundred generated records rather than a hand-picked
  three — a peek that only happens on one shape of journal is still a peek.
* **Sim-time never runs backwards.** Exchange clocks are not monotonic, so a
  record arriving later can carry an earlier stamp. The clock is the running
  maximum, which keeps the first rule true without dropping or reordering the
  late record itself.

The two rules pull against each other, and that tension is the whole design:
clamping the clock forward is what makes an out-of-order arrival safe to
reveal, because after the clamp the stale event is in the tick's past.

Records here always come from `journals.py` — the upstream dialect, whose
timestamp *field name* differs per type and is absent from two of them, is
half of what the gate has to get right, and a hand-rolled record would quietly
test a tidier format than the one that exists.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import random
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

import journals
from sbx import gate
from sbx.errors import SbxError

# The clock these tests measure everything against: the dialect's own sample
# trade, so a stamp in a failure message is recognisably a journals.py stamp.
BASE = datetime.fromisoformat(journals.TRADE["executed_at"])

# Big enough that a lucky ordering cannot pass for a correct implementation.
RECORD_COUNT = 400

# The types that are bookkeeping rather than market events. `journals.py` has
# a constant for `error` but not for `raw`, so the raw record below is derived
# from the nearest thing rather than invented: only its `type` reaches the
# gate's decision, which drops it before reading any other field.
NOT_EVENTS = ("error", "raw")

RAW = {**journals.ERROR, "type": "raw"}

# Far past anything a lazy reader touches, and short enough that a reader that
# is *not* lazy fails instead of hanging the suite. See endless_trades.
LAZINESS_LIMIT = 1000


def at(offset_ms: int) -> str:
    """A dialect-shaped timestamp `offset_ms` from the base instant."""
    return (BASE + timedelta(milliseconds=offset_ms)).isoformat(timespec="microseconds")


def price_of(sequence: int) -> str:
    """A distinct price per record, so a misplaced event is visible by value."""
    return f"{118200 + sequence % 7}.{sequence % 100:02d}"


def trade_at(offset_ms: int, sequence: int) -> dict[str, Any]:
    return journals.trade(price=price_of(sequence), at=at(offset_ms), sequence=sequence)


def book_update_at(offset_ms: int, sequence: int) -> dict[str, Any]:
    """A book update at a chosen time.

    `journals.py` exposes a constructor for trades only, so the one book-update
    constant is varied in place — the fields that matter to the gate are the
    type and `event_time`.
    """
    return {
        **journals.BOOK_UPDATE,
        "event_time": at(offset_ms),
        "sequence_num": sequence,
    }


def without(record: dict[str, Any], field: str) -> dict[str, Any]:
    """A copy of a record with one field missing, as a truncated feed emits it."""
    return {key: value for key, value in record.items() if key != field}


def stamp_of(event: dict[str, Any]) -> datetime | None:
    """The event's own exchange timestamp, read under its own type's field name.

    Returns None for the two types that carry no time of their own — they are
    the reason a single "timestamp" column does not exist upstream.
    """
    field = journals.TIMESTAMP_FIELDS.get(event["type"])
    return None if field is None else datetime.fromisoformat(event[field])


def replayed(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records a correct gate reveals, in the order it must reveal them."""
    return [record for record in records if record["type"] not in NOT_EVENTS]


def wandering_trades(count: int, *, seed: int) -> list[dict[str, Any]]:
    """Trades whose stamps trend forward but arrive out of order.

    The jitter is deliberately wider than the step, which is what a feed
    stitched from two exchange gateways looks like: mostly ordered, never
    sorted. A journal that merely rose would prove nothing about the clamp.
    """
    rng = random.Random(seed)
    return [
        trade_at(sequence * 40 + rng.randint(-900, 900), sequence)
        for sequence in range(1, count + 1)
    ]


def wandering_journal(count: int, *, seed: int) -> list[dict[str, Any]]:
    """A disordered journal of every type, including the ones with no clock."""
    rng = random.Random(seed)
    records: list[dict[str, Any]] = []
    for sequence in range(1, count + 1):
        offset = sequence * 40 + rng.randint(-900, 900)
        roll = rng.random()
        if roll < 0.50:
            records.append(trade_at(offset, sequence))
        elif roll < 0.80:
            records.append(book_update_at(offset, sequence))
        elif roll < 0.88:
            records.append(journals.GAP)
        elif roll < 0.94:
            records.append(journals.DISCONNECT)
        else:
            records.append(journals.ERROR)
    return records


def endless_trades(produced: list[int]) -> Iterator[dict[str, Any]]:
    """Trades without end, as far as any lazy reader can tell.

    A genuinely infinite generator would *hang* the suite against an
    implementation that materialises its input, and a wrong implementation has
    to fail rather than never finish — so this one raises well past the point
    where laziness has already been proved or disproved.
    """
    for sequence in itertools.count(1):
        if sequence > LAZINESS_LIMIT:
            raise RuntimeError("replay read the whole journal before yielding a tick")
        produced.append(sequence)
        yield trade_at(sequence * 250, sequence)


# ---------------------------------------------------------------------------
# Tick — the shape of a revealed moment
# ---------------------------------------------------------------------------


def test_tick_is_a_dataclass_of_now_then_events() -> None:
    assert dataclasses.is_dataclass(gate.Tick)
    assert [field.name for field in dataclasses.fields(gate.Tick)] == ["now", "events"]


def test_a_tick_is_a_str_now_and_a_tuple_of_event_dicts() -> None:
    for tick in gate.replay(journals.sample_records()):
        assert isinstance(tick, gate.Tick)
        assert isinstance(tick.now, str)
        # A tuple, not a list: what the host hands over is a reading, not a
        # buffer the strategy can append its own guesses to.
        assert isinstance(tick.events, tuple)
        assert all(isinstance(event, dict) for event in tick.events)


def test_a_tick_cannot_be_edited_after_it_is_handed_over() -> None:
    [tick] = gate.replay([trade_at(0, 1)])

    with pytest.raises(dataclasses.FrozenInstanceError):
        tick.now = at(60_000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tick.events = ()


# ---------------------------------------------------------------------------
# what counts as an event — and what is merely bookkeeping
# ---------------------------------------------------------------------------


def test_an_empty_journal_yields_no_ticks() -> None:
    assert list(gate.replay([])) == []


def test_error_records_are_not_events() -> None:
    records = [trade_at(0, 1), journals.ERROR, trade_at(1000, 2)]

    ticks = list(gate.replay(records))

    assert len(ticks) == 2
    assert [event["type"] for tick in ticks for event in tick.events] == [
        "trade",
        "trade",
    ]


def test_raw_records_are_not_events() -> None:
    records = [trade_at(0, 1), RAW, trade_at(1000, 2)]

    ticks = list(gate.replay(records))

    assert len(ticks) == 2
    assert [event["type"] for tick in ticks for event in tick.events] == [
        "trade",
        "trade",
    ]


def test_a_journal_of_nothing_but_bookkeeping_yields_no_ticks() -> None:
    assert list(gate.replay([journals.ERROR, journals.ERROR])) == []
    assert list(gate.replay([RAW, RAW])) == []


def test_skipped_records_do_not_touch_the_clock() -> None:
    # An error record carries a `received_at` later than everything around it.
    # Reading it would drag sim-time forward and reveal the next trade early.
    assert stamp_of(journals.ERROR) > stamp_of(trade_at(1000, 2))
    records = [trade_at(0, 1), journals.ERROR, trade_at(1000, 2)]

    ticks = list(gate.replay(records))

    assert [tick.now for tick in ticks] == [at(0), at(1000)]


def test_one_tick_per_replayed_record_each_holding_that_one_event() -> None:
    records = journals.sample_records()

    ticks = list(gate.replay(records))

    assert len(ticks) == len(replayed(records))
    assert all(len(tick.events) == 1 for tick in ticks)


def test_every_other_type_is_replayed_in_file_order() -> None:
    records = journals.sample_records()

    ticks = list(gate.replay(records))

    assert [tick.events[0]["type"] for tick in ticks] == [
        record["type"] for record in replayed(records)
    ]


def test_events_are_the_records_themselves() -> None:
    # The gate decides *when*, not *what*: a record is revealed unchanged, so
    # a strategy sees the same fields the exchange sent.
    records = journals.sample_records()

    revealed = [event for tick in gate.replay(records) for event in tick.events]

    assert revealed == replayed(records)


def test_replay_does_not_disturb_the_journal_it_was_given() -> None:
    records = journals.sample_records()
    before = copy.deepcopy(records)

    list(gate.replay(records))

    assert records == before


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(list, id="list"),
        pytest.param(tuple, id="tuple"),
        pytest.param(iter, id="iterator"),
        pytest.param(lambda records: (r for r in records), id="generator"),
    ],
)
def test_replay_accepts_any_iterable_of_records(wrap: Any) -> None:
    records = journals.sample_records()

    ticks = list(gate.replay(wrap(records)))

    assert [tick.events[0]["type"] for tick in ticks] == [
        record["type"] for record in replayed(records)
    ]


# ---------------------------------------------------------------------------
# sim-time — read from each type's own field, or inherited when there is none
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offset_ms",
    [
        pytest.param(0, id="sub-second"),
        pytest.param(750, id="whole-second"),
        pytest.param(59_750, id="whole-minute"),
    ],
)
def test_a_trades_sim_time_is_its_own_executed_at(offset_ms: int) -> None:
    record = trade_at(offset_ms, 1)

    [tick] = gate.replay([record])

    # Passed through as text, not re-rendered. Re-rendering looks harmless
    # until a stamp lands on a whole second: `datetime.isoformat` drops the
    # microseconds the journal wrote, so the same instant reaches the ledger
    # as two different strings — and `result_hash` is over the strings.
    assert tick.now == record["executed_at"]


def test_a_book_updates_sim_time_is_its_own_event_time() -> None:
    record = book_update_at(0, 1)

    [tick] = gate.replay([record])

    assert tick.now == record["event_time"]


def test_sim_time_follows_the_records_forward() -> None:
    records = [trade_at(0, 1), trade_at(1500, 2), trade_at(4000, 3)]

    ticks = list(gate.replay(records))

    assert [tick.now for tick in ticks] == [at(0), at(1500), at(4000)]


def test_the_two_timestamp_fields_are_read_per_type_not_whichever_is_present() -> None:
    # A trade and a book update in the same journal, each carrying only its own
    # type's field. A gate that guessed "the first field that looks like a time"
    # would still pass a single-type journal.
    records = [trade_at(0, 1), book_update_at(2000, 2), trade_at(5000, 3)]

    ticks = list(gate.replay(records))

    assert [tick.now for tick in ticks] == [at(0), at(2000), at(5000)]


def test_gap_and_disconnect_inherit_the_current_sim_time() -> None:
    records = [trade_at(0, 1), journals.GAP, journals.DISCONNECT, trade_at(2000, 4)]

    ticks = list(gate.replay(records))

    assert [tick.events[0]["type"] for tick in ticks] == [
        "trade",
        "gap",
        "disconnect",
        "trade",
    ]
    # They are events — they get a tick — but they carry no time, so they must
    # not nudge the clock in either direction.
    assert [tick.now for tick in ticks] == [at(0), at(0), at(0), at(2000)]


def test_a_run_of_untimed_records_holds_the_clock_still() -> None:
    records = [trade_at(0, 1), *[journals.GAP] * 5, *[journals.DISCONNECT] * 5]

    ticks = list(gate.replay(records))

    assert len(ticks) == len(records)
    assert {tick.now for tick in ticks} == {at(0)}


def test_an_untimed_record_before_any_clock_still_yields_a_tick() -> None:
    """A leading gap has no sim-time to inherit.

    What the clock *reads* before the first timestamped record is genuinely
    unspecified, so nothing is asserted about the value beyond it being a
    string that has not already run ahead of the trade behind it. That the
    tick exists at all is not unspecified: one tick per replayed record, and a
    `disconnect` at the head of a journal — the feed was already down when the
    session opened — is exactly the event a strategy must not miss.
    """
    ticks = list(gate.replay([journals.GAP, trade_at(0, 2)]))

    assert len(ticks) == 2
    assert ticks[0].events[0]["type"] == "gap"
    assert isinstance(ticks[0].now, str)
    assert ticks[0].now <= ticks[1].now


# ---------------------------------------------------------------------------
# the monotonic clamp — a stale arrival never rewinds the clock
# ---------------------------------------------------------------------------


def test_a_record_stamped_in_the_past_does_not_rewind_sim_time() -> None:
    stale = trade_at(-2500, 3)  # arrives third, stamped before the first
    records = [trade_at(0, 1), trade_at(3000, 2), stale]

    ticks = list(gate.replay(records))

    assert [tick.now for tick in ticks] == [at(0), at(3000), at(3000)]


def test_a_stale_record_is_still_revealed_where_it_arrived() -> None:
    # The clamp moves the *clock*, never the record: dropping or resorting the
    # late arrival would be a different, silently lossy product.
    stale = trade_at(-2500, 3)
    records = [trade_at(0, 1), trade_at(3000, 2), stale, trade_at(3500, 4)]

    ticks = list(gate.replay(records))

    assert [tick.events[0]["sequence_num"] for tick in ticks] == [1, 2, 3, 4]
    assert ticks[2].events[0]["price"] == stale["price"]
    assert stamp_of(ticks[2].events[0]) < datetime.fromisoformat(ticks[2].now)


def test_the_clamp_holds_across_types() -> None:
    records = [trade_at(4000, 1), book_update_at(1000, 2), trade_at(2000, 3)]

    ticks = list(gate.replay(records))

    assert [tick.now for tick in ticks] == [at(4000), at(4000), at(4000)]


def test_untimed_records_do_not_release_the_clamp() -> None:
    records = [trade_at(3000, 1), journals.GAP, trade_at(500, 3), journals.DISCONNECT]

    ticks = list(gate.replay(records))

    assert [tick.now for tick in ticks] == [at(3000)] * 4


def test_sim_time_is_the_running_maximum_over_a_disordered_journal() -> None:
    records = wandering_trades(RECORD_COUNT, seed=20260729)
    stamps = [stamp_of(record) for record in records]
    # Guard against a fixture that quietly stopped exercising the case.
    assert any(later < earlier for earlier, later in zip(stamps, stamps[1:]))

    nows = [datetime.fromisoformat(tick.now) for tick in gate.replay(records)]

    assert len(nows) == len(records)
    assert nows == sorted(nows)  # never backwards, stated plainly
    assert nows == list(itertools.accumulate(stamps, max))  # and stated exactly
    assert nows[0] == stamps[0]
    assert nows[-1] == max(stamps)


def test_sim_time_never_goes_backwards_across_every_record_type() -> None:
    records = wandering_journal(RECORD_COUNT, seed=4242)

    nows = [datetime.fromisoformat(tick.now) for tick in gate.replay(records)]

    assert len(nows) == len(replayed(records))
    assert nows == sorted(nows)


# ---------------------------------------------------------------------------
# the invariant the product is sold on — no tick reveals its own future
# ---------------------------------------------------------------------------


def test_no_tick_ever_reveals_an_event_stamped_after_its_sim_time() -> None:
    records = wandering_trades(RECORD_COUNT, seed=987654321)

    ticks = list(gate.replay(records))

    assert len(ticks) == len(records)
    for index, tick in enumerate(ticks):
        now = datetime.fromisoformat(tick.now)
        assert tick.events, f"tick {index} revealed nothing"
        for event in tick.events:
            assert stamp_of(event) <= now, (
                f"tick {index} at {tick.now} revealed an event from "
                f"{event['executed_at']}"
            )


def test_the_invariant_holds_over_a_journal_of_every_type() -> None:
    records = wandering_journal(RECORD_COUNT, seed=31337)

    ticks = list(gate.replay(records))

    assert len(ticks) == len(replayed(records))
    for index, tick in enumerate(ticks):
        now = datetime.fromisoformat(tick.now)
        for event in tick.events:
            stamp = stamp_of(event)
            if stamp is not None:
                assert stamp <= now, f"tick {index} at {tick.now} revealed {event}"


def test_no_record_is_revealed_before_the_tick_it_arrived_at() -> None:
    # The other half of the invariant: withholding the future is worthless if
    # the reader shuffles the past, so file order is asserted end to end.
    records = wandering_trades(RECORD_COUNT, seed=555)

    ticks = list(gate.replay(records))

    revealed = [event for tick in ticks for event in tick.events]
    assert [event["sequence_num"] for event in revealed] == [
        record["sequence_num"] for record in records
    ]
    assert [event["price"] for event in revealed] == [
        record["price"] for record in records
    ]


def test_every_tick_reveals_exactly_one_record() -> None:
    records = wandering_journal(RECORD_COUNT, seed=8080)

    ticks = list(gate.replay(records))

    revealed = [event for tick in ticks for event in tick.events]
    assert all(len(tick.events) == 1 for tick in ticks)
    assert len(revealed) == len(replayed(records))


# ---------------------------------------------------------------------------
# failures — a journal the gate cannot place in time is not replayed at all
# ---------------------------------------------------------------------------

BAD_TYPES = [
    pytest.param("quote", id="unknown"),
    pytest.param("Trade", id="wrong-case"),
    pytest.param("trades", id="near-miss-plural"),
    pytest.param("trade ", id="trailing-space"),
    pytest.param("", id="empty"),
    pytest.param(None, id="null"),
    pytest.param(1, id="int"),
    pytest.param(True, id="bool"),
    pytest.param(["trade"], id="list"),
]


@pytest.mark.parametrize("value", BAD_TYPES)
def test_an_unknown_record_type_is_refused(value: Any) -> None:
    with pytest.raises(SbxError):
        list(gate.replay([{**journals.TRADE, "type": value}]))


def test_a_record_with_no_type_at_all_is_refused() -> None:
    with pytest.raises(SbxError):
        list(gate.replay([without(journals.TRADE, "type")]))


@pytest.mark.parametrize(
    "record,field",
    [
        pytest.param(journals.TRADE, "executed_at", id="trade"),
        pytest.param(journals.BOOK_UPDATE, "event_time", id="book-update"),
    ],
)
def test_a_timestamped_record_without_its_timestamp_is_refused(
    record: dict[str, Any], field: str
) -> None:
    with pytest.raises(SbxError):
        list(gate.replay([without(record, field)]))


def test_a_trade_is_not_rescued_by_another_types_timestamp_field() -> None:
    # Having *a* time is not having *its* time: guessing here would place the
    # trade at a book update's instant and quietly bend the clock.
    impostor = {
        **without(journals.TRADE, "executed_at"),
        "event_time": journals.BOOK_UPDATE["event_time"],
    }

    with pytest.raises(SbxError):
        list(gate.replay([impostor]))


def test_a_bad_record_late_in_the_journal_still_raises() -> None:
    records = [trade_at(0, 1), trade_at(1000, 2), {**journals.TRADE, "type": "quote"}]

    with pytest.raises(SbxError):
        list(gate.replay(records))


def test_the_ticks_before_a_bad_record_are_delivered_before_the_failure() -> None:
    # Replay is a stream, so the failure arrives where the bad record is, not
    # in a validation pass the caller never asked for.
    records = [trade_at(0, 1), without(journals.TRADE, "executed_at"), trade_at(9, 3)]
    ticks = gate.replay(records)

    assert next(ticks).events[0]["sequence_num"] == 1
    with pytest.raises(SbxError):
        next(ticks)


@pytest.mark.parametrize(
    "records,mentioned",
    [
        pytest.param([{**journals.TRADE, "type": "quote"}], "quote", id="unknown-type"),
        pytest.param(
            [without(journals.TRADE, "executed_at")], "executed_at", id="no-trade-time"
        ),
        pytest.param(
            [without(journals.BOOK_UPDATE, "event_time")], "event_time", id="no-book-time"
        ),
    ],
)
def test_a_refusal_says_what_it_could_not_place(
    records: list[dict[str, Any]], mentioned: str
) -> None:
    with pytest.raises(SbxError) as caught:
        list(gate.replay(records))

    message = str(caught.value)
    assert message.strip() != ""
    assert mentioned in message


# ---------------------------------------------------------------------------
# laziness — the journal is a stream, and the future is not in memory
# ---------------------------------------------------------------------------


def test_replay_returns_an_iterator_and_reads_nothing_until_asked() -> None:
    produced: list[int] = []

    ticks = gate.replay(endless_trades(produced))

    assert iter(ticks) is ticks
    assert produced == []


def test_five_ticks_can_be_taken_from_an_endless_journal() -> None:
    produced: list[int] = []

    ticks = list(itertools.islice(gate.replay(endless_trades(produced)), 5))

    assert len(ticks) == 5
    assert [tick.events[0]["sequence_num"] for tick in ticks] == [1, 2, 3, 4, 5]
    # Instants, not text: how a stamp is rendered is pinned elsewhere, and this
    # test is about what replay reads, not about how it prints.
    assert [datetime.fromisoformat(tick.now) for tick in ticks] == [
        BASE + timedelta(milliseconds=250 * n) for n in range(1, 6)
    ]
    # At most one record of read-ahead. Materialising the journal is not merely
    # wasteful: the future would then exist in the host's memory during the
    # tick that must not know about it.
    assert len(produced) <= 6


def test_ticks_are_produced_one_at_a_time_as_they_are_pulled() -> None:
    produced: list[int] = []
    ticks = gate.replay(endless_trades(produced))

    for expected in range(1, 4):
        tick = next(ticks)
        assert tick.events[0]["sequence_num"] == expected
        assert len(produced) <= expected + 1
