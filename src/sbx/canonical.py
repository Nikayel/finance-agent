"""Canonical encoding and content addressing.

Determinism is the product requirement, so this module is deliberately strict:
one encoding, one hash, and a hard refusal to serialise anything whose bytes
could depend on the machine, the process, or the order things happened in.

Two choices here are load-bearing and easy to "fix" by mistake:

* **Floats are rejected outright.** Money is ``Decimal`` end to end. A float
  result depends on accumulation order, which is exactly the kind of drift
  ``sbx verify`` exists to catch, so it must never reach a hash.
* **A Decimal's trailing zeros are preserved.** ``Decimal("1.50")`` and
  ``Decimal("1.5")`` compare equal but encode differently, because exchange
  precision is data. Normalising them would silently discard it.

Decimals encode as untagged JSON strings, the same dialect the upstream
journals use. That keeps the ledger greppable with ordinary tools at the cost
of type ambiguity on the way back in — :func:`decode` returns plain JSON
types, and each record schema re-applies ``Decimal`` to its own known fields.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import NotCanonicalError

# Big enough that hashing a multi-gigabyte journal never loads it whole, small
# enough to stay off the large-object heap.
_CHUNK_BYTES = 1 << 20

_ROOT = "$"


def decimal_text(value: Decimal) -> str:
    """Render a Decimal in the one form sbx will ever hash.

    Positional notation (never scientific), trailing zeros kept, and negative
    zero folded onto zero so ``-0.00`` and ``0.00`` cannot hash differently.
    """
    if not value.is_finite():
        raise NotCanonicalError(f"non-finite Decimal has no canonical form: {value}")
    text = format(value, "f")
    if text.startswith("-") and value == 0:
        text = text[1:]
    return text


def _canonicalise(value: Any, path: str, ancestors: frozenset[int]) -> Any:
    """Convert to json-safe types, rejecting anything without a stable encoding.

    ``ancestors`` holds the ids of the containers on the path to ``value``, so
    a genuine cycle is refused while a shared subtree is still allowed.
    """
    if value is None or isinstance(value, (bool, int, str)):
        # bool first: it is an int subclass, and true/false must not become 1/0.
        return value

    if isinstance(value, Decimal):
        return decimal_text(value)

    if isinstance(value, float):
        raise NotCanonicalError(
            f"float at {path}: use Decimal — float accumulation order is not "
            f"reproducible ({value!r})"
        )

    if isinstance(value, (dict, list, tuple)):
        if id(value) in ancestors:
            raise NotCanonicalError(f"circular reference at {path}")
        inner = ancestors | {id(value)}

        if isinstance(value, dict):
            for key in value:
                if not isinstance(key, str):
                    raise NotCanonicalError(
                        f"non-string key {key!r} at {path}: JSON would coerce it "
                        f"and two different keys could collide"
                    )
            return {
                key: _canonicalise(item, f"{path}.{key}", inner)
                for key, item in value.items()
            }

        return [
            _canonicalise(item, f"{path}[{index}]", inner)
            for index, item in enumerate(value)
        ]

    raise NotCanonicalError(
        f"{type(value).__name__} at {path} has no canonical form"
    )


def _reject_float(text: str) -> Any:
    raise NotCanonicalError(
        f"bare float {text} in canonical data: monetary values are strings"
    )


def encode(obj: Any) -> bytes:
    """Encode to canonical bytes: sorted keys, no whitespace, pure ASCII."""
    text = json.dumps(
        _canonicalise(obj, _ROOT, frozenset()),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        check_circular=False,  # _canonicalise already proved the graph acyclic
    )
    # ensure_ascii guarantees this, and asserting it keeps the JSONL framing
    # claim (no raw newline can appear in a record) true by construction.
    return text.encode("ascii")


def encode_line(obj: Any) -> bytes:
    """Encode as one newline-terminated record, ready to append to a JSONL file."""
    return encode(obj) + b"\n"


def decode(data: bytes | str) -> Any:
    """Parse canonical bytes back into plain JSON types.

    Decimals come back as strings: the encoding is untagged on purpose, so the
    caller's schema is what restores the type.
    """
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    return json.loads(text, parse_float=_reject_float)


def hash_bytes(data: bytes) -> str:
    """sha256 of raw bytes, as lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def hash_object(obj: Any) -> str:
    """sha256 of an object's canonical encoding — the identity of a result."""
    return hash_bytes(encode(obj))


def hash_file(path: str | Path) -> str:
    """sha256 of a file's contents, streamed so size does not matter."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
