"""Process exit codes, in one place so the CLI and the commands agree.

Kept out of :mod:`sbx.cli` so command handlers can name them without importing
the parser that dispatches to them.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2  # argparse's own convention, named here so tests can assert it.
