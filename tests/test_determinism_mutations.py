"""Milestone 6, the other half — unpin each source and watch the result move.

``test_determinism.py`` pins four decisions and shows that a run holds still.
Holding still is not evidence on its own: a ``result_hash`` that never moved
because the strategy never depended on anything would satisfy every assertion
in that file and prove nothing at all. This file supplies the missing half for
the four sources the design names — hash randomisation, set and dict iteration
order, float accumulation order, and locale — by reintroducing each one and
showing that the divergence it causes is real.

Nothing here mocks anything. sbx has no knob for unsetting ``PYTHONHASHSEED``
or ``LC_ALL`` — a config surface that only tests use is still a config surface
— so each mutation is demonstrated as a **contrast** instead. The same source
text is evaluated twice: once in a bare interpreter nobody pinned, where it
comes out differently every time, and once inside the cell, where it does not.
A test that reached in and patched the cell's environment would be testing the
patch.

The locale mutation is the one that stops a step short, and says so where it
does: the unpinned half runs in a bare interpreter rather than in a cell,
because there is no supported way to start a cell in another locale. What the
cell half proves is the next best thing — that the locale the *host* was
started in reaches the strategy nowhere.

Float accumulation is different in kind, and earns a stronger claim than
"detected": the drift cannot reach a hash at all. Three refusals are exercised
end to end — the runtime turns down a ``float`` size, the host turns down the
fifty-one decimal places a float's exact value carries when it is laundered
through ``Decimal``, and the encoder turns down a float at any nesting depth.
What the ledger *can* be shown is the drift itself, in the one form that does
get through: two strategies adding the same three numbers in opposite orders
record different positions, and their ``Decimal`` equivalents record one.

The harness — the journal, the seal, the repeated runs — comes from
``harness.py``, the same one ``test_determinism`` uses. The two files are a
pair, and a contrast run through a lookalike harness would be a weaker claim
than one run through the same harness.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from sbx import canonical, ledger
from sbx.errors import NotCanonicalError
from sbx.exits import EXIT_ERROR, EXIT_OK
from harness import (
    FLOAT_TENTHS,
    SEED,
    hashes_of,
    positions_of,
    run_many,
    strategy_file,
)

# Fresh interpreters for the unpinned half of each contrast. Three would almost
# certainly do; five costs a fifth of a second and leaves nothing to argue
# about when the assertion is "these did not all agree".
UNPINNED_REPEATS = 5

# Runs of the cell for the pinned half. Three separate interpreters is enough
# to tell "the same every time" from "the same twice by luck", and every extra
# one is another full replay of the journal.
CELL_REPEATS = 3

# Not C, and not any machine's default. Everything claimed about it is claimed
# by running it: a machine without it skips rather than pretends.
OTHER_LOCALE = "de_DE.UTF-8"

# The C locale's answers, which are what the cell must give whatever the host
# was started in. `weekday` is the vivid one — it is a word, and it changes
# language — while `point` is the one that ruins a ledger quietly.
PINNED_LOCALE = "LC_ALL=C|LANG=C"
PINNED_POINT = "point=."
PINNED_WEEKDAY = "weekday=Thursday"

# Three tenths and their neighbours, added one way and then the other. Adding
# downwards lands on the exact answer, which is precisely why order dependence
# is so hard to notice: half the time it is not wrong.
PARTS_UP = "0.1, 0.2, 0.3"
PARTS_DOWN = "0.3, 0.2, 0.1"
EXACT_PARTS_UP = 'Decimal("0.1"), Decimal("0.2"), Decimal("0.3")'
EXACT_PARTS_DOWN = 'Decimal("0.3"), Decimal("0.2"), Decimal("0.1")'

FLOAT_SUM_UP = "0.6000000000000001"
FLOAT_SUM_DOWN = "0.6"
EXACT_SUM = "0.6"

# What `Decimal(0.1 + 0.2 + 0.3)` actually holds: not six tenths but the float's
# exact binary value, fifty-one places of accumulation debris written out.
LAUNDERED_PLACES = 51


# --- the expressions under test, shared by both halves of each contrast ------

# A fingerprint of the two things that move when the hash salt moves: the order
# a set of strings iterates in, and the raw hash values themselves.
FINGERPRINT_SOURCE = """\
keys = tuple("key-%04d" % number for number in range(500))
walked = "|".join(set(keys))
fingerprint = abs(hash(walked)) + sum(abs(hash(key)) for key in keys)
"""

FINGERPRINT_PROGRAM = FINGERPRINT_SOURCE + "print(fingerprint)\n"

SALTED_STRATEGY = (
    FINGERPRINT_SOURCE
    + '''
from decimal import Decimal


def strategy(market):
    placed = False
    for tick in market.ticks():
        if not placed:
            size = Decimal(fingerprint % 100000 + 1) * Decimal("0.00001")
            market.order("BUY", size)
            placed = True
'''
)

# The set and the dict walked side by side, so the choice of instrument is
# something this file demonstrates rather than something it asserts in prose.
INSTRUMENT_PROGRAM = """\
keys = tuple("key-%04d" % number for number in range(500))
print("|".join(set(keys)))
print("|".join(dict.fromkeys(keys)))
"""

# `setlocale(LC_ALL, "")` is the mutation itself: it is how code asks for the
# *ambient* locale rather than the one it was started in, and it is what
# anything formatting a number "for the user" ends up calling.
LOCALE_SOURCE = """\
import locale
import os
import sys
import time

