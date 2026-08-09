"""Shared fixtures.

The CLI is exercised as the real installed console script, never by calling
``main()`` in-process: the entry point, its exit codes and its stderr are part
of what the package promises, and importing around them would test a different
thing than the user runs.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import journals

CONSOLE_SCRIPT = Path(sys.executable).parent / "sbx"

Run = Callable[..., "subprocess.CompletedProcess[str]"]


@pytest.fixture(scope="session")
def sbx_cli() -> Run:
    """Return a callable that invokes the installed ``sbx`` console script."""
    if not CONSOLE_SCRIPT.exists():
        pytest.fail(
            f"no console script at {CONSOLE_SCRIPT} — "
            'run `pip install -e ".[dev]"` in this interpreter first'
        )

    def run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CONSOLE_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=30,
            **kwargs,  # type: ignore[arg-type]
        )

    return run


@pytest.fixture
def sbx_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.sbx`` at a throwaway directory for one test.

    sbx resolves its home from ``Path.home()`` on every call and caches
    nothing, so moving ``HOME`` is enough — and because ``monkeypatch.setenv``
    mutates ``os.environ``, subprocesses spawned by the test inherit the same
    redirect. No sbx-specific environment knob exists, on purpose: a config
    surface that only tests use is still a config surface.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / ".sbx"


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    """A journal file in the upstream dialect, covering every record type."""
    return journals.write_journal(tmp_path / "events.jsonl")
