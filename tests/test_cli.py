"""Milestone 1 — the CLI shell: four verbs, no more.

These tests are the fence. A fifth verb, or a verb that quietly disappears,
fails here before it reaches a reviewer.
"""

from __future__ import annotations

import pytest

import sbx
from sbx.cli import EXIT_ERROR, EXIT_USAGE, VERBS

EXPECTED_VERBS = ("ls", "run", "seal", "verify")

# Verbs whose implementation is still ahead of us. They must already parse
# their real argument surface, then say plainly that they do nothing yet.
PENDING_INVOCATIONS = {
    "run": ("run", "strategy.py", "--data", "deadbeef", "--seed", "42"),
    "verify": ("verify", "run-0001"),
}


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


@pytest.mark.parametrize("verb", sorted(PENDING_INVOCATIONS))
def test_each_pending_verb_parses_then_reports_not_implemented(
    sbx_cli, verb: str
) -> None:
    result = sbx_cli(*PENDING_INVOCATIONS[verb])
    assert result.returncode == EXIT_ERROR
    assert "not implemented" in result.stderr.lower()
    # Failures never contaminate stdout, so callers can pipe it safely.
    assert result.stdout == ""
    # And they never leak a traceback at the user.
    assert "Traceback" not in result.stderr


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