locale.setlocale(locale.LC_ALL, "")

facts = "|".join(
    [
        "LC_ALL=" + str(os.environ.get("LC_ALL")),
        "LANG=" + str(os.environ.get("LANG")),
        "point=" + locale.localeconv()["decimal_point"],
        "encoding=" + locale.getpreferredencoding(False),
        "filesystem=" + sys.getfilesystemencoding(),
        "weekday=" + time.strftime("%A", time.gmtime(0)),
    ]
)
"""

LOCALE_PROGRAM = LOCALE_SOURCE + "print(facts)\n"

LOCALE_STRATEGY = (
    LOCALE_SOURCE
    + '''
from decimal import Decimal


def strategy(market):
    # The cell points a strategy's stdout at stderr and `sbx run` prints what
    # it said, so this line arrives back through the CLI's own report.
    print(facts)
    placed = False
    for tick in market.ticks():
        if not placed:
            # Folded into the size as well as printed: a locale that moved
            # would then move `result_hash`, not merely one line of output.
            total = sum(ord(letter) for letter in facts)
            market.order("BUY", Decimal(total) * Decimal("0.00001"))
            placed = True
'''
)

# One summing strategy for four runs. The float and Decimal versions differ in
# exactly one thing — the type of the three numbers — so nothing else can be
# blamed for the difference in what they record. `str()` of a float is its
# shortest round-tripping form, so the order-dependent tail survives into the
# ledger instead of being rounded away on the way there.
SUM_STRATEGY = """\
from decimal import Decimal

PARTS = (__PARTS__,)


def strategy(market):
    total = PARTS[0]
    for part in PARTS[1:]:
        total += part
    placed = False
    for tick in market.ticks():
        if not placed:
            market.order("BUY", Decimal(str(total)))
            placed = True
"""

FLOAT_SIZE_STRATEGY = """\
def strategy(market):
    for tick in market.ticks():
        market.order("BUY", 0.1)
"""

LAUNDERED_FLOAT_STRATEGY = """\
from decimal import Decimal


def strategy(market):
    for tick in market.ticks():
        market.order("BUY", Decimal(0.1 + 0.2 + 0.3))
"""


# --- helpers ----------------------------------------------------------------


def summing(parts: str) -> str:
    """The summing strategy over three named numbers.

    Substituted by sentinel rather than by ``%`` or ``format``: the template is
    Python source, and both of those would fight with it.
    """
    return SUM_STRATEGY.replace("__PARTS__", parts)


def bare_python(program: str, **environment: str) -> str:
    """Run `program` in a fresh interpreter nobody pinned; return its stdout.

    The binary is this interpreter's, because the binary is not the variable
    under test — the environment it starts in is. Everything sbx pins is named
    here explicitly rather than inherited, so each contrast is between two
    stated environments and not between two moods of the machine.
    """
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **environment},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def run_once(
    sbx_cli, strategy: Path, data: str, **kwargs: object
) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Run one tuple and return both what the CLI said and what it recorded.

    ``run_many``, in the file this one mutates, returns records alone. Several
    claims here are about what the CLI *printed* — the strategy's own output,
    and the reason a run failed — so both halves come back.
    """
    before = len(ledger.entries_of("run"))
    result = sbx_cli(
        "run", str(strategy), "--data", data, "--seed", str(SEED), **kwargs
    )
    runs = ledger.entries_of("run")
    assert len(runs) == before + 1
    return result, runs[-1]


def summed(sbx_cli, tmp_path: Path, data: str, name: str, parts: str) -> dict:
    """Run the summing strategy over `parts` and return its ledger record."""
    strategy = strategy_file(tmp_path, summing(parts), f"{name}.py")
    result, record = run_once(sbx_cli, strategy, data)
    assert result.returncode == EXIT_OK, result.stderr
    return record


