"""Four verbs, no more: seal, run, verify, ls.

This module owns argument parsing and dispatch — nothing else. Each verb's
actual work lives in :mod:`sbx.commands` and is reached through the
``_COMMANDS`` registry, which is also the single source of truth for what
verbs exist. Keeping the handlers out of here is what stops the CLI slowly
becoming the program.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import __version__, commands
from .errors import SbxError
from .exits import EXIT_ERROR, EXIT_OK, EXIT_USAGE

__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_USAGE", "VERBS", "build_parser", "main"]

AddArguments = Callable[[argparse.ArgumentParser], None]
Handler = Callable[[argparse.Namespace], int]


@dataclass(frozen=True)
class Command:
    """One verb: its one-line help, its argument surface, its handler."""

    help: str
    add_arguments: AddArguments
    handler: Handler


def _seal_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("journal", help="path to the journal file to snapshot")


def _run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("strategy", help="path to the strategy file to execute")
    parser.add_argument(
        "--data", required=True, metavar="HASH", help="hash of a sealed dataset"
    )
    parser.add_argument(
        "--seed", required=True, type=int, help="seed for the strategy's randomness"
    )


def _verify_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_id", metavar="RUN-ID", help="id of a run in the ledger")


def _ls_arguments(parser: argparse.ArgumentParser) -> None:
    """``ls`` takes no arguments, and is not going to grow any."""


_COMMANDS: dict[str, Command] = {
    "seal": Command(
        help="snapshot a journal, hash it, and register it immutably",
        add_arguments=_seal_arguments,
        handler=commands.seal,
    ),
    "run": Command(
        help="execute a strategy in the sealed cell and record the run",
        add_arguments=_run_arguments,
        handler=commands.run,
    ),
    "verify": Command(
        help="re-execute a recorded run and diff it byte-for-byte",
        add_arguments=_verify_arguments,
        handler=commands.pending(6),
    ),
    "ls": Command(
        help="list sealed datasets and recorded runs",
        add_arguments=_ls_arguments,
        handler=commands.ls,
    ),
}

VERBS: tuple[str, ...] = tuple(_COMMANDS)


def build_parser() -> argparse.ArgumentParser:
    """Build the whole CLI from the command registry."""
    parser = argparse.ArgumentParser(
        prog="sbx",
        description="Run untrusted strategies against journaled market data, "
        "with look-ahead impossible and every run replayable.",
    )
    parser.add_argument("--version", action="version", version=f"sbx {__version__}")

    subparsers = parser.add_subparsers(dest="verb", metavar="VERB", required=True)
    for verb, command in _COMMANDS.items():
        subparser = subparsers.add_parser(
            verb, help=command.help, description=command.help
        )
        command.add_arguments(subparser)
        subparser.set_defaults(handler=command.handler)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``sbx`` console script."""
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except SbxError as error:
        print(f"sbx: {error}", file=sys.stderr)
        return EXIT_ERROR
