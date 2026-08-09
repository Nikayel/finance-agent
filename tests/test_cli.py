"""Milestone 1 — the CLI shell: four verbs, no more.

These tests are the fence. A fifth verb, or a verb that quietly disappears,
fails here before it reaches a reviewer.
"""

from __future__ import annotations

import pytest

import sbx
from sbx.cli import EXIT_ERROR, EXIT_USAGE, VERBS

EXPECTED_VERBS = ("ls", "run", "seal", "verify")

# Every verb now does something, so a verb's own suite owns its behaviour.
# What stays here is the fence: which verbs exist, and how the entry point
# behaves when it is handed nonsense.


def test_verb_set_is_exactly_four() -> None:
    assert tuple(sorted(VERBS)) == EXPECTED_VERBS


def test_version_is_reported(sbx_cli) -> None:
    result = sbx_cli("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"sbx {sbx.__version__}"


def test_help_lists_every_verb(sbx_cli) -> None:
    result = sbx_cli("--help")
    assert result.returncode == 0
    for verb in EXPECTED_VERBS:
        assert verb in result.stdout


@pytest.mark.parametrize("verb", EXPECTED_VERBS)
def test_each_verb_has_its_own_help(sbx_cli, verb: str) -> None:
    result = sbx_cli(verb, "--help")
    assert result.returncode == 0
    assert verb in result.stdout


def test_a_failing_verb_reports_one_clean_line(sbx_cli, sbx_home) -> None:
    """Expected failures are a sentence on stderr, never a traceback."""
    result = sbx_cli("verify", "run-9999")

    assert result.returncode == EXIT_ERROR
    # Failures never contaminate stdout, so callers can pipe it safely.
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert result.stderr.strip().count("\n") == 0


def test_no_verb_is_a_usage_error(sbx_cli) -> None:
    result = sbx_cli()
    assert result.returncode == EXIT_USAGE


@pytest.mark.parametrize(
    "verb", ["backtest", "report", "serve", "plot", "export", "init", "config"]
)
def test_a_fifth_verb_is_rejected(sbx_cli, verb: str) -> None:
    result = sbx_cli(verb)
    assert result.returncode == EXIT_USAGE


def test_run_requires_data_and_seed(sbx_cli) -> None:
    assert sbx_cli("run", "strategy.py").returncode == EXIT_USAGE
    assert sbx_cli("run", "strategy.py", "--data", "deadbeef").returncode == EXIT_USAGE
    assert sbx_cli("run", "strategy.py", "--seed", "42").returncode == EXIT_USAGE


def test_seed_must_be_an_integer(sbx_cli) -> None:
    result = sbx_cli("run", "strategy.py", "--data", "deadbeef", "--seed", "later")
    assert result.returncode == EXIT_USAGE
