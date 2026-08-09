"""Where sbx keeps its state, and nothing configurable about it.

Everything lives under ``~/.sbx``. There is no ``SBX_HOME`` environment
variable and no config file: a knob that only the test suite turns is still a
knob, and every one of them is a way for two runs to differ without saying so.

Each function re-resolves from :func:`pathlib.Path.home` on every call and
caches nothing, which is what lets a test move ``HOME`` and have subprocesses
follow it.
"""

from __future__ import annotations

from pathlib import Path

from .errors import SbxError

HOME_DIRNAME = ".sbx"
DATASETS_DIRNAME = "datasets"
CODE_DIRNAME = "code"
LEDGER_FILENAME = "ledger.jsonl"


def require_file(path: str | Path, noun: str) -> Path:
    """Insist on a regular file, and say precisely which way it is wrong.

    One definition, because the same three-line check had grown four copies
    with three different answers: one reported a directory as missing, and one
    accepted a fifo as a journal.
    """
    candidate = Path(path)
    if candidate.is_dir():
        raise SbxError(f"{candidate} is a directory, not a {noun}")
    if not candidate.is_file():
        raise SbxError(f"no such {noun}: {candidate}")
    return candidate


def sbx_home() -> Path:
    """The root of everything sbx owns."""
    return Path.home() / HOME_DIRNAME


def datasets_dir() -> Path:
    """The content-addressed store of sealed datasets."""
    return sbx_home() / DATASETS_DIRNAME


def code_dir() -> Path:
    """Kept copies of every strategy that has ever been run.

    Without these, a run tuple is only re-executable while nobody edits the
    file it names — and "months later" is the point.
    """
    return sbx_home() / CODE_DIRNAME


def ledger_path() -> Path:
    """The append-only record of everything sbx has done."""
    return sbx_home() / LEDGER_FILENAME
