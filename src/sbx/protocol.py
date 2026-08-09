"""The wire between host and cell.

A frame is a 4-byte big-endian length followed by exactly that many bytes of
canonical JSON. That is the whole format.

Two rules exist because the far end is untrusted code:

* **A declared length over the limit is refused before the body is read.**
  Otherwise a cell could make the host allocate whatever it felt like claiming
  by lying in four bytes.
* **A truncated frame is an error, a clean boundary is not.** End-of-stream
  exactly where a frame would start means the cell finished; end-of-stream
  part-way through a header or a body means it died mid-sentence, and the host
  must not treat the fragment as if it meant something.

Both sides speak this. The cell-side copy is the same module, handed to the
cell in its working directory — the wire format is one definition, not two
that drift.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from typing import Any, BinaryIO

from . import canonical
from .errors import ProtocolError

MAX_FRAME_BYTES = 1 << 20
HEADER_BYTES = 4

_HEADER = struct.Struct(">I")


def encode_frame(message: Mapping[str, Any]) -> bytes:
    """Length-prefix a message's canonical encoding."""
    # dict() so any Mapping can be sent; the canonical encoder is deliberately
    # strict about types and a frame is the wrong place to argue with it.
    body = canonical.encode(dict(message))
    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame of {len(body)} bytes exceeds the {MAX_FRAME_BYTES}-byte limit"
        )
    return _HEADER.pack(len(body)) + body


def write_frame(stream: BinaryIO, message: Mapping[str, Any]) -> None:
    """Write one frame and flush, so the other end is not left waiting."""
    stream.write(encode_frame(message))
    stream.flush()


def _read_exactly(stream: BinaryIO, count: int) -> bytes | None:
    """Read exactly `count` bytes, or None if the stream ended before any.

    A pipe hands over whatever has arrived, not whatever was asked for, so the
    loop is the point: without it the protocol works on a `BytesIO` in a test
    and desynchronises on a real fd under load.
    """
    parts: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if not parts:
                return None
            raise ProtocolError(
                f"stream ended {remaining} bytes short of a {count}-byte read"
            )
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one frame, or None at a clean end of stream."""
    header = _read_exactly(stream, HEADER_BYTES)
    if header is None:
        return None  # the cell stopped talking at a frame boundary: fine

    (length,) = _HEADER.unpack(header)
    if length == 0:
        raise ProtocolError("frame declares a zero-length body")
    if length > MAX_FRAME_BYTES:
        # Before any read of the body, on purpose.
        raise ProtocolError(
            f"frame declares {length} bytes, over the {MAX_FRAME_BYTES}-byte limit"
        )

    body = _read_exactly(stream, length)
    if body is None:
        raise ProtocolError(f"stream ended where a {length}-byte body was promised")

    try:
        message = canonical.decode(body)
    except Exception as error:
        raise ProtocolError(f"frame body is not canonical JSON: {error}") from error

    if not isinstance(message, dict):
        raise ProtocolError(
            f"frame body is a {type(message).__name__}, not an object"
        )
    if "m" not in message:
        raise ProtocolError("frame carries no 'm' kind")
    return message
