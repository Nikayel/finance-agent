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

HOME_DIRNAME = ".sbx"
DATASETS_DIRNAME = "datasets"
LEDGER_FILENAME = "ledger.jsonl"


def sbx_home() -> Path:
    """The root of everything sbx owns."""
    return Path.home() / HOME_DIRNAME


def datasets_dir() -> Path:
    """The content-addressed store of sealed datasets."""
    return sbx_home() / DATASETS_DIRNAME


def ledger_path() -> Path:
    """The append-only record of everything sbx has done."""
    return sbx_home() / LEDGER_FILENAME
