"""The failures sbx expects.

Anything raised that is not an :class:`SbxError` is a bug in sbx, and the CLI
deliberately lets it traceback rather than dressing it up as a clean message.
"""

from __future__ import annotations


class SbxError(Exception):
    """An expected, user-facing failure; the CLI prints it and exits nonzero."""


class NotCanonicalError(SbxError):
    """A value has no canonical encoding, so it must never reach a hash."""


class ProtocolError(SbxError):
    """The cell said something the host cannot act on.

    The other end of this wire is untrusted code, so every malformed frame is
    an expected condition rather than a bug: the host reports it and kills the
    cell instead of trying to recover a conversation it has lost track of.
    """


class LedgerCorruptError(SbxError):
    """A committed ledger line will not parse.

    Distinct from a torn write: an unterminated final line is an append that
    never committed and is ignored without complaint. This means damage to a
    record the ledger had already promised was durable.
    """