def reported_facts(stdout: str) -> str:
    """The line the locale strategy printed, dug out of the run report.

    `sbx run` indents whatever the strategy said under a heading of its own, so
    the line is found by what it begins with rather than by where it sits.
    """
    [facts] = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith("LC_ALL=")
    ]
    return facts


@pytest.fixture(scope="session")
def other_locale() -> str:
    """A locale that is not C, or a skip if this machine has none installed."""
    probe = "import locale, sys; locale.setlocale(locale.LC_ALL, sys.argv[1])"
    result = subprocess.run(
        [sys.executable, "-c", probe, OTHER_LOCALE],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"{OTHER_LOCALE} is not installed on this machine")
    return OTHER_LOCALE


# ---------------------------------------------------------------------------
# set and dict iteration order — why the instrument is a set
# ---------------------------------------------------------------------------


def test_a_set_walk_moves_with_the_salt_where_a_dict_walk_does_not() -> None:
    """The choice of instrument, demonstrated rather than asserted in prose."""
    walks = [
        bare_python(INSTRUMENT_PROGRAM, PYTHONHASHSEED="random").splitlines()
        for _ in range(UNPINNED_REPEATS)
    ]

    # Two distinct orderings establish that the ordering is not fixed. Insisting
    # all five differ would be a claim about how the salt is distributed, which
    # is not this file's business.
    assert len({walk[0] for walk in walks}) >= 2
    # And the reason a dict is the wrong instrument: it iterates in insertion
    # order whatever the salt is, so a dict-walking strategy reproduces even on
    # an unpinned interpreter and would prove nothing about the pin.
    assert len({walk[1] for walk in walks}) == 1


# ---------------------------------------------------------------------------
# hash randomisation — the mutation is the interpreter's own default
# ---------------------------------------------------------------------------


