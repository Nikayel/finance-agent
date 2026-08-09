"""Milestone 4 — the framed protocol between the host and the sealed cell.

The gate is only as strong as the pipe underneath it. Every tick the host
reveals, and every order the strategy sends back, crosses this framing, and the
cell on the other end is hostile by assumption. Three properties carry the
module, and all three are easy to pass by accident:

* **The header is a claim, not a fact.** A cell that writes a four-byte length
  of ``0xFFFFFFFF`` must not make the host reserve four gigabytes. The length
  is checked *before* a single body byte is read, which is why the oversized
  tests assert on how far the stream advanced rather than only on the raised
  error.
* **A truncated frame is not a clean shutdown.** End of stream at a frame
  boundary means the peer finished; end of stream three bytes into a header, or
  one byte short of a body, means the peer died mid-sentence. Conflating the
  two turns a killed cell into a run that looks like it completed.
* **A pipe delivers bytes, not messages.** ``read()`` is free to return one
  byte, and on a real fd under load it does. The dribble streams below make
  that the normal case instead of the rare one.

The fakes here are small real objects — an ``io.RawIOBase`` that hands back one
byte at a time, a stream that raises if anything reads past the header — rather
than mocks, and the last section repeats the round trip over a real
``os.pipe()`` and a real child process, because a framing that only works on
``BytesIO`` has not been tested against the thing it ships on.
"""

from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from types import MappingProxyType
from typing import Any, BinaryIO

import pytest

import journals
from sbx.canonical import decode, encode
from sbx.errors import NotCanonicalError, ProtocolError, SbxError
from sbx.protocol import MAX_FRAME_BYTES, encode_frame, read_frame, write_frame

HEADER_BYTES = 4

# Every way a payload can try to break out of the framing: raw newlines (the
# ledger's delimiter), a NUL, the line separators that are invisible in an
# editor, and characters that leave the ASCII range.
NASTY_TEXT = (
    "a\nb\r\nc\td \"quoted\" back\\slash \xe9 \U0001f642 "
    "\u2028 \u2029 \x00 end"
)

MESSAGES = [
    pytest.param({"m": "hello"}, id="minimal"),
    pytest.param(
        {"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00", "events": []},
        id="empty-tick",
    ),
    pytest.param({"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00",
                  "events": [journals.TRADE, journals.GAP]}, id="tick-with-events"),
    pytest.param({"m": "order", "side": "BUY", "size": Decimal("0.00100000")},
                 id="decimal"),
    pytest.param({"m": "note", "text": NASTY_TEXT}, id="nasty-text"),
    pytest.param({"m": "flags", "ok": True, "bad": False, "nil": None},
                 id="json-scalars"),
    pytest.param({"m": "deep", "a": {"b": {"c": [1, 2, {"d": None}]}}}, id="nested"),
    pytest.param({"m": "done", "fills": [], "pnl": "0", "position": "0"}, id="report"),
]

FLOAT_PLACEMENTS = [
    pytest.param({"m": "order", "size": 0.001}, id="top-level"),
    pytest.param({"m": "tick", "prices": [1, 2.5]}, id="in-list"),
    pytest.param({"m": "done", "fills": [{"price": 118200.75}]}, id="nested"),
    pytest.param({"m": "done", "pnl": float("nan")}, id="nan"),
    pytest.param({"m": "done", "pnl": float("inf")}, id="infinity"),
]


class BodyRead(Exception):
    """Raised by :class:`Landmine` when a body byte is read that never should be."""


class Dribble(io.RawIOBase):
    """A readable stream that hands back at most ``limit`` bytes per read.

    Subclasses ``RawIOBase`` so that implementing ``readinto`` supplies ``read``
    too: the module under test may reach for either, and which one it picked is
    not part of the contract.
    """

    def __init__(self, data: bytes, limit: int = 1) -> None:
        super().__init__()
        self._data = data
        self._limit = limit
        self.consumed = 0
        self.reads = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self.reads += 1
        take = min(len(buffer), self._limit, len(self._data) - self.consumed)
        buffer[:take] = self._data[self.consumed : self.consumed + take]
        self.consumed += take
        return take


