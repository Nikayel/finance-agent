"""Milestone 5 — the frame that runs the human's attacks on the time gate.

The attacks are not in this file and were not written by the process that wrote
this file. `tests/adversarial/README.md` states that rule and why the milestone
is worth nothing without it. What lives here is only the frame: it finds
whatever the human has left in `tests/adversarial/`, feeds each attack to the
real `sbx run`, and prints one line per attack saying how the gate refused it.
That printed report *is* the milestone's exit gate — "all adversarial
strategies fail with a report saying how" — which is why it is emitted rather
than merely asserted on.

Two things an attack must not manage, stated as the assertions:

* **It cannot claim its own success.** An attack reaches `sys.exit(97)` only
  when it has watched the gate hand over the future, so a 97 arriving at the
  user in any form is a failing test. See the exit-code convention in the
  directory's README.
* **It cannot conjure a fill.** Fills are the only thing a strategy can move,
  so a breach shows up there or nowhere. Every fill has to land on a trade that
  is genuinely in the sealed journal, at that trade's own price, and never on
  the first trade — the tick that carried it had no decision behind it yet.

Today `tests/adversarial/` holds no attacks, and this file collects exactly one
test: that the directory and its rules exist. That is the honest state of the
milestone.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import journals

from sbx import ledger
from sbx.exits import EXIT_OK

ADVERSARIAL = Path(__file__).resolve().parent / "adversarial"

SEED = 5

# The attack's own verdict on itself, the same code `tests/hostile/` uses: 97 is
# reached only when the thing being attacked handed over what it should not.
CLAIMED_SUCCESS = 97

# How that verdict reads by the time it has crossed the cell boundary. The
# cell's runtime reports a strategy's `SystemExit` to the host verbatim, and the
# cell itself names a bare exit status; either way it reaches the user as the
# one line `sbx run` prints on stderr.
CLAIM_MARKERS = (f"SystemExit: {CLAIMED_SUCCESS}", f"status {CLAIMED_SUCCESS}")

# The two labelled lines every attack's docstring carries.
ATTEMPTS = "Attempts:"
GATE_MUST = "Gate must:"

TRADE_COUNT = 8


# ---------------------------------------------------------------------------
# the journal the attacks are pointed at
# ---------------------------------------------------------------------------


def at_second(second: int) -> str:
    """A sim-time inside one minute, in the upstream dialect's format."""
    return f"2026-07-29T14:30:{second:02d}.000000+00:00"


def attack_records() -> list[dict[str, Any]]:
    """A short journal whose prices are distinct, so a fill names one trade.

    Salted with the two records that are special to the gate: an `error`, which
    is never revealed at all, and a `gap`, which is revealed but carries no time
    of its own. Both are places a seek-ahead attempt might go looking.
    """
    records: list[dict[str, Any]] = [
        journals.trade(
            price=f"118{200 + index}.50", at=at_second(index + 1), sequence=index + 1
        )
        for index in range(TRADE_COUNT)
    ]
    records.insert(2, journals.ERROR)
    records.insert(5, journals.GAP)
    return records


PRICE_AT = {
    record["executed_at"]: record["price"]
    for record in attack_records()
    if record["type"] == "trade"
}

# The first trade is the first tick, so its price was on the strategy's screen
# before it could place anything. Nothing may ever fill there.
FIRST_TRADE = at_second(1)


def sha256_of(path: Path) -> str:
    """The digest the sealed dataset is named by, computed without sbx."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def sealed_data(sbx_cli, sbx_home: Path, tmp_path: Path) -> str:
    """Seal the attack journal through the CLI and return its digest."""
    source = journals.write_journal(tmp_path / "market.jsonl", attack_records())
    result = sbx_cli("seal", str(source))
    assert result.returncode == EXIT_OK
    return sha256_of(source)


# ---------------------------------------------------------------------------
# reading an attack, and reporting on it
# ---------------------------------------------------------------------------


def labelled(path: Path, label: str) -> str:
    """The text after `label` in an attack's docstring, or "" if it has none."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    return ""