def test_the_pinned_salt_is_what_holds_a_set_walking_strategy_still(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """One expression, two environments: unpinned it wanders, in a cell it does not.

    ``PYTHONHASHSEED`` is set to ``random`` explicitly rather than left off.
    Random is already the interpreter's default, but a child inherits whatever
    the test runner was started with, and a contrast that quietly depended on
    the ambient value would be evidence about this machine rather than about
    the pin.
    """
    unpinned = {
        bare_python(FINGERPRINT_PROGRAM, PYTHONHASHSEED="random")
        for _ in range(UNPINNED_REPEATS)
    }
    assert len(unpinned) >= 2

    strategy = strategy_file(tmp_path, SALTED_STRATEGY)
    runs = run_many(sbx_cli, strategy, sealed_data, times=CELL_REPEATS)

    assert len(hashes_of(runs)) == 1
    assert len(positions_of(runs)) == 1
    # A fingerprint that had collapsed to a constant would satisfy both lines
    # above while proving nothing, so check the strategy actually traded.
    assert Decimal(runs[0]["position"]) > 0


# ---------------------------------------------------------------------------
# locale and encoding — the host's locale reaches the strategy nowhere
# ---------------------------------------------------------------------------


def test_an_unpinned_locale_changes_what_a_program_computes(
    other_locale: str,
) -> None:
    """The mutation, in the only place it can honestly be staged.

    There is no way to start a cell in another locale, so this half runs in a
    bare interpreter. What it establishes is that the thing being pinned is
    worth pinning: the same source, run under two locales, computes two
    different strings.
    """
    here = bare_python(LOCALE_PROGRAM, LC_ALL="C", LANG="C").strip()
    elsewhere = bare_python(
        LOCALE_PROGRAM, LC_ALL=other_locale, LANG=other_locale
    ).strip()

    assert here != elsewhere
    # The decimal separator is the one that ruins a ledger quietly: a size
    # formatted through the locale is "0,6" on one machine and "0.6" on
    # another, and both look like a number to everything downstream.
    assert PINNED_POINT in here
    assert "point=," in elsewhere


def test_the_cell_pins_the_locale_whatever_the_host_was_started_in(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str, other_locale: str
) -> None:
    strategy = strategy_file(tmp_path, LOCALE_STRATEGY)

    plain, first = run_once(sbx_cli, strategy, sealed_data)
    foreign, second = run_once(
        sbx_cli,
        strategy,
        sealed_data,
        env={**os.environ, "LC_ALL": other_locale, "LANG": other_locale},
    )
    assert plain.returncode == EXIT_OK, plain.stderr
    assert foreign.returncode == EXIT_OK, foreign.stderr

    facts = reported_facts(plain.stdout)
    assert facts == reported_facts(foreign.stdout)
    assert PINNED_LOCALE in facts
    assert PINNED_POINT in facts
    assert PINNED_WEEKDAY in facts

    # The facts are inside the order size as well as printed, so this is the
    # claim that the pin held all the way to the recorded result.
    assert first["result_hash"] == second["result_hash"]
    assert Decimal(first["position"]) > 0

    # And the contrast: the same source, in an interpreter nobody pinned,
    # started in the very environment the second run above was started in.
    assert facts != bare_python(
        LOCALE_PROGRAM, LC_ALL=other_locale, LANG=other_locale
    ).strip()


# ---------------------------------------------------------------------------
# float accumulation order — visible in the ledger, and refused three ways
# ---------------------------------------------------------------------------


def test_float_accumulation_order_reaches_the_ledger_where_decimal_does_not(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """The mutation, recorded: the same three numbers, added the other way."""
    up = summed(sbx_cli, tmp_path, sealed_data, "floats_up", PARTS_UP)
    down = summed(sbx_cli, tmp_path, sealed_data, "floats_down", PARTS_DOWN)

    assert up["position"] == FLOAT_SUM_UP
    assert down["position"] == FLOAT_SUM_DOWN
    assert up["position"] != down["position"]

    exact_up = summed(sbx_cli, tmp_path, sealed_data, "exact_up", EXACT_PARTS_UP)
    exact_down = summed(sbx_cli, tmp_path, sealed_data, "exact_down", EXACT_PARTS_DOWN)

    assert exact_up["position"] == EXACT_SUM
    assert exact_down["position"] == EXACT_SUM

    # The position is the load-bearing assertion above. `result_hash` carries
    # the code hash, and these are four different files, so the four hashes
    # differ whatever the arithmetic did — only what was recorded can tell the
    # two implementations apart.
    records = (up, down, exact_up, exact_down)
    assert len({record["result_hash"] for record in records}) == len(records)


def test_a_float_size_fails_the_run_rather_than_being_recorded(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    strategy = strategy_file(tmp_path, FLOAT_SIZE_STRATEGY, "float_size.py")

    result, record = run_once(sbx_cli, strategy, sealed_data)

    assert result.returncode == EXIT_ERROR
    assert record["outcome"] == "failed"
    # No result, rather than a result nobody could reproduce.
    assert record["result_hash"] is None
    assert record["fills"] == []
    # The run is still written down — a run that failed is still something sbx
    # did — and the reason names the float rather than complaining vaguely.
    assert record["run_id"] in result.stderr
    assert "must be a Decimal" in result.stderr


def test_a_float_laundered_through_decimal_is_refused_by_the_host(
    sbx_cli, sbx_home: Path, tmp_path: Path, sealed_data: str
) -> None:
    """The type check is not the whole defence, and this is the proof.

    ``Decimal(0.1 + 0.2 + 0.3)`` *is* a Decimal, so it walks past the runtime's
    refusal untouched. What the host then turns down is the accumulation debris
    itself, arriving in full: fifty-one decimal places where a size was due.
    """
    strategy = strategy_file(tmp_path, LAUNDERED_FLOAT_STRATEGY, "laundered.py")

    result, record = run_once(sbx_cli, strategy, sealed_data)

    assert result.returncode == EXIT_ERROR
    assert record["outcome"] == "failed"
    assert record["result_hash"] is None
    assert record["fills"] == []
    assert "decimal places" in result.stderr

    laundered = Decimal(0.1 + 0.2 + 0.3)
    assert laundered != Decimal(EXACT_SUM)
    assert -laundered.as_tuple().exponent == LAUNDERED_PLACES


def test_the_float_that_decimal_avoids_has_no_canonical_form() -> None:
    accumulated = 0.1 + 0.1 + 0.1

    # Not the same number, which is the entire reason money is Decimal here.
    assert accumulated != Decimal("0.3")
    assert str(accumulated) == FLOAT_TENTHS

    with pytest.raises(NotCanonicalError):
        canonical.encode(accumulated)
    # And at depth, which is where a shallower encoder would let one through.
    with pytest.raises(NotCanonicalError):
        canonical.encode({"fills": [{"size": accumulated}]})

    # The Decimal it is not encodes, and encodes as text.
    assert canonical.encode(Decimal("0.3")) == b'"0.3"'
    # Nor can a float arrive from outside: a bare JSON number is refused on the
    # way back in too, so a hand-edited ledger line cannot smuggle one home.
    with pytest.raises(NotCanonicalError):
        canonical.decode(b'{"size":0.3}')
