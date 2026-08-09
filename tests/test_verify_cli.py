"""Milestone 6 — `sbx verify`, driven through the console script.

`sbx run` produces a claim. `sbx verify` is the only thing that can check one,
so what these tests pin is less the arithmetic — the gate and the fill rule are
tested where they live — than what verify is *allowed to say*:

* **A claim reproduces or it does not, and a divergence is named.** A
  reproduced run prints the result hash back, so the verdict can be matched
  against the ledger by eye. A diverged one prints the first record that
  differs, not a diff of everything: a wall of output is how a divergence
  report goes unread.
* **Nothing may be claimed that was not replayed.** If the sealed bytes no
  longer hash to what the run recorded, the verdict is ``TAMPERED`` and
  ``replayed_hash`` is None. Re-running against altered data produces a number,
  and that number is evidence about nothing.
* **Where a result came from is part of the result.** A run recorded on another
  machine says so whether it reproduces or not. A verify that printed a clean
  ``REPRODUCED`` while quietly omitting that the run happened somewhere else
  would make every other guarantee in the ledger unfalsifiable, so that case
  has tests of its own — built by appending a real record under a different
  fingerprint rather than by pretending to be a different machine.
* **Verifying is a read.** The ledger is byte-identical afterwards.

A run is re-executable months later only because `sbx run` kept the strategy
source under ``~/.sbx/code``. The tests that delete and rewrite the original
file are what pin that, and the one that deletes the *kept* copy is what pins
the failure when it is gone.

The CLI is exercised as the installed console script throughout: the exit code
and the stderr line are the contract, and calling ``main()`` in-process would
test neither.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import journals

from sbx import canonical, ledger, store, verify
from sbx.errors import SbxError
from sbx.exits import EXIT_ERROR, EXIT_OK

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

SEED = 7
TRADE_COUNT = 12

# A fingerprint no machine here could produce, standing in for "recorded
# elsewhere". Shaped like the real thing, because it is read as one.
FOREIGN_ENVIRONMENT = "0" * 64


# --- strategies, as the plain text a user would write -----------------------

BUYING_STRATEGY = """\
from decimal import Decimal


def strategy(market):
    placed = False
    for tick in market.ticks():
        if not placed:
            market.order("BUY", Decimal("0.001"))
            placed = True
"""

REWRITTEN_STRATEGY = """\
def strategy(market):
    for tick in market.ticks():
        pass
"""

# Honest nondeterminism: the wall clock is the one input the cell cannot seal,
# so an order sized from it cannot land on the same number twice. Nanoseconds
# rather than `time.time()` so the two executions are *certain* to disagree
# rather than merely overwhelmingly likely to. Only the low digits are used,
# and a trailing 1 is appended: the size has to stay a plausible quantity —
# positive, and inside the decimal places the host will accept — or the run
# fails instead of diverging, which would prove nothing about verify.
CLOCK_STRATEGY = """\
import time
from decimal import Decimal


def strategy(market):
    size = Decimal("0." + str(time.time_ns())[-12:] + "1")
    placed = False
    for tick in market.ticks():
        if not placed:
            market.order("BUY", size)
            placed = True
"""


def replay_records() -> list[dict[str, Any]]:
    """Trades a second apart, salted with the records replay treats specially."""
    records: list[dict[str, Any]] = [
        journals.trade(
            price=f"118{200 + index}.50",
            at=f"2026-07-29T14:30:{index + 1:02d}.000000+00:00",
            sequence=index + 1,
        )
        for index in range(TRADE_COUNT)
    ]
    records.insert(3, journals.ERROR)  # not an event: skipped entirely
    records.insert(7, journals.GAP)  # an event with no timestamp of its own
    return records


def transplanted(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Append a real run record again, as another machine would have written it.

    Nothing here fakes a machine, and nothing here recomputes a `result_hash`:
    the record is one sbx wrote, re-appended under a new id with the single
    field that would differ replaced. Faking a machine would test the machine,
    and rebuilding the hash would test this file's copy of a formula that lives
    in `sbx.runner` — what is under test is whether verify reads the
    fingerprint it was handed and says what it found.
    """
    return ledger.append({**record, **changes, "run_id": ledger.next_run_id()})


