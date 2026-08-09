"""The failures sbx expects.

Anything raised that is not an :class:`SbxError` is a bug in sbx, and the CLI
deliberately lets it traceback rather than dressing it up as a clean message.
"""

from __future__ import annotations


class SbxError(Exception):
    """An expected, user-facing failure; the CLI prints it and exits nonzero."""