class Scripted(io.RawIOBase):
    """A readable stream that returns exactly the chunks it was handed, then EOF.

    Lets a test choose *where* the stream tears the frame — mid-header or
    mid-body — instead of hoping a size limit lands there.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__()
        self._chunks = [chunk for chunk in chunks if chunk]
        self.consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not self._chunks:
            return 0
        chunk = self._chunks[0]
        take = min(len(buffer), len(chunk))
        buffer[:take] = chunk[:take]
        remainder = chunk[take:]
        if remainder:
            self._chunks[0] = remainder
        else:
            self._chunks.pop(0)
        self.consumed += take
        return take


class Landmine(io.RawIOBase):
    """A stream holding only a header; reading past it is the failure.

    The oversized-length rule is about memory, not about the exception, so this
    turns "the host tried to read the body" into a loud error instead of
    something a test has to infer from a byte count.
    """

    def __init__(self, header: bytes) -> None:
        super().__init__()
        self._header = header
        self.consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.consumed >= len(self._header):
            raise BodyRead("read past the header of an over-long frame")
        take = min(len(buffer), len(self._header) - self.consumed)
        buffer[:take] = self._header[self.consumed : self.consumed + take]
        self.consumed += take
        return take


class Recorder(io.BytesIO):
    """A byte sink that remembers the order of its writes and flushes."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def write(self, data: Any) -> int:
        self.events.append("write")
        return super().write(data)

    def flush(self) -> None:
        self.events.append("flush")
        super().flush()


