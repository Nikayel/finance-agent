"""Milestone 2 — `sbx seal` and `sbx ls`, driven through the console script.

The milestone's exit gate is a single command: seal a journal, change one byte
of the sealed copy, and have `sbx ls` say so. These tests are that gate.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import journals

from sbx import paths
from sbx.exits import EXIT_ERROR, EXIT_OK


def _tamper(data_path: Path) -> None:
    """Flip one byte in place, leaving the file exactly as long as it was."""
    original_mode = data_path.stat().st_mode
    os.chmod(data_path, 0o644)
    payload = bytearray(data_path.read_bytes())
    middle = len(payload) // 2
    payload[middle] ^= 0x01
    data_path.write_bytes(bytes(payload))
    os.chmod(data_path, original_mode)


# --- seal -------------------------------------------------------------------


def test_seal_reports_the_content_hash(sbx_cli, sbx_home: Path, journal: Path) -> None:
    expected = hashlib.sha256(journal.read_bytes()).hexdigest()

    result = sbx_cli("seal", str(journal))

    assert result.returncode == EXIT_OK
    assert expected in result.stdout
    assert (paths.datasets_dir() / expected / "data.jsonl").exists()


def test_seal_copies_the_bytes_verbatim(sbx_cli, sbx_home: Path, journal: Path) -> None:
    sbx_cli("seal", str(journal))

    digest = hashlib.sha256(journal.read_bytes()).hexdigest()
    stored = paths.datasets_dir() / digest / "data.jsonl"
    assert stored.read_bytes() == journal.read_bytes()


def test_sealing_twice_is_a_no_op(sbx_cli, sbx_home: Path, journal: Path) -> None:
    first = sbx_cli("seal", str(journal))
    second = sbx_cli("seal", str(journal))

    assert first.returncode == second.returncode == EXIT_OK
    assert "already sealed" in second.stdout
    assert len(list(paths.datasets_dir().iterdir())) == 1
    # And the ledger records the dataset once, not once per attempt.
    assert paths.ledger_path().read_bytes().count(b"\n") == 1


def test_sealing_a_missing_journal_fails_cleanly(sbx_cli, sbx_home: Path) -> None:
    result = sbx_cli("seal", "/nonexistent/events.jsonl")

    assert result.returncode == EXIT_ERROR
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert "no such journal" in result.stderr


def test_sealing_an_empty_journal_is_refused(
    sbx_cli, sbx_home: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.touch()

    result = sbx_cli("seal", str(empty))

    assert result.returncode == EXIT_ERROR
    assert "Traceback" not in result.stderr


# --- ls ---------------------------------------------------------------------


def test_ls_on_an_untouched_home_says_nothing_is_there(
    sbx_cli, sbx_home: Path
) -> None:
    result = sbx_cli("ls")

    assert result.returncode == EXIT_OK
    assert "DATASETS" in result.stdout
    assert "RUNS" in result.stdout
    assert result.stdout.count("(none)") == 2


def test_ls_lists_a_sealed_dataset_as_ok(
    sbx_cli, sbx_home: Path, journal: Path
) -> None:
    sbx_cli("seal", str(journal))
    digest = hashlib.sha256(journal.read_bytes()).hexdigest()

    result = sbx_cli("ls")

    assert result.returncode == EXIT_OK
    assert digest[:12] in result.stdout
    assert "ok" in result.stdout
    assert "TAMPERED" not in result.stdout


def test_ls_reports_a_tampered_dataset(sbx_cli, sbx_home: Path, journal: Path) -> None:
    """The milestone's demo: one byte changed, one command to see it."""
    sbx_cli("seal", str(journal))
    digest = hashlib.sha256(journal.read_bytes()).hexdigest()
    stored = paths.datasets_dir() / digest / "data.jsonl"
    length_before = stored.stat().st_size

    _tamper(stored)

    assert stored.stat().st_size == length_before  # a real edit, not a truncation
    result = sbx_cli("ls")
    assert result.returncode == EXIT_OK
    assert "TAMPERED" in result.stdout


def test_ls_lists_every_dataset(sbx_cli, sbx_home: Path, tmp_path: Path) -> None:
    first = journals.write_journal(tmp_path / "a.jsonl")
    second = journals.write_journal(
        tmp_path / "b.jsonl",
        [journals.trade(price="1.00", at="2026-07-29T14:30:01+00:00", sequence=1)],
    )
    sbx_cli("seal", str(first))
    sbx_cli("seal", str(second))

    result = sbx_cli("ls")

    for source in (first, second):
        assert hashlib.sha256(source.read_bytes()).hexdigest()[:12] in result.stdout