def tamper(data_path: Path) -> None:
    """Flip one byte in place, leaving the file exactly as long as it was."""
    original_mode = data_path.stat().st_mode
    os.chmod(data_path, 0o644)
    payload = bytearray(data_path.read_bytes())
    payload[len(payload) // 2] ^= 0x01
    data_path.write_bytes(bytes(payload))
    os.chmod(data_path, original_mode)


def forget_kept_code(record: dict[str, Any]) -> Path:
    """Delete the strategy copy the run kept, as a stray `rm` would.

    The copy is read-only and its directory may be too — sbx never deletes
    either, so the modes are cleared here rather than being worked around.
    """
    kept = store.code_path(record["code"])
    os.chmod(kept.parent, 0o755)
    os.chmod(kept, 0o644)
    kept.unlink()
    return kept


def assert_clean_failure(result) -> None:
    """Exit 1, one line of explanation, and nothing that reads as a crash."""
    assert result.returncode == EXIT_ERROR
    assert result.stderr.strip() != ""
    assert len(result.stderr.strip().splitlines()) == 1
    # A traceback is sbx admitting it did not expect its own failure mode.
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


def mentions_environment(result) -> bool:
    """Whether the command said anything at all about where the run happened.

    Either stream counts: the verdict belongs on stdout, but a note that the
    machine differed is not itself an error, and the spec does not place it.
    """
    return "environment" in (result.stdout + result.stderr).lower()


@pytest.fixture
def sealed_data(sbx_cli, sbx_home: Path, tmp_path: Path) -> str:
    """Seal the replay journal through the CLI and return its digest."""
    source = journals.write_journal(tmp_path / "market.jsonl", replay_records())
    result = sbx_cli("seal", str(source))
    assert result.returncode == EXIT_OK
    return canonical.hash_file(source)


@pytest.fixture
def strategy(tmp_path: Path) -> Path:
    """The strategy file the user wrote, and is free to edit afterwards."""
    path = tmp_path / "strategy.py"
    path.write_text(BUYING_STRATEGY, encoding="utf-8")
    return path


@pytest.fixture
def recorded_run(sbx_cli, sbx_home: Path, sealed_data: str, strategy: Path) -> dict:
    """One completed run, and the ledger record that is its whole claim."""
    result = sbx_cli("run", str(strategy), "--data", sealed_data, "--seed", str(SEED))
    assert result.returncode == EXIT_OK

    runs = ledger.entries_of("run")
    assert len(runs) == 1
    assert runs[0]["outcome"] == "completed"
    assert runs[0]["fills"] != []  # a run with nothing in it proves nothing
    return runs[0]


# ---------------------------------------------------------------------------
# the shape of a verdict
# ---------------------------------------------------------------------------


def test_the_verdicts_are_exactly_the_three_names() -> None:
    assert verify.VERDICTS == ("REPRODUCED", "DIVERGED", "TAMPERED")


def test_a_verification_says_exactly_what_it_is_documented_to_say() -> None:
    assert dataclasses.is_dataclass(verify.Verification)
    assert [field.name for field in dataclasses.fields(verify.Verification)] == [
        "verdict",
        "run_id",
        "recorded_hash",
        "replayed_hash",
        "divergence",
        "same_environment",
    ]


def test_a_verdict_cannot_be_edited_after_it_is_returned(
    sbx_home: Path, recorded_run: dict
) -> None:
    verification = verify.verify(recorded_run["run_id"])

    # A verdict a caller can overwrite is not a verdict.
    with pytest.raises(dataclasses.FrozenInstanceError):
        verification.verdict = "DIVERGED"


# ---------------------------------------------------------------------------
# reproduction — the same tuple, re-executed, byte for byte
# ---------------------------------------------------------------------------


def test_verifying_a_fresh_run_reproduces_it(
    sbx_cli, sbx_home: Path, recorded_run: dict
) -> None:
    result = sbx_cli("verify", recorded_run["run_id"])

    assert result.returncode == EXIT_OK
    assert "REPRODUCED" in result.stdout
    # Printing the hash back is what lets a reader match the verdict against
    # the ledger without trusting the verdict.
    assert recorded_run["result_hash"] in result.stdout
    assert "Traceback" not in result.stderr


def test_a_reproduction_reports_the_hash_it_matched(
    sbx_home: Path, recorded_run: dict
) -> None:
    verification = verify.verify(recorded_run["run_id"])

    assert verification.verdict == "REPRODUCED"
    assert verification.run_id == recorded_run["run_id"]
    assert verification.recorded_hash == recorded_run["result_hash"]
    assert HEX64.match(verification.recorded_hash)
    assert verification.replayed_hash == verification.recorded_hash
    assert verification.divergence is None
    # This run happened here, minutes ago, so there is nothing to disclose.
    assert verification.same_environment is True


def test_verifying_leaves_the_ledger_byte_identical(
    sbx_cli, sbx_home: Path, recorded_run: dict
) -> None:
    before = ledger.path().read_bytes()

    assert sbx_cli("verify", recorded_run["run_id"]).returncode == EXIT_OK
    verify.verify(recorded_run["run_id"])

    # Verifying is a read. A verify that recorded itself would be a run.
    assert ledger.path().read_bytes() == before
    assert len(ledger.entries()) == 2  # the sealing and the one run


# ---------------------------------------------------------------------------
# the kept source — a run outlives the file it was typed into
# ---------------------------------------------------------------------------


def test_the_run_keeps_the_source_it_executed(
    sbx_home: Path, recorded_run: dict, strategy: Path
) -> None:
    kept = store.code_path(recorded_run["code"])

    assert kept.is_file()
    assert kept.is_relative_to(sbx_home)
    assert kept.read_bytes() == strategy.read_bytes()
    assert canonical.hash_file(kept) == recorded_run["code"]
    # Read-only, so the one copy that can still answer for the run cannot be
    # edited into agreeing with a later result.
    assert kept.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize(
    "lose",
    [
        pytest.param(lambda path: path.unlink(), id="deleted"),
        pytest.param(
            lambda path: path.write_text(REWRITTEN_STRATEGY, encoding="utf-8"),
            id="rewritten",
        ),
    ],
)
def test_a_run_still_reproduces_once_the_original_file_is_gone(
    sbx_cli,
    sbx_home: Path,
    recorded_run: dict,
    strategy: Path,
    lose: Callable[[Path], Any],
) -> None:
    lose(strategy)

    result = sbx_cli("verify", recorded_run["run_id"])

    assert result.returncode == EXIT_OK
    assert "REPRODUCED" in result.stdout
    assert verify.verify(recorded_run["run_id"]).verdict == "REPRODUCED"


# ---------------------------------------------------------------------------
# failures — one line, no traceback, exit 1
# ---------------------------------------------------------------------------


def test_an_unknown_run_id_fails_cleanly(
    sbx_cli, sbx_home: Path, recorded_run: dict
) -> None:
    result = sbx_cli("verify", "run-9999")

    assert_clean_failure(result)
    assert "run-9999" in result.stderr
    # Nothing was verified, so no verdict may be printed either.
    for verdict in verify.VERDICTS:
        assert verdict not in result.stdout


def test_an_unknown_run_id_is_an_sbx_error(sbx_home: Path, recorded_run: dict) -> None:
    with pytest.raises(SbxError) as caught:
        verify.verify("run-9999")

    assert "run-9999" in str(caught.value)


def test_a_missing_kept_source_names_what_is_gone(
    sbx_cli, sbx_home: Path, recorded_run: dict
) -> None:
    forget_kept_code(recorded_run)

    result = sbx_cli("verify", recorded_run["run_id"])

    assert_clean_failure(result)
    # The code hash is the only name the missing file has.
    assert recorded_run["code"][:12] in result.stderr
    with pytest.raises(SbxError):
        verify.verify(recorded_run["run_id"])


# ---------------------------------------------------------------------------
# tampering — altered bytes end the verification, they do not steer it
# ---------------------------------------------------------------------------


def test_a_tampered_dataset_is_reported_rather_than_re_executed(
    sbx_cli, sbx_home: Path, recorded_run: dict
) -> None:
    data_path = store.load(recorded_run["data"]).data_path
    length_before = data_path.stat().st_size

    tamper(data_path)

    assert data_path.stat().st_size == length_before  # an edit, not a truncation
    result = sbx_cli("verify", recorded_run["run_id"])
    assert result.returncode == EXIT_ERROR
    assert "TAMPERED" in result.stdout
    assert "REPRODUCED" not in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_a_tampered_verification_replayed_nothing(
    sbx_home: Path, recorded_run: dict
) -> None:
    tamper(store.load(recorded_run["data"]).data_path)

    verification = verify.verify(recorded_run["run_id"])

    assert verification.verdict == "TAMPERED"
    assert verification.recorded_hash == recorded_run["result_hash"]
    # The point of the whole verdict: a re-run against bytes that are no longer
    # the sealed bytes produces a number about some other dataset. There is no
    # replayed hash because there was nothing honest to replay.
    assert verification.replayed_hash is None


# ---------------------------------------------------------------------------
# divergence — the first record that differs, and only that one
# ---------------------------------------------------------------------------


@pytest.fixture
def clock_run(sbx_cli, sbx_home: Path, sealed_data: str, tmp_path: Path) -> dict:
    """A completed run whose result is a reading of the wall clock."""
    path = tmp_path / "clock.py"
    path.write_text(CLOCK_STRATEGY, encoding="utf-8")

    result = sbx_cli("run", str(path), "--data", sealed_data, "--seed", str(SEED))
    assert result.returncode == EXIT_OK

    runs = ledger.entries_of("run")
    assert len(runs) == 1
    assert runs[0]["fills"] != []
    return runs[0]


def test_a_clock_reading_run_does_not_reproduce(
    sbx_cli, sbx_home: Path, clock_run: dict
) -> None:
    result = sbx_cli("verify", clock_run["run_id"])

    assert result.returncode == EXIT_ERROR
    assert "DIVERGED" in result.stdout
    assert "REPRODUCED" not in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    # A verdict with no difference attached sends the reader back to the ledger
    # to find it by hand, which is the job this command exists to do.
    assert any(
        field in result.stdout for field in ("position", "pnl", "fills")
    ), result.stdout


def test_a_divergence_names_one_field_and_stops(
    sbx_home: Path, clock_run: dict
) -> None:
    verification = verify.verify(clock_run["run_id"])

    assert verification.verdict == "DIVERGED"
    assert verification.recorded_hash == clock_run["result_hash"]
    assert HEX64.match(verification.replayed_hash or "")
    assert verification.replayed_hash != verification.recorded_hash

    divergence = verification.divergence
    assert divergence is not None
    assert divergence.strip() != ""
    # One line: the *first* difference, not a diff of everything.
    assert "\n" not in divergence
    assert any(field in divergence for field in ("position", "pnl", "fills"))


# ---------------------------------------------------------------------------
# the environment — a result never hides where it came from
# ---------------------------------------------------------------------------


def test_a_run_recorded_elsewhere_says_so_even_when_it_reproduces(
    sbx_cli, sbx_home: Path, recorded_run: dict
) -> None:
    moved = transplanted(recorded_run, env_fingerprint=FOREIGN_ENVIRONMENT)
    assert moved["env_fingerprint"] != recorded_run["env_fingerprint"]

    result = sbx_cli("verify", moved["run_id"])

    assert result.returncode == EXIT_OK
    assert "REPRODUCED" in result.stdout
    # The failure this test exists to stop: a clean verdict that quietly omits
    # having been recorded on a machine nobody here can inspect.
    assert mentions_environment(result), result.stdout + result.stderr

    verification = verify.verify(moved["run_id"])
    assert verification.verdict == "REPRODUCED"
    assert verification.replayed_hash == verification.recorded_hash
    assert verification.same_environment is False


def test_a_run_recorded_elsewhere_that_diverges_says_both(
    sbx_cli, sbx_home: Path, clock_run: dict
) -> None:
    """A foreign claim that fails to reproduce must not report only one of the two.

    Saying "DIVERGED" and nothing else invites the reader to blame the
    strategy, when the run may simply have happened on a machine this one
    cannot speak for.
    """
    moved = transplanted(clock_run, env_fingerprint=FOREIGN_ENVIRONMENT)

    result = sbx_cli("verify", moved["run_id"])

    assert result.returncode == EXIT_ERROR
    assert "DIVERGED" in result.stdout
    assert any(field in result.stdout for field in ("position", "pnl", "fills"))
    assert mentions_environment(result), result.stdout + result.stderr

    verification = verify.verify(moved["run_id"])
    assert verification.verdict == "DIVERGED"
    assert verification.same_environment is False
    assert verification.divergence is not None
    assert verification.divergence.strip() != ""
