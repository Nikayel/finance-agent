"""Milestone 2 — canonical encoding and content addressing.

The ledger's promise is ``(data, code, seed) -> result``, re-runnable to
identical bytes. That promise is only as good as this module: the same
logical value must serialise to the same bytes on any machine, in any
process, forever. These tests are that promise written down, including the
deliberate sharp edges (significant trailing zeros, untagged Decimals) that
someone will otherwise "fix" in six months.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from sbx.canonical import (
    decimal_text,
    decode,
    encode,
    encode_line,
    hash_bytes,
    hash_file,
    hash_object,
)
from sbx.errors import NotCanonicalError, SbxError

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

# Pinned so a swap to some other 64-hex-character digest cannot pass unnoticed.
SHA256_OF_NOTHING = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Every way a payload can try to smuggle a raw newline, a quote, a backslash
# or a non-ASCII byte past the JSONL framing.
NASTY_TEXT = 'a\nb\r\nc\td "quoted" back\\slash é 🙂 \u2028 \u2029 \x00 end'


class Opaque:
    """A perfectly ordinary object with no canonical meaning."""

    def __init__(self) -> None:
        self.price = 1


def nest(depth: int) -> object:
    """Build alternating dict/list nesting ``depth`` levels deep."""
    node: object = {"leaf": 1}
    for level in range(depth):
        node = {"level": level, "child": [node]}
    return node


# ---------------------------------------------------------------------------
# encoding basics — shapes, allowed types, and decode as the inverse
# ---------------------------------------------------------------------------


def test_encode_returns_bytes_with_no_trailing_newline() -> None:
    encoded = encode({"a": 1})
    assert isinstance(encoded, bytes)
    assert not encoded.endswith(b"\n")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param({}, b"{}", id="empty-dict"),
        pytest.param([], b"[]", id="empty-list"),
        pytest.param((), b"[]", id="empty-tuple"),
        pytest.param(None, b"null", id="none"),
        pytest.param(0, b"0", id="zero"),
        pytest.param(-17, b"-17", id="negative-int"),
        pytest.param("", b'""', id="empty-string"),
        pytest.param("ok", b'"ok"', id="string"),
        pytest.param([1, 2, 3], b"[1,2,3]", id="list"),
        pytest.param({"a": 1}, b'{"a":1}', id="dict"),
    ],
)
def test_minimal_values_encode_to_the_obvious_bytes(
    value: object, expected: bytes
) -> None:
    assert encode(value) == expected


def test_booleans_are_not_integers() -> None:
    assert encode(True) == b"true"
    assert encode(False) == b"false"
    assert encode(1) == b"1"
    assert encode(0) == b"0"
    # bool is a subclass of int; the encoder must not flatten it into one.
    assert encode(True) != encode(1)
    assert encode(False) != encode(0)


def test_output_carries_no_whitespace() -> None:
    encoded = encode({"b": [1, 2, {"a": "x"}], "a": None})
    for whitespace in (b" ", b"\t", b"\n", b"\r"):
        assert whitespace not in encoded
    assert encoded == b'{"a":null,"b":[1,2,{"a":"x"}]}'


def test_output_is_pure_ascii() -> None:
    encoded = encode({"é": "🙂", "k": NASTY_TEXT})
    assert max(encoded) < 128
    encoded.decode("ascii")  # would raise if any byte escaped the escaping
    assert encode("é") == b'"\\u00e9"'


def test_tuples_encode_exactly_like_lists() -> None:
    assert encode((1, 2, 3)) == encode([1, 2, 3])
    assert encode({"k": (1, ("a", 2))}) == encode({"k": [1, ["a", 2]]})
    assert hash_object((1, 2, 3)) == hash_object([1, 2, 3])


def test_large_integers_survive_exactly() -> None:
    big = 2**63 + 12345
    assert encode(big) == b"9223372036854788153"
    # No float coercion anywhere: the digits appear literally, not as 9.22e18.
    assert str(big).encode("ascii") in encode({"qty": big})
    assert decode(encode({"qty": big}))["qty"] == big
    assert encode(-(2**200)) == str(-(2**200)).encode("ascii")


def test_deep_nesting_encodes_and_round_trips() -> None:
    deep = nest(50)
    encoded = encode(deep)
    assert encoded.startswith(b'{"child":')
    assert decode(encoded) == deep


def test_decode_accepts_bytes_and_str() -> None:
    encoded = encode({"a": [1, "x", None, True]})
    assert decode(encoded) == decode(encoded.decode("ascii"))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({}, id="empty-dict"),
        pytest.param([], id="empty-list"),
        pytest.param(None, id="none"),
        pytest.param(True, id="bool"),
        pytest.param(-5, id="int"),
        pytest.param(2**70, id="big-int"),
        pytest.param(NASTY_TEXT, id="nasty-text"),
        pytest.param({"b": 1, "a": [1, {"z": None, "y": [True, False]}]}, id="nested"),
    ],
)
def test_decode_of_encode_returns_an_equal_value(value: object) -> None:
    assert decode(encode(value)) == value


def test_tuples_come_back_as_lists() -> None:
    assert decode(encode({"k": (1, 2)})) == {"k": [1, 2]}


def test_decode_returns_decimals_as_strings() -> None:
    # Deliberate asymmetry: the encoding is untagged, so each record schema
    # re-applies Decimal() to its own known fields. Do not "fix" this.
    restored = decode(encode({"px": Decimal("1.50")}))
    assert restored == {"px": "1.50"}
    assert isinstance(restored["px"], str)
    assert not isinstance(restored["px"], Decimal)


def test_decode_rejects_non_json_input() -> None:
    with pytest.raises((NotCanonicalError, ValueError)):
        decode(b'{"a": ')


# ---------------------------------------------------------------------------
# rejection — everything that must never reach the wire
# ---------------------------------------------------------------------------

BAD_VALUES = [
    pytest.param(1.0, id="float"),
    pytest.param(0.0, id="float-zero"),
    pytest.param(-0.0, id="float-negative-zero"),
    pytest.param(2.5, id="float-fraction"),
    pytest.param(1e-8, id="float-tiny"),
    pytest.param(float("nan"), id="float-nan"),
    pytest.param(float("inf"), id="float-inf"),
    pytest.param(float("-inf"), id="float-neg-inf"),
    pytest.param({1, 2}, id="set"),
    pytest.param(frozenset({1, 2}), id="frozenset"),
    pytest.param(b"raw", id="bytes"),
    pytest.param(bytearray(b"raw"), id="bytearray"),
    pytest.param(datetime(2024, 1, 1, 12, 30), id="datetime"),
    pytest.param(date(2024, 1, 1), id="date"),
    pytest.param(time(12, 30), id="time"),
    pytest.param(Decimal("NaN"), id="decimal-nan"),
    pytest.param(Decimal("sNaN"), id="decimal-snan"),
    pytest.param(Decimal("Infinity"), id="decimal-inf"),
    pytest.param(Decimal("-Infinity"), id="decimal-neg-inf"),
    pytest.param(complex(1, 2), id="complex"),
    pytest.param(range(3), id="range"),
    pytest.param(Opaque(), id="arbitrary-object"),
    pytest.param(object(), id="bare-object"),
]

# The same offending value, buried at every depth a real record could hide it.
NESTINGS: list[object] = [
    pytest.param(lambda v: v, id="bare"),
    pytest.param(lambda v: [v], id="in-list"),
    pytest.param(lambda v: (v,), id="in-tuple"),
    pytest.param(lambda v: {"k": v}, id="in-dict"),
    pytest.param(lambda v: {"a": [{"b": [v]}]}, id="deeply-nested"),
    pytest.param(lambda v: {"a": 1, "b": [2, {"c": ["x", v]}]}, id="beside-good-data"),
]


@pytest.mark.parametrize("wrap", NESTINGS)
@pytest.mark.parametrize("bad", BAD_VALUES)
def test_encode_rejects_uncanonical_values_at_any_depth(
    bad: object, wrap: Callable[[object], object]
) -> None:
    with pytest.raises(NotCanonicalError):
        encode(wrap(bad))


BAD_KEYS = [
    pytest.param(1, id="int-key"),
    pytest.param(0, id="zero-key"),
    pytest.param(True, id="true-key"),
    pytest.param(False, id="false-key"),
    pytest.param(None, id="none-key"),
    pytest.param(1.0, id="float-key"),
    pytest.param(Decimal("1"), id="decimal-key"),
    pytest.param((1, 2), id="tuple-key"),
    pytest.param(2**64, id="big-int-key"),
]


@pytest.mark.parametrize("key", BAD_KEYS)
def test_encode_rejects_non_string_dict_keys(key: object) -> None:
    # json would silently coerce int/bool/None keys to strings, which makes
    # {1: "a"} and {"1": "a"} indistinguishable once written.
    with pytest.raises(NotCanonicalError):
        encode({key: "value"})


@pytest.mark.parametrize("key", BAD_KEYS)
def test_encode_rejects_non_string_dict_keys_when_nested(key: object) -> None:
    with pytest.raises(NotCanonicalError):
        encode({"a": [{"b": {key: "value"}}]})


def test_string_keys_of_any_shape_are_fine() -> None:
    assert encode({"": 1, "a b": 2, "\n": 3}) == b'{"":1,"\\n":3,"a b":2}'


def test_encode_line_and_hash_object_reject_too() -> None:
    # Rejection lives in the encoder, so every public entry point inherits it.
    with pytest.raises(NotCanonicalError):
        encode_line({"a": [{"b": 1.0}]})
    with pytest.raises(NotCanonicalError):
        hash_object({"a": [{"b": 1.0}]})


def test_not_canonical_error_is_an_sbx_error() -> None:
    assert issubclass(NotCanonicalError, SbxError)
    with pytest.raises(NotCanonicalError) as caught:
        encode({"px": 1.0})
    assert isinstance(caught.value, SbxError)
    # The CLI prints these at users, so a bare raise is not good enough.
    assert str(caught.value).strip() != ""


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Decimal("NaN"), id="nan"),
        pytest.param(Decimal("-NaN"), id="negative-nan"),
        pytest.param(Decimal("sNaN"), id="snan"),
        pytest.param(Decimal("Infinity"), id="infinity"),
        pytest.param(Decimal("-Infinity"), id="negative-infinity"),
    ],
)
def test_decimal_text_rejects_non_finite_decimals(value: Decimal) -> None:
    # decimal_text is the only thing that turns a Decimal into wire text, so
    # it — not just encode — has to refuse "NaN" and "Infinity".
    with pytest.raises(NotCanonicalError):
        decimal_text(value)


def test_circular_references_raise() -> None:
    looped: dict[str, object] = {"a": 1}
    looped["self"] = looped
    with pytest.raises((NotCanonicalError, ValueError)):
        encode(looped)

    branch: list[object] = [1]
    branch.append({"child": branch})
    with pytest.raises((NotCanonicalError, ValueError)):
        encode(branch)


# ---------------------------------------------------------------------------
# decimal_text — plain notation, significant trailing zeros, no ambient state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(Decimal("0"), "0", id="zero"),
        pytest.param(Decimal("1"), "1", id="one"),
        pytest.param(Decimal("1.5"), "1.5", id="simple"),
        pytest.param(Decimal("-1.5"), "-1.5", id="negative"),
        pytest.param(Decimal("1E+2"), "100", id="positive-exponent"),
        pytest.param(Decimal("1E-3"), "0.001", id="negative-exponent"),
        pytest.param(Decimal("-2.5E+3"), "-2500", id="negative-with-exponent"),
        pytest.param(Decimal("0E-8"), "0.00000000", id="zero-with-scale"),
        pytest.param(Decimal("0.00420000"), "0.00420000", id="trailing-zeros"),
        pytest.param(
            Decimal("123456789012345678901234567890.000000001"),
            "123456789012345678901234567890.000000001",
            id="high-precision",
        ),
    ],
)
def test_decimal_text_is_plain_positional(value: Decimal, expected: str) -> None:
    result = decimal_text(value)
    assert isinstance(result, str)
    assert result == expected


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1E+2"),
        Decimal("1E-3"),
        Decimal("1E+30"),
        Decimal("1E-30"),
        Decimal("-4.2E+9"),
        Decimal("0E-8"),
    ],
)
def test_decimal_text_never_uses_scientific_notation(value: Decimal) -> None:
    text = decimal_text(value)
    assert "e" not in text.lower()
    assert Decimal(text) == value


def test_trailing_zeros_are_significant() -> None:
    # Exchange precision is data: "0.00420000" is a different observation from
    # "0.0042", and collapsing them would silently rewrite the journal.
    assert decimal_text(Decimal("0.00420000")) == "0.00420000"
    assert decimal_text(Decimal("0.00420000")) != decimal_text(Decimal("0.0042"))
    assert decimal_text(Decimal("1.50")) == "1.50"
    assert decimal_text(Decimal("100.00")) == "100.00"


def test_equal_decimals_with_different_scales_encode_differently() -> None:
    # Load-bearing and intentional: these two Decimals compare equal, but they
    # are not the same recorded fact, so they must not share a content hash.
    assert Decimal("1.50") == Decimal("1.5")
    assert encode(Decimal("1.50")) != encode(Decimal("1.5"))
    assert hash_object(Decimal("1.50")) != hash_object(Decimal("1.5"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(Decimal("-0"), "0", id="negative-zero"),
        pytest.param(Decimal("-0.00"), "0.00", id="negative-zero-scaled"),
        pytest.param(Decimal("-0E-8"), "0.00000000", id="negative-zero-exponent"),
        pytest.param(Decimal("0.00"), "0.00", id="positive-zero-scaled"),
    ],
)
def test_negative_zero_is_normalised(value: Decimal, expected: str) -> None:
    # Sign dropped (there is no negative zero quantity), precision kept.
    text = decimal_text(value)
    assert not text.startswith("-")
    assert text == expected


def test_negative_zero_and_zero_share_one_encoding() -> None:
    assert encode(Decimal("-0")) == encode(Decimal("0")) == b'"0"'
    assert hash_object(Decimal("-0.00")) == hash_object(Decimal("0.00"))


def test_decimal_encodes_as_an_untagged_json_string() -> None:
    assert encode(Decimal("1.50")) == b'"1.50"'
    assert encode({"px": Decimal("1E-3")}) == b'{"px":"0.001"}'
    assert encode([Decimal("-1.5"), Decimal("100.00")]) == b'["-1.5","100.00"]'


def test_decimal_text_ignores_ambient_context_precision() -> None:
    value = Decimal("123456789.123456789012345678901234567890")
    expected = decimal_text(value)
    with decimal.localcontext() as ctx:
        ctx.prec = 3
        ctx.rounding = decimal.ROUND_UP
        assert decimal_text(value) == expected
        assert encode(value) == b'"' + expected.encode("ascii") + b'"'
    assert decimal_text(value) == expected


def test_decimal_text_ignores_a_mutated_global_context() -> None:
    value = Decimal("0.00420000")
    context = decimal.getcontext()
    previous_prec, previous_rounding = context.prec, context.rounding
    try:
        context.prec = 3
        context.rounding = decimal.ROUND_UP
        # Any implementation reaching for normalize(), +value or quantize()
        # would round here — and would also eat the trailing zeros.
        assert decimal_text(value) == "0.00420000"
        assert decimal_text(Decimal("1.23456789")) == "1.23456789"
    finally:
        context.prec = previous_prec
        context.rounding = previous_rounding


# ---------------------------------------------------------------------------
# determinism — same logical value, same bytes, everywhere
# ---------------------------------------------------------------------------


def test_insertion_order_does_not_matter() -> None:
    forward = {"alpha": 1, "beta": 2, "gamma": 3, "delta": 4}
    backward = {"delta": 4, "gamma": 3, "beta": 2, "alpha": 1}
    assert encode(forward) == encode(backward)
    assert hash_object(forward) == hash_object(backward)


def test_keys_sort_at_every_level() -> None:
    assert encode({"b": {"z": 1, "a": 2}, "a": 1}) == b'{"a":1,"b":{"a":2,"z":1}}'
    scrambled = {"b": [{"y": 0, "x": {"n": 1, "m": 2}}], "a": 1}
    ordered = {"a": 1, "b": [{"x": {"m": 2, "n": 1}, "y": 0}]}
    assert encode(scrambled) == encode(ordered)


def test_keys_sort_by_codepoint_of_the_unescaped_string() -> None:
    # "z" (U+007A) sorts before "é" (U+00E9) — sorting happens on the real
    # string, not on its \uXXXX escape.
    assert encode({"é": 1, "z": 2}) == b'{"z":2,"\\u00e9":1}'
    assert encode({"b": 1, "A": 2, "a": 3}) == b'{"A":2,"a":3,"b":1}'


def test_encoding_is_stable_across_repeated_calls() -> None:
    payload = {"b": [1, {"d": Decimal("1.50"), "c": None}], "a": "x"}
    first = encode(payload)
    assert all(encode(payload) == first for _ in range(20))


DIGEST_PROGRAM = (
    "import json, sys\n"
    "from sbx.canonical import hash_object\n"
    "sys.stdout.write(hash_object(json.load(sys.stdin)))\n"
)


def digest_in_subprocess(payload: object, hashseed: str) -> str:
    env = dict(os.environ, PYTHONHASHSEED=hashseed)
    result = subprocess.run(
        [sys.executable, "-c", DIGEST_PROGRAM],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_encoding_is_identical_under_different_hash_seeds() -> None:
    # The one test that catches hash randomisation leaking into output: a set
    # or a hash-ordered iteration anywhere in the encoder shows up here and
    # nowhere else.
    keys = [f"key-{i:03d}" for i in range(200)]
    payload = {
        key: {"z": index, "a": [key, "x"], "m": None}
        for index, key in enumerate(reversed(keys))
    }

    first = digest_in_subprocess(payload, "0")
    second = digest_in_subprocess(payload, "12345")

    assert HEX64.match(first)
    assert first == second
    assert first == hash_object(payload)


# ---------------------------------------------------------------------------
# hashing — content addressing over the canonical bytes
# ---------------------------------------------------------------------------


def test_hash_bytes_is_sha256() -> None:
    assert hash_bytes(b"") == hashlib.sha256(b"").hexdigest()
    assert hash_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()
    assert hash_bytes(b"") == SHA256_OF_NOTHING


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param({}, id="empty-dict"),
        pytest.param({"px": Decimal("1.50"), "qty": 3}, id="record"),
        pytest.param([1, "x", True, {"a": [None]}], id="nested"),
    ],
)
def test_hash_object_is_the_hash_of_the_canonical_bytes(value: object) -> None:
    assert hash_object(value) == hash_bytes(encode(value))
    assert HEX64.match(hash_object(value))


def test_different_objects_hash_differently() -> None:
    assert hash_object({"a": 1}) != hash_object({"a": "1"})
    assert hash_object({"a": 1}) != hash_object({"a": 2})
    assert hash_object([1, 2]) != hash_object([2, 1])
    assert hash_object(True) != hash_object(1)


def test_hash_file_matches_hash_bytes(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    payload = b'{"a":1}\n{"b":2}\n'
    path.write_bytes(payload)
    assert hash_file(path) == hash_bytes(payload)
    assert hash_file(str(path)) == hash_bytes(payload)


def test_hash_file_handles_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert hash_file(path) == hash_bytes(b"")
    assert hash_file(path) == SHA256_OF_NOTHING


def test_hash_file_handles_a_file_larger_than_any_chunk(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    payload = bytes(range(256)) * 4096 * 5  # ~5 MiB
    path.write_bytes(payload)
    assert hash_file(path) == hash_bytes(payload)
    assert hash_file(path) == hashlib.sha256(payload).hexdigest()


def test_hash_file_reads_past_the_first_chunk(tmp_path: Path) -> None:
    # Streaming done wrong (hash the first read() and stop) still passes the
    # small-file tests; only a late-byte difference catches it.
    payload = bytearray(b"x" * (3 * 1024 * 1024))
    head = tmp_path / "head.bin"
    head.write_bytes(bytes(payload))
    payload[-1] = ord("y")
    tail = tmp_path / "tail.bin"
    tail.write_bytes(bytes(payload))
    assert hash_file(head) != hash_file(tail)


def test_hash_file_of_encoded_bytes_matches_hash_object(tmp_path: Path) -> None:
    record = {"run": "run-0001", "px": Decimal("1.50")}
    path = tmp_path / "record.json"
    path.write_bytes(encode(record))
    assert hash_file(path) == hash_object(record)


# ---------------------------------------------------------------------------
# framing — one record, one line, no matter what the payload contains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param({}, id="empty-dict"),
        pytest.param({"note": NASTY_TEXT}, id="nasty-text"),
        pytest.param({"px": Decimal("0.00420000")}, id="decimal"),
        pytest.param([1, [2, [3]]], id="nested-list"),
    ],
)
def test_encode_line_is_encode_plus_one_newline(value: object) -> None:
    line = encode_line(value)
    assert line == encode(value) + b"\n"
    assert line.count(b"\n") == 1
    assert line.endswith(b"\n")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(NASTY_TEXT, id="bare-string"),
        pytest.param({"note": NASTY_TEXT}, id="in-value"),
        pytest.param({NASTY_TEXT: "v"}, id="in-key"),
        pytest.param([NASTY_TEXT, {"k": [NASTY_TEXT]}], id="nested"),
    ],
)
def test_payloads_can_never_break_the_framing(value: object) -> None:
    encoded = encode(value)
    for forbidden in (b"\n", b"\r"):
        assert forbidden not in encoded
    assert max(encoded) < 128
    assert decode(encoded) == value


def test_a_jsonl_stream_splits_back_into_its_records() -> None:
    records = [
        {"kind": "open", "note": NASTY_TEXT},
        {"kind": "fill", "px": Decimal("1.50"), "qty": 2**60},
        {"kind": "close", "tags": ["é", "🙂", 'has "quotes"']},
    ]
    stream = b"".join(encode_line(record) for record in records)
    lines = stream.split(b"\n")

    assert lines[-1] == b""  # trailing newline, no partial final record
    assert len(lines) == len(records) + 1
    assert [decode(line) for line in lines[:-1]] == [
        decode(encode(record)) for record in records
    ]