class Frozen(Mapping[str, Any]):
    """A Mapping that is emphatically not a dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def as_read(message: Mapping[str, Any]) -> dict[str, Any]:
    """What a message looks like once it has been through the wire.

    The canonical encoding is untagged, so Decimals arrive as strings. That
    asymmetry is inherited from :mod:`sbx.canonical` on purpose and is asserted
    directly further down.
    """
    return decode(encode(dict(message)))


def framed(body: bytes, declared: int | None = None) -> bytes:
    """A frame built by hand, so the header can lie about the body."""
    return struct.pack(">I", len(body) if declared is None else declared) + body


def message_of_exactly(total: int) -> dict[str, Any]:
    """A message whose canonical encoding is exactly ``total`` bytes."""
    padding = total - len(encode({"m": "blob", "pad": ""}))
    assert padding >= 0
    return {"m": "blob", "pad": "x" * padding}


@contextmanager
def pipe(buffering: int = -1) -> Iterator[tuple[BinaryIO, BinaryIO]]:
    """A real ``os.pipe()`` as a reader/writer pair, closed on the way out."""
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=buffering)
    writer = os.fdopen(write_fd, "wb", buffering=buffering)
    try:
        yield reader, writer  # type: ignore[misc]
    finally:
        writer.close()
        reader.close()


# ---------------------------------------------------------------------------
# the wire format — four big-endian bytes, then canonical JSON
# ---------------------------------------------------------------------------


def test_max_frame_bytes_is_one_mebibyte() -> None:
    assert MAX_FRAME_BYTES == 1 << 20
    assert isinstance(MAX_FRAME_BYTES, int)
    assert not isinstance(MAX_FRAME_BYTES, bool)


def test_protocol_errors_are_sbx_errors() -> None:
    # The CLI catches SbxError and prints one line; anything else tracebacks,
    # and a torn pipe is an expected failure, not a bug in sbx.
    assert issubclass(ProtocolError, SbxError)


@pytest.mark.parametrize("message", MESSAGES)
def test_a_frame_is_a_length_header_then_the_canonical_body(
    message: dict[str, Any]
) -> None:
    frame = encode_frame(message)
    body = encode(message)

    assert isinstance(frame, bytes)
    assert len(frame) == HEADER_BYTES + len(body)
    assert frame[HEADER_BYTES:] == body
    assert frame[:HEADER_BYTES] == len(body).to_bytes(HEADER_BYTES, "big")
    assert struct.unpack(">I", frame[:HEADER_BYTES])[0] == len(body)


def test_the_length_is_big_endian_not_native() -> None:
    # A body under 256 bytes hides the byte order: every ordering agrees when
    # only one byte is non-zero. This one is long enough to disagree.
    message = message_of_exactly(260)
    frame = encode_frame(message)

    assert frame[:HEADER_BYTES] == (260).to_bytes(HEADER_BYTES, "big")
    assert frame[:HEADER_BYTES] != (260).to_bytes(HEADER_BYTES, "little")
    assert frame[0] == 0 and frame[1] == 0  # the low byte is last, not first


def test_the_body_is_pure_ascii_whatever_the_payload_says() -> None:
    frame = encode_frame({"m": "note", "text": NASTY_TEXT})
    body = frame[HEADER_BYTES:]

    # Length framing does not care about newlines, but a human tailing the pipe
    # does, and canonical encoding is what keeps the body one printable line.
    assert max(body) < 128
    assert b"\n" not in body
    assert b"\r" not in body


def test_the_same_message_always_encodes_to_the_same_frame() -> None:
    first = {"m": "order", "side": "SELL", "size": Decimal("0.5"), "seq": 3}
    second = {"seq": 3, "size": Decimal("0.5"), "m": "order", "side": "SELL"}

    # Key order is the caller's accident; the frame is the record. Two dicts
    # that differ only in insertion order have to produce identical bytes.
    assert encode_frame(first) == encode_frame(second)
    assert encode_frame(first) == encode_frame(dict(first))


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(MappingProxyType, id="mappingproxy"),
        pytest.param(Frozen, id="custom-mapping"),
    ],
)
def test_encode_frame_accepts_any_mapping(wrap: Any) -> None:
    # canonical.encode only understands dict, so encode_frame has to normalise
    # the top level itself — the signature promises Mapping, not dict.
    message = {"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00"}
    assert encode_frame(wrap(message)) == encode_frame(message)


# ---------------------------------------------------------------------------
# round trips — a whole stream, read back message for message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", MESSAGES)
def test_a_frame_round_trips_through_a_byte_stream(message: dict[str, Any]) -> None:
    stream = io.BytesIO(encode_frame(message))

    received = read_frame(stream)

    assert isinstance(received, dict)
    assert received == as_read(message)
    assert read_frame(stream) is None


def test_decimals_arrive_as_strings() -> None:
    stream = io.BytesIO(
        encode_frame({"m": "order", "size": Decimal("0.00100000"),
                      "price": Decimal("118200.75")})
    )

    received = read_frame(stream)

    # Deliberate asymmetry, inherited from the untagged canonical encoding: the
    # wire carries digits and each side re-applies Decimal to its own known
    # fields. Do not "fix" this into a symmetric round trip.
    assert received == {"m": "order", "size": "0.00100000", "price": "118200.75"}
    assert isinstance(received["size"], str)
    assert not isinstance(received["size"], Decimal)
    # Trailing zeros are exchange precision, not noise, so they survive.
    assert Decimal(received["size"]) == Decimal("0.001")
    assert received["size"] == "0.00100000"


def test_nasty_text_survives_the_wire_unchanged() -> None:
    message = {"m": "note", "text": NASTY_TEXT}

    received = read_frame(io.BytesIO(encode_frame(message)))

    assert received == {"m": "note", "text": NASTY_TEXT}
    assert received["text"] == NASTY_TEXT


def test_two_frames_back_to_back_read_as_two_messages_in_order() -> None:
    first = {"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00", "events": []}
    second = {"m": "order", "side": "BUY", "size": Decimal("0.001")}
    stream = io.BytesIO(encode_frame(first) + encode_frame(second))

    assert read_frame(stream) == as_read(first)
    assert read_frame(stream) == as_read(second)
    assert read_frame(stream) is None


def test_many_frames_read_back_in_the_order_they_were_written() -> None:
    messages = [{"m": "tick", "index": index} for index in range(64)]
    stream = io.BytesIO(b"".join(encode_frame(message) for message in messages))

    received = list(iter(lambda: read_frame(stream), None))

    assert received == messages
    assert [message["index"] for message in received] == list(range(64))


def test_read_frame_consumes_exactly_one_frame() -> None:
    first = encode_frame({"m": "tick", "index": 0})
    second = encode_frame({"m": "tick", "index": 1})
    stream = io.BytesIO(first + second)

    read_frame(stream)

    # There is no buffer between calls, so over-reading by even a byte would
    # eat the head of the next frame and desync the two processes forever.
    assert stream.tell() == len(first)


def test_frames_of_wildly_different_sizes_interleave() -> None:
    messages = [
        {"m": "tick", "index": 0},
        message_of_exactly(100_000),
        {"m": "tick", "index": 1},
        {"m": "x"},  # a body barely longer than its own header
        {"m": "tick", "index": 2},
    ]
    stream = io.BytesIO(b"".join(encode_frame(message) for message in messages))

    assert list(iter(lambda: read_frame(stream), None)) == messages


# ---------------------------------------------------------------------------
# partial reads — a pipe delivers bytes, not messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 2, 3, 7])
@pytest.mark.parametrize("message", MESSAGES)
def test_a_dribbling_stream_yields_the_same_message(
    message: dict[str, Any], limit: int
) -> None:
    frame = encode_frame(message)
    stream = Dribble(frame, limit=limit)

    received = read_frame(stream)

    assert received == as_read(message)
    assert stream.consumed == len(frame)
    # Proof the stream really dribbled: no read handed over more than `limit`,
    # so the frame cannot have arrived in one piece.
    assert stream.reads >= len(frame) / limit


def test_a_header_split_across_reads_is_reassembled() -> None:
    message = {"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00", "events": []}
    frame = encode_frame(message)
    # The length itself arrives in three instalments, which is the case a
    # single `read(4)` with no loop gets wrong.
    stream = Scripted([frame[:1], frame[1:3], frame[3:4], frame[4:]])

    assert read_frame(stream) == as_read(message)
    assert stream.consumed == len(frame)


def test_a_body_split_across_reads_is_reassembled() -> None:
    message = message_of_exactly(4096)
    frame = encode_frame(message)
    body = frame[HEADER_BYTES:]
    stream = Scripted([frame[:HEADER_BYTES], body[:1], body[1:2000], body[2000:]])

    assert read_frame(stream) == message
    assert stream.consumed == len(frame)


def test_a_header_and_body_arriving_in_one_read_is_also_fine() -> None:
    message = {"m": "tick", "index": 9}
    frame = encode_frame(message)
    stream = Scripted([frame])

    assert read_frame(stream) == message


def test_two_frames_survive_a_one_byte_at_a_time_stream() -> None:
    first = {"m": "tick", "index": 0}
    second = {"m": "order", "side": "SELL", "size": Decimal("2")}
    stream = Dribble(encode_frame(first) + encode_frame(second), limit=1)

    assert read_frame(stream) == as_read(first)
    assert read_frame(stream) == as_read(second)
    assert read_frame(stream) is None


def test_a_dribbling_stream_that_stops_mid_body_is_a_torn_frame() -> None:
    frame = encode_frame({"m": "tick", "index": 0})
    stream = Dribble(frame[:-1], limit=1)

    with pytest.raises(ProtocolError):
        read_frame(stream)


# ---------------------------------------------------------------------------
# end of stream — three outcomes, and only one of them is a clean shutdown
# ---------------------------------------------------------------------------


def test_clean_eof_torn_header_and_torn_body_are_three_distinct_outcomes() -> None:
    """The whole point of the section, in one place.

    A peer that closed the pipe between frames is done; a peer that closed it
    part-way through one was killed. Treating the second as the first turns a
    contained strategy into a run that appears to have finished.
    """
    frame = encode_frame({"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00"})

    assert read_frame(io.BytesIO(b"")) is None

    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(frame[:2]))

    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(frame[:-1]))


def test_an_empty_stream_is_a_clean_eof() -> None:
    stream = io.BytesIO(b"")

    assert read_frame(stream) is None
    assert read_frame(stream) is None  # asking again changes nothing


def test_eof_after_the_last_frame_is_clean() -> None:
    stream = io.BytesIO(encode_frame({"m": "done", "pnl": "0"}))

    assert read_frame(stream) is not None
    assert read_frame(stream) is None
    assert read_frame(stream) is None


@pytest.mark.parametrize("kept", [1, 2, 3], ids=["one-byte", "two-bytes", "three"])
def test_eof_inside_the_header_is_a_torn_frame(kept: int) -> None:
    frame = encode_frame({"m": "tick", "index": 0})

    with pytest.raises(ProtocolError) as caught:
        read_frame(io.BytesIO(frame[:kept]))

    assert str(caught.value).strip() != ""


@pytest.mark.parametrize(
    "make_prefix",
    [
        pytest.param(lambda frame: frame[:HEADER_BYTES], id="header-only"),
        pytest.param(lambda frame: frame[: HEADER_BYTES + 1], id="one-body-byte"),
        pytest.param(lambda frame: frame[: len(frame) // 2], id="half-a-body"),
        pytest.param(lambda frame: frame[:-1], id="one-byte-short"),
    ],
)
def test_eof_inside_the_body_is_a_torn_frame(make_prefix: Any) -> None:
    # "header-only" is the trap: nothing of the body arrived, which looks like a
    # boundary if you only check whether the *body* read came back empty.
    frame = encode_frame({"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00"})

    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(make_prefix(frame)))


def test_a_torn_second_frame_does_not_retract_the_first() -> None:
    first = {"m": "tick", "index": 0}
    torn = encode_frame(first) + encode_frame({"m": "tick", "index": 1})[:-2]
    stream = io.BytesIO(torn)

    assert read_frame(stream) == first

    with pytest.raises(ProtocolError):
        read_frame(stream)


# ---------------------------------------------------------------------------
# the declared length — a number written by the untrusted side
# ---------------------------------------------------------------------------

OVERSIZED = [
    pytest.param(MAX_FRAME_BYTES + 1, id="one-over"),
    pytest.param(MAX_FRAME_BYTES * 2, id="double"),
    pytest.param(1 << 30, id="a-gibibyte"),
    pytest.param(0xFFFFFFFF, id="every-bit-set"),
]


@pytest.mark.parametrize("declared", OVERSIZED)
def test_an_over_long_declared_length_is_refused(declared: int) -> None:
    with pytest.raises(ProtocolError) as caught:
        read_frame(io.BytesIO(framed(b'{"m":"tick"}', declared=declared)))

    assert str(caught.value).strip() != ""


@pytest.mark.parametrize("declared", OVERSIZED)
def test_an_over_long_length_is_refused_before_the_body_is_read(declared: int) -> None:
    stream = io.BytesIO(framed(b'{"m":"tick"}', declared=declared))

    with pytest.raises(ProtocolError):
        read_frame(stream)

    # The rule is about memory, not about the exception: a host that reads
    # first and checks afterwards has already allocated whatever the cell asked
    # for. Nothing past the header may have been consumed.
    assert stream.tell() == HEADER_BYTES


@pytest.mark.parametrize("declared", OVERSIZED)
def test_nothing_reads_the_body_of_an_over_long_frame(declared: int) -> None:
    # Same rule, stated so that a violation is an explicit BodyRead rather than
    # a byte count a reader has to interpret.
    stream = Landmine(struct.pack(">I", declared))

    with pytest.raises(ProtocolError):
        read_frame(stream)

    assert stream.consumed == HEADER_BYTES


def test_a_frame_of_exactly_the_maximum_size_is_allowed() -> None:
    message = message_of_exactly(MAX_FRAME_BYTES)
    frame = encode_frame(message)
    assert len(frame) == HEADER_BYTES + MAX_FRAME_BYTES

    # The limit is a ceiling, not a fence one byte below it.
    assert read_frame(io.BytesIO(frame)) == message


def test_a_frame_one_byte_over_the_maximum_is_refused() -> None:
    # Framed by hand rather than through encode_frame: whether the *writer* also
    # refuses to build an over-long frame is unspecified, and a hostile cell
    # would not be using encode_frame anyway. This pins the reader.
    frame = framed(encode(message_of_exactly(MAX_FRAME_BYTES + 1)))
    assert len(frame) == HEADER_BYTES + MAX_FRAME_BYTES + 1

    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(frame))


def test_a_declared_length_of_zero_is_refused() -> None:
    # Not a clean EOF and not an empty message: there is no such thing as a
    # zero-byte body, so this is a corrupt header.
    stream = io.BytesIO(framed(b"", declared=0))

    with pytest.raises(ProtocolError):
        read_frame(stream)


def test_a_zero_length_header_is_not_mistaken_for_end_of_stream() -> None:
    stream = io.BytesIO(framed(b"", declared=0) + encode_frame({"m": "tick"}))

    with pytest.raises(ProtocolError):
        read_frame(stream)


# ---------------------------------------------------------------------------
# the body — canonical JSON, and a dict with an "m"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json at all", id="garbage"),
        pytest.param(b'{"m":"tick"', id="truncated-object"),
        pytest.param(b'{"m":"tick",}', id="trailing-comma"),
        pytest.param(b"}broken{", id="reversed-braces"),
        pytest.param(b"   ", id="whitespace-only"),
        pytest.param(b"\xff\xfe{}", id="not-utf-8"),
    ],
)
def test_a_body_that_will_not_parse_is_refused(body: bytes) -> None:
    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(framed(body)))


def test_a_body_holding_a_bare_float_is_refused() -> None:
    # Ambiguous in the spec — "not valid canonical JSON" says ProtocolError,
    # while canonical.decode raises NotCanonicalError of its own accord — so
    # this only pins the part both readings agree on: an expected failure the
    # CLI can print, never a stray ValueError.
    with pytest.raises(SbxError):
        read_frame(io.BytesIO(framed(b'{"m":"order","size":0.001}')))


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"[1,2]", id="list"),
        pytest.param(b'[{"m":"tick"}]', id="list-of-messages"),
        pytest.param(b'"tick"', id="string"),
        pytest.param(b"42", id="int"),
        pytest.param(b"null", id="null"),
        pytest.param(b"true", id="bool"),
    ],
)
def test_a_body_that_is_not_a_dict_is_refused(body: bytes) -> None:
    # Valid JSON, correctly framed, and still not a message. A reader that only
    # checks the framing hands a list to code that will index it by name.
    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(framed(body)))


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({}, id="empty"),
        pytest.param({"now": "2026-07-29T14:30:01.250000+00:00"}, id="tick-without-m"),
        pytest.param({"M": "tick"}, id="wrong-case"),
        pytest.param({"msg": "tick"}, id="near-miss"),
        pytest.param({"kind": "tick"}, id="ledger-key"),
    ],
)
def test_a_message_without_an_m_key_is_refused(message: dict[str, Any]) -> None:
    # Encoded by hand rather than through encode_frame: whether the *writer*
    # also refuses these is unspecified, and this pins only the reader.
    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(framed(encode(message))))


def test_an_m_key_is_all_a_message_needs() -> None:
    assert read_frame(io.BytesIO(framed(encode({"m": "ping"})))) == {"m": "ping"}


# ---------------------------------------------------------------------------
# write_frame — write and flush, or write nothing at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", MESSAGES)
def test_write_frame_writes_exactly_the_encoded_frame(message: dict[str, Any]) -> None:
    stream = io.BytesIO()

    assert write_frame(stream, message) is None
    assert stream.getvalue() == encode_frame(message)


def test_write_frame_flushes_after_writing() -> None:
    stream = Recorder()

    write_frame(stream, {"m": "tick", "index": 0})

    assert "write" in stream.events
    # Order matters: a flush before the last write leaves the frame sitting in
    # the buffer, and the peer blocks on a message that was "sent".
    assert stream.events[-1] == "flush"
    assert stream.getvalue() == encode_frame({"m": "tick", "index": 0})


def test_write_frame_really_flushes_a_buffered_pipe() -> None:
    message = {"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00"}
    read_fd, write_fd = os.pipe()
    # A buffered writer is the realistic case — the cell's stdout is one — and
    # its buffer is far bigger than a tick, so nothing arrives without a flush.
    writer = os.fdopen(write_fd, "wb")
    try:
        write_frame(writer, message)

        # Non-blocking, so an unflushed frame fails the test instead of hanging
        # the suite on a read that will never be satisfied.
        os.set_blocking(read_fd, False)
        try:
            delivered = os.read(read_fd, 1 << 16)
        except BlockingIOError:
            pytest.fail("write_frame returned with the frame still in the buffer")
        assert delivered == encode_frame(message)
    finally:
        writer.close()
        os.close(read_fd)


def test_write_frame_appends_rather_than_rewrites() -> None:
    first = {"m": "tick", "index": 0}
    second = {"m": "tick", "index": 1}
    stream = io.BytesIO()

    write_frame(stream, first)
    prefix = stream.getvalue()
    write_frame(stream, second)

    assert stream.getvalue() == prefix + encode_frame(second)
    assert stream.getvalue() == encode_frame(first) + encode_frame(second)


@pytest.mark.parametrize("message", FLOAT_PLACEMENTS)
def test_a_float_anywhere_in_a_message_is_refused(message: dict[str, Any]) -> None:
    with pytest.raises(NotCanonicalError):
        encode_frame(message)


@pytest.mark.parametrize("message", FLOAT_PLACEMENTS)
def test_a_refused_message_puts_no_bytes_on_the_wire(message: dict[str, Any]) -> None:
    stream = io.BytesIO()
    write_frame(stream, {"m": "tick", "index": 0})
    before = stream.getvalue()

    with pytest.raises(NotCanonicalError):
        write_frame(stream, message)

    # Half a frame is worse than no frame: the length header would already have
    # been read by the peer, and every later frame would be misaligned.
    assert stream.getvalue() == before


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({1, 2}, id="set"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param(Decimal("NaN"), id="decimal-nan"),
        pytest.param(object(), id="object"),
    ],
)
def test_values_with_no_canonical_form_never_reach_the_wire(value: Any) -> None:
    stream = io.BytesIO()

    with pytest.raises(NotCanonicalError):
        write_frame(stream, {"m": "tick", "value": value})

    assert stream.getvalue() == b""


# ---------------------------------------------------------------------------
# real file descriptors — a pipe, and a child process on the other end
#
# No fakes in this section, on purpose. Short reads, buffering and EOF-on-close
# are properties of the fd the protocol actually ships on; a suite that only
# ever saw BytesIO has proved the framing against a stream that never once
# returned fewer bytes than it was asked for.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("buffering", [-1, 0], ids=["buffered", "unbuffered"])
def test_frames_round_trip_through_a_real_pipe(buffering: int) -> None:
    messages = [
        {"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00", "events": []},
        {"m": "order", "side": "BUY", "size": Decimal("0.001")},
        {"m": "note", "text": NASTY_TEXT},
    ]

    with pipe(buffering=buffering) as (reader, writer):
        for message in messages:
            write_frame(writer, message)
        writer.close()  # EOF only arrives when the write end is gone

        assert [read_frame(reader) for _ in messages] == [
            as_read(message) for message in messages
        ]
        assert read_frame(reader) is None


def test_closing_the_write_end_between_frames_is_a_clean_eof() -> None:
    with pipe() as (reader, writer):
        write_frame(writer, {"m": "done", "pnl": "0"})
        writer.close()

        assert read_frame(reader) == {"m": "done", "pnl": "0"}
        assert read_frame(reader) is None


def test_a_writer_that_dies_mid_frame_leaves_a_torn_frame() -> None:
    frame = encode_frame({"m": "tick", "now": "2026-07-29T14:30:01.250000+00:00"})

    with pipe() as (reader, writer):
        # Exactly what a cell killed by the watchdog leaves behind: some of a
        # frame, then a closed fd. The host must not read that as "finished".
        writer.write(frame[:-3])
        writer.flush()
        writer.close()

        with pytest.raises(ProtocolError):
            read_frame(reader)


def test_a_header_torn_by_a_dying_writer_is_not_a_clean_eof() -> None:
    frame = encode_frame({"m": "tick", "index": 0})

    with pipe() as (reader, writer):
        writer.write(frame[:2])
        writer.flush()
        writer.close()

        with pytest.raises(ProtocolError):
            read_frame(reader)


CHILD_WRITES_FRAMES = """\
import sys

from sbx.protocol import write_frame

out = sys.stdout.buffer
for index in range(5):
    write_frame(out, {"m": "tick", "index": index})
# Comfortably past the 64 KiB pipe buffer, so the parent has to read while the
# child is still writing and every read comes back short.
write_frame(out, {"m": "blob", "pad": "x" * 200000})
write_frame(out, {"m": "done", "pnl": "0"})
"""


def test_frames_from_a_child_process_arrive_intact_and_in_order() -> None:
    expected = [{"m": "tick", "index": index} for index in range(5)]
    expected.append({"m": "blob", "pad": "x" * 200000})
    expected.append({"m": "done", "pnl": "0"})

    with subprocess.Popen(
        [sys.executable, "-c", CHILD_WRITES_FRAMES],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        assert child.stdout is not None
        received = list(iter(lambda: read_frame(child.stdout), None))
        stderr = child.stderr.read() if child.stderr else b""

    assert child.returncode == 0, stderr.decode("utf-8", "replace")
    assert received == expected
    assert [message["m"] for message in received] == (
        ["tick"] * 5 + ["blob", "done"]
    )