def refusal_of(result) -> str:
    """`sbx run`'s own account of the run, flattened onto one line."""
    said = result.stderr.strip()
    if said:
        return " ".join(said.split())
    return "the run completed; by the exit-code convention the attempt was refused"


def report(attack: Path, result, record: dict[str, Any] | None) -> str:
    """One line naming what the attack tried and how the gate answered."""
    outcome = "no run recorded" if record is None else record["outcome"]
    fills = 0 if record is None else len(record["fills"])
    return (
        f"[adversarial] {attack.name}: attempted {labelled(attack, ATTEMPTS)} "
        f"-> sbx exit {result.returncode}, outcome {outcome}, {fills} fills: "
        f"{refusal_of(result)}"
    )


# ---------------------------------------------------------------------------
# the standing test — the rules exist even when the attacks do not
# ---------------------------------------------------------------------------


def test_the_adversarial_directory_and_its_rules_are_present() -> None:
    """The one test that runs whether or not anybody has written an attack.

    An empty directory is a real state of this milestone, not a failure. What
    would be a failure is the rules going missing: the human-only rule is the
    whole reason these attacks count for anything, and the harness itself reads
    the exit-code and docstring conventions out of the same README.
    """
    assert ADVERSARIAL.is_dir(), f"missing {ADVERSARIAL}"
    readme = ADVERSARIAL / "README.md"
    assert readme.is_file(), f"missing {readme}"

    rules = readme.read_text(encoding="utf-8")
    assert "hand" in rules and "agent" in rules  # who may write an attack
    assert str(CLAIMED_SUCCESS) in rules  # how one announces that it won
    assert ATTEMPTS in rules and GATE_MUST in rules  # what its docstring says


# ---------------------------------------------------------------------------
# one test per attack — defined only when there is an attack to run
# ---------------------------------------------------------------------------

ATTACKS = sorted(ADVERSARIAL.glob("strategy_*.py"))

# The guard is not decoration: pytest turns an empty `parametrize` into a
# skipped placeholder, and a skipped adversarial test reads as one somebody
# turned off rather than as a directory nobody has filled in yet.
if ATTACKS:

    @pytest.mark.parametrize(
        "attack", [pytest.param(path, id=path.name) for path in ATTACKS]
    )
    def test_an_attack_on_the_time_gate_is_refused(
        sbx_cli, sbx_home: Path, sealed_data: str, attack: Path
    ) -> None:
        """Run one hand-written attack through the real CLI and report on it."""
        assert labelled(attack, ATTEMPTS) != "", f"{attack.name} has no {ATTEMPTS!r}"
        assert labelled(attack, GATE_MUST) != "", f"{attack.name} has no {GATE_MUST!r}"

        result = sbx_cli(
            "run", str(attack), "--data", sealed_data, "--seed", str(SEED)
        )

        runs = ledger.entries_of("run")
        record = runs[0] if runs else None
        # Printed before the assertions, so the report survives a failure — the
        # run that beat the gate is the one whose account matters most.
        print(report(attack, result, record))

        # A run that happened but left no record is indistinguishable from one
        # nobody started, which is the one thing the ledger may not allow.
        assert record is not None, f"{attack.name} ran but left no record"
        assert len(runs) == 1
        # sbx expecting its own failure mode is part of surviving an attack.
        assert "Traceback" not in result.stderr

        # The attack's verdict on itself. 97 reaching the user in any form means
        # it was handed the future and said so.
        assert result.returncode != CLAIMED_SUCCESS
        for marker in CLAIM_MARKERS:
            assert marker not in result.stderr, marker
            assert marker not in result.stdout, marker

        # Fills are the only thing a strategy can move, so a silent success
        # would show as a fill that no trade in the journal could have produced.
        for fill in record["fills"]:
            assert fill["at"] in PRICE_AT, fill
            assert fill["price"] == PRICE_AT[fill["at"]], fill
            assert fill["at"] != FIRST_TRADE, fill

        # And the sealed bytes those fills were priced from are still the bytes
        # that were sealed: an attack that edits the dataset has won by other
        # means. `ls` re-hashes every dataset, so this is a real check.
        assert "TAMPERED" not in sbx_cli("ls").stdout
