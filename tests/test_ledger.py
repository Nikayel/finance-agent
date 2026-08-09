"""Milestone 2 — the append-only ledger.

The ledger is the only evidence that a dataset was sealed or a run happened, so
its failure modes matter far more than its features. These tests pin the two
rules that keep it trustworthy after a crash:

* **The newline is the commit marker.** Bytes after the last ``\\n`` are an
  append that never finished, and the reader ignores them in silence — even
  when they happen to be perfectly good JSON. Parseability is not commitment.
* **A committed line that will not parse is corruption**, reported with its
  1-based line number, never quietly skipped.

Everything else follows from those: one canonical line per record, appends that
only ever extend the file, and a single ``write()`` under ``O_APPEND`` so eight
concurrent writers leave eight intact lines. The crash and concurrency tests
drive real processes and a real ``SIGKILL``; patching ``os.write`` or
``os.fsync`` would assert something about the patch, not about durability.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from sbx import ledger
from sbx.canonical import encode, encode_line
from sbx.errors import LedgerCorruptError, NotCanonicalError, SbxError

EXPECTED_KINDS = ("dataset", "run")

# Digit-free names: the corruption tests assert on the numbers a message
# mentions, and a "run-0001" in the payload would muddy that check.
WORDS = (
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
)

# Every way a payload can try to smuggle a raw newline past the JSONL framing.
NASTY_TEXT = 'a\nb\r\nc\td "quoted" back\\slash é 🙂 \u2028 \u2029 \x00 end'

RUN_ID = re.compile(r"\Arun-\d{4,}\Z")


def record(kind: str, name: str) -> dict[str, Any]:
    """The smallest well-formed record of a given kind."""
    return {"kind": kind, "name": name}


def write_raw(payload: bytes) -> Path:
    """Put exact bytes in the ledger, behind append's back.

    The only way to build a torn or damaged file without waiting for a real
    crash — the crash tests below do it the honest way.
    """
    target = ledger.path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def numbers_in(message: str) -> set[int]:
    """Numbers a message mentions, ignoring the ones in the file path.

    ``tmp_path`` is full of digits (``pytest-123/test_foo0``) and would drown
    the line number the ledger actually chose.
    """
    stripped = message.replace(str(ledger.path()), "")
    return {int(token) for token in re.findall(r"\d+", stripped)}


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


# ---------------------------------------------------------------------------
# layout — where the ledger lives, and what merely asking costs
# ---------------------------------------------------------------------------


def test_path_is_the_ledger_inside_sbx_home(sbx_home: Path) -> None:
    assert isinstance(ledger.path(), Path)
    assert ledger.path() == sbx_home / "ledger.jsonl"
    assert ledger.path().name == "ledger.jsonl"


def test_path_is_resolved_fresh_on_every_call(
    sbx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ledger.path() == sbx_home / "ledger.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    # Nothing is cached at import time, which is what lets a test — or a user
    # with two homes — redirect the ledger without reloading the module.
    assert ledger.path() == tmp_path / "elsewhere" / ".sbx" / "ledger.jsonl"


def test_path_creates_nothing(sbx_home: Path) -> None:
    # A query, not a command: the directory appears on the first append, so
    # `sbx ls` on a fresh machine must not litter the home directory.
    ledger.path()
    assert not sbx_home.exists()


def test_ledger_kinds_is_exactly_dataset_and_run() -> None:
    assert isinstance(ledger.LEDGER_KINDS, tuple)
    assert ledger.LEDGER_KINDS == EXPECTED_KINDS


def test_ledger_failures_are_sbx_errors() -> None:
    # The CLI catches SbxError and prints it; anything else tracebacks.
    assert issubclass(LedgerCorruptError, SbxError)
    assert issubclass(NotCanonicalError, SbxError)


# ---------------------------------------------------------------------------
# append — one canonical line per record, and only ever more of them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", EXPECTED_KINDS)
def test_append_writes_exactly_the_canonical_line(sbx_home: Path, kind: str) -> None:
    entry = {"kind": kind, "name": "alpha", "rows": 3}
    ledger.append(entry)
    assert ledger.path().read_bytes() == encode_line(entry)


def test_append_creates_the_parent_directory(sbx_home: Path) -> None:
    assert not sbx_home.exists()
    ledger.append(record("run", "alpha"))
    assert sbx_home.is_dir()
    assert ledger.path().is_file()


def test_append_returns_the_record_it_stored(sbx_home: Path) -> None:
    stored = ledger.append(record("dataset", "alpha"))
    assert isinstance(stored, dict)
    assert stored == ledger.entries()[-1]
    assert encode_line(stored) == ledger.path().read_bytes()


def test_appends_accumulate_in_order(sbx_home: Path) -> None:
    written = [record("run", name) for name in WORDS[:5]]
    for entry in written:
        ledger.append(entry)

    assert ledger.entries() == written
    assert ledger.path().read_bytes() == b"".join(encode_line(e) for e in written)


def test_appending_never_rewrites_earlier_bytes(sbx_home: Path) -> None:
    for name in WORDS[:3]:
        ledger.append(record("run", name))
    prefix = ledger.path().read_bytes()

    ledger.append(record("run", "delta"))

    after = ledger.path().read_bytes()
    assert after.startswith(prefix)
    assert after[len(prefix) :] == encode_line(record("run", "delta"))


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"kind": "run"}, id="bare"),
        pytest.param({"kind": "dataset", "z": 1, "a": 2}, id="unsorted-keys"),
        pytest.param({"kind": "run", "note": NASTY_TEXT}, id="nasty-text"),
        pytest.param({"kind": "run", "px": Decimal("0.00420000")}, id="decimal"),
        pytest.param({"kind": "dataset", "rows": [{"b": 1, "a": None}]}, id="nested"),
    ],
)
def test_every_stored_line_is_canonical(sbx_home: Path, entry: dict[str, Any]) -> None:
    ledger.append(entry)
    stored = ledger.path().read_bytes()

    assert stored == encode_line(entry)
    assert stored.endswith(b"\n")
    assert stored.count(b"\n") == 1
    assert max(stored) < 128  # pure ASCII, so no payload byte can look like framing
    assert b"\r" not in stored


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(MappingProxyType, id="mappingproxy"),
        pytest.param(Frozen, id="custom-mapping"),
    ],
)
def test_append_accepts_any_mapping(sbx_home: Path, wrap: Any) -> None:
    # canonical.encode only understands dict, so append has to normalise the
    # top level itself — the signature promises Mapping, not dict.
    entry = {"kind": "run", "name": "alpha"}
    ledger.append(wrap(entry))
    assert ledger.entries() == [entry]


def test_entries_re_read_the_file_rather_than_a_cache(sbx_home: Path) -> None:
    stored = ledger.append(record("run", "alpha"))
    stored["name"] = "tampered"
    ledger.entries()[0]["name"] = "tampered-too"
    # The file is the ledger; anything handed to a caller is a copy of it.
    assert ledger.entries() == [record("run", "alpha")]


# ---------------------------------------------------------------------------
# validation — what the ledger refuses, and the nothing it writes when it does
# ---------------------------------------------------------------------------

NOT_A_RECORD = [
    pytest.param(None, id="none"),
    pytest.param(42, id="int"),
    pytest.param("kind", id="string"),
    pytest.param(b'{"kind":"run"}', id="bytes"),
    pytest.param(["kind", "run"], id="list"),
    pytest.param([{"kind": "run"}], id="list-of-records"),
    pytest.param(("kind", "run"), id="tuple"),
    pytest.param({"kind", "run"}, id="set"),
    pytest.param(object(), id="object"),
]

BAD_KINDS = [
    pytest.param({}, id="empty"),
    pytest.param({"name": "alpha"}, id="missing-kind"),
    pytest.param({"Kind": "run"}, id="wrong-case-key"),
    pytest.param({"kind": ""}, id="empty-kind"),
    pytest.param({"kind": "runs"}, id="near-miss-plural"),
    pytest.param({"kind": "RUN"}, id="upper-case"),
    pytest.param({"kind": "Dataset"}, id="title-case"),
    pytest.param({"kind": "run "}, id="trailing-space"),
    pytest.param({"kind": "result"}, id="unknown"),
    pytest.param({"kind": None}, id="null-kind"),
    pytest.param({"kind": 1}, id="int-kind"),
    pytest.param({"kind": True}, id="bool-kind"),
    pytest.param({"kind": ["run"]}, id="list-kind"),
]


@pytest.mark.parametrize("value", NOT_A_RECORD)
def test_append_refuses_anything_that_is_not_a_mapping(
    sbx_home: Path, value: Any
) -> None:
    with pytest.raises(SbxError):
        ledger.append(value)
    assert not ledger.path().exists()


@pytest.mark.parametrize("entry", BAD_KINDS)
def test_append_refuses_a_record_without_a_known_kind(
    sbx_home: Path, entry: dict[str, Any]
) -> None:
    with pytest.raises(SbxError):
        ledger.append(entry)
    assert not ledger.path().exists()


@pytest.mark.parametrize("entry", BAD_KINDS)
def test_a_refused_record_leaves_the_existing_ledger_untouched(
    sbx_home: Path, entry: dict[str, Any]
) -> None:
    ledger.append(record("run", "alpha"))
    before = ledger.path().read_bytes()

    with pytest.raises(SbxError):
        ledger.append(entry)

    assert ledger.path().read_bytes() == before
    assert ledger.entries() == [record("run", "alpha")]


def test_rejection_messages_are_worth_printing(sbx_home: Path) -> None:
    with pytest.raises(SbxError) as caught:
        ledger.append({"kind": "result"})
    assert str(caught.value).strip() != ""


# ---------------------------------------------------------------------------
# encoding — floats never reach the file, Decimals arrive as text
# ---------------------------------------------------------------------------

FLOAT_PLACEMENTS = [
    pytest.param({"kind": "run", "pnl": 1.0}, id="top-level"),
    pytest.param({"kind": "run", "pnl": [1, 2.5]}, id="in-list"),
    pytest.param({"kind": "run", "fills": [{"px": 0.1}]}, id="nested"),
    pytest.param({"kind": "dataset", "ratio": float("nan")}, id="nan"),
    pytest.param({"kind": "dataset", "ratio": float("inf")}, id="infinity"),
]


@pytest.mark.parametrize("entry", FLOAT_PLACEMENTS)
def test_a_float_anywhere_is_refused_and_nothing_is_written(
    sbx_home: Path, entry: dict[str, Any]
) -> None:
    with pytest.raises(NotCanonicalError):
        ledger.append(entry)
    assert not ledger.path().exists()


@pytest.mark.parametrize("entry", FLOAT_PLACEMENTS)
def test_a_refused_float_does_not_disturb_earlier_records(
    sbx_home: Path, entry: dict[str, Any]
) -> None:
    ledger.append(record("dataset", "alpha"))
    before = ledger.path().read_bytes()

    with pytest.raises(NotCanonicalError):
        ledger.append(entry)

    assert ledger.path().read_bytes() == before
    assert ledger.entries() == [record("dataset", "alpha")]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({1, 2}, id="set"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param(datetime(2024, 1, 1, 12, 30), id="datetime"),
        pytest.param(Decimal("NaN"), id="decimal-nan"),
        pytest.param(Decimal("Infinity"), id="decimal-infinity"),
        pytest.param(object(), id="object"),
    ],
)
def test_values_with_no_canonical_form_are_refused(sbx_home: Path, value: Any) -> None:
    with pytest.raises(NotCanonicalError):
        ledger.append({"kind": "run", "value": value})
    assert not ledger.path().exists()


def test_decimals_are_stored_as_text_and_come_back_as_text(sbx_home: Path) -> None:
    ledger.append({"kind": "run", "pnl": Decimal("1.50"), "fee": Decimal("0.00420000")})
    [entry] = ledger.entries()

    # Deliberate asymmetry, inherited from the untagged canonical encoding: the
    # ledger stores digits, and each record schema re-applies Decimal to its own
    # known fields on the way back. Do not "fix" this into a round trip.
    assert entry["pnl"] == "1.50"
    assert isinstance(entry["pnl"], str)
    assert not isinstance(entry["pnl"], Decimal)
    # Trailing zeros are exchange precision, not noise, so they survive intact.
    assert entry["fee"] == "0.00420000"
    assert Decimal(entry["pnl"]) == Decimal("1.50")


# ---------------------------------------------------------------------------
# reading — the newline is the commit marker
# ---------------------------------------------------------------------------


def test_reading_a_missing_ledger_finds_nothing_and_creates_nothing(
    sbx_home: Path,
) -> None:
    assert not ledger.path().exists()
    assert ledger.entries() == []
    assert ledger.entries_of("run") == []
    assert ledger.next_run_id() == "run-0001"
    assert not ledger.path().exists()  # reading is not a reason to create it


def test_an_empty_file_reads_as_an_empty_ledger(sbx_home: Path) -> None:
    write_raw(b"")
    assert ledger.entries() == []
    assert ledger.entries_of("dataset") == []
    assert ledger.next_run_id() == "run-0001"


def test_a_torn_tail_is_ignored_without_complaint(sbx_home: Path) -> None:
    committed = record("run", "alpha")
    write_raw(encode_line(committed) + b'{"kind":"run","name":"bra')

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a warning here would fail the test too
        assert ledger.entries() == [committed]


def test_a_torn_tail_that_is_valid_json_is_still_ignored(sbx_home: Path) -> None:
    """The newline decides commitment; parseability does not.

    A writer killed mid-append can leave bytes that parse perfectly well — a
    prefix of a bigger number, a record whose last field simply had not been
    written yet, or (as here) an entire record whose terminating newline never
    made it to disk. The reader cannot tell any of those from a finished write
    by looking at the JSON, so it does not try: only the newline says
    "committed". Keeping this fragment would mean the ledger occasionally
    reports a run that the writer never promised had happened.
    """
    committed = record("run", "alpha")
    uncommitted = record("run", "bravo")
    write_raw(encode_line(committed) + encode(uncommitted))  # note: no newline

    assert ledger.entries() == [committed]
    assert ledger.entries_of("run") == [committed]
    assert ledger.next_run_id() == "run-0002"


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param(b'{"kind":"run","name":"bra', id="truncated-object"),
        pytest.param(b'{"kind":"run","name":"bravo"}', id="whole-valid-record"),
        pytest.param(b"1234", id="valid-json-number"),
        pytest.param(b"null", id="valid-json-null"),
        pytest.param(b'"text"', id="valid-json-string"),
        pytest.param(b" ", id="whitespace"),
        pytest.param(b"not json at all", id="garbage"),
    ],
)
def test_anything_after_the_last_newline_is_uncommitted(
    sbx_home: Path, fragment: bytes
) -> None:
    committed = [record("run", "alpha"), record("dataset", "bravo")]
    write_raw(b"".join(encode_line(e) for e in committed) + fragment)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ledger.entries() == committed


def test_a_file_that_is_only_a_torn_fragment_reads_as_empty(sbx_home: Path) -> None:
    write_raw(b'{"kind":"run","name":"alp')
    assert ledger.entries() == []
    assert ledger.next_run_id() == "run-0001"


def test_appending_after_a_torn_tail_still_works(sbx_home: Path) -> None:
    committed = [record("run", "alpha"), record("dataset", "bravo")]
    write_raw(b"".join(encode_line(e) for e in committed) + b'{"kind":"run","na')

    added = ledger.append(record("run", "charlie"))

    # Only the observable behaviour is specified: the implementation may leave
    # the torn bytes in place or truncate them away, but every committed record
    # has to survive and the new one has to land after them.
    assert ledger.entries() == [*committed, added]
    for entry in committed:
        assert entry in ledger.entries()
    assert ledger.next_run_id() == "run-0003"


# ---------------------------------------------------------------------------
# corruption — a committed line that will not parse is not a torn write
# ---------------------------------------------------------------------------

CORRUPT_CONTENT = [
    pytest.param(b"not json at all", id="garbage"),
    pytest.param(b'{"kind":"run"', id="truncated-object"),
    pytest.param(b'{"kind":"run",}', id="trailing-comma"),
    pytest.param(b"}broken{", id="reversed-braces"),
    # A blank committed line means bytes went missing between two newlines,
    # which is damage — not something to skip politely.
    pytest.param(b"", id="blank-line"),
    pytest.param(b"   ", id="whitespace-only"),
]


@pytest.mark.parametrize("position", [1, 5, 9], ids=["first", "middle", "last"])
@pytest.mark.parametrize("content", CORRUPT_CONTENT)
def test_a_committed_line_that_will_not_parse_raises(
    sbx_home: Path, content: bytes, position: int
) -> None:
    lines = [encode_line(record("run", name)) for name in WORDS]
    lines[position - 1] = content + b"\n"
    write_raw(b"".join(lines))

    with pytest.raises(LedgerCorruptError) as caught:
        ledger.entries()

    assert position in numbers_in(str(caught.value))


@pytest.mark.parametrize("position", [5, 9], ids=["middle", "last"])
def test_the_reported_line_number_is_one_based(sbx_home: Path, position: int) -> None:
    # WORDS carry no digits, so once the tmp path is stripped the only numbers
    # left in the message are ones the ledger chose (plus any column offsets a
    # wrapped decoder error contributes, which is why line 1 — where the
    # off-by-one would be 0 — is a poor discriminator and is left out here).
    lines = [encode_line(record("run", name)) for name in WORDS]
    lines[position - 1] = b"}broken{\n"
    write_raw(b"".join(lines))

    with pytest.raises(LedgerCorruptError) as caught:
        ledger.entries()

    mentioned = numbers_in(str(caught.value))
    assert position in mentioned
    assert position - 1 not in mentioned


def test_corruption_is_raised_by_every_reader(sbx_home: Path) -> None:
    write_raw(encode_line(record("run", "alpha")) + b"broken\n")

    for read in (ledger.entries, lambda: ledger.entries_of("run"), ledger.next_run_id):
        with pytest.raises(LedgerCorruptError):
            read()


def test_a_corrupt_line_is_not_excused_by_a_torn_tail(sbx_home: Path) -> None:
    # An unterminated last line is forgiven; damage earlier in the file is not,
    # and one must never be used to explain away the other.
    write_raw(b"broken\n" + encode_line(record("run", "alpha")) + b'{"kind":"ru')

    with pytest.raises(LedgerCorruptError):
        ledger.entries()


def test_a_corrupt_line_message_is_worth_printing(sbx_home: Path) -> None:
    write_raw(b"broken\n")
    with pytest.raises(LedgerCorruptError) as caught:
        ledger.entries()
    assert str(caught.value).strip() != ""


# ---------------------------------------------------------------------------
# next_run_id — derived from the ledger's contents, never from a counter
# ---------------------------------------------------------------------------


def test_next_run_id_starts_at_one(sbx_home: Path) -> None:
    assert ledger.next_run_id() == "run-0001"


def test_next_run_id_ignores_dataset_records(sbx_home: Path) -> None:
    for name in WORDS[:5]:
        ledger.append(record("dataset", name))
    assert ledger.next_run_id() == "run-0001"


def test_next_run_id_increments_only_on_run_records(sbx_home: Path) -> None:
    assert ledger.next_run_id() == "run-0001"
    ledger.append(record("run", "alpha"))
    assert ledger.next_run_id() == "run-0002"
    ledger.append(record("dataset", "bravo"))
    assert ledger.next_run_id() == "run-0002"
    ledger.append(record("run", "charlie"))
    assert ledger.next_run_id() == "run-0003"


def test_next_run_id_is_a_query_not_a_counter(sbx_home: Path) -> None:
    ledger.append(record("run", "alpha"))
    assert ledger.next_run_id() == "run-0002"
    assert ledger.next_run_id() == "run-0002"  # asking twice changes nothing
    assert ledger.entries() == [record("run", "alpha")]


def test_next_run_id_counts_records_not_the_ids_inside_them(sbx_home: Path) -> None:
    # The id comes from how many runs the ledger holds. A record that merely
    # *claims* to be run-9999 does not move the sequence, so a hand-edited or
    # imported ledger cannot make the next run collide with an existing one.
    ledger.append({"kind": "run", "run_id": "run-9999"})
    ledger.append({"kind": "run", "run_id": "run-0002"})
    assert ledger.next_run_id() == "run-0003"


def test_next_run_id_is_zero_padded_and_monotonic(sbx_home: Path) -> None:
    seen = []
    for index in range(15):
        current = ledger.next_run_id()
        assert RUN_ID.match(current)  # at least four digits, room to grow past 9999
        assert current == f"run-{index + 1:04d}"
        seen.append(current)
        ledger.append({"kind": "run", "run_id": current})
        if index % 3 == 0:
            ledger.append(record("dataset", "alpha"))

    assert len(set(seen)) == len(seen)
    assert seen == sorted(seen)  # zero padding makes lexical order numeric order


def test_next_run_id_ignores_an_uncommitted_tail(sbx_home: Path) -> None:
    write_raw(encode_line(record("run", "alpha")) + encode(record("run", "bravo")))
    assert ledger.next_run_id() == "run-0002"


# ---------------------------------------------------------------------------
# entries_of — a filter, not a re-ordering
# ---------------------------------------------------------------------------


def test_entries_of_filters_by_kind_and_keeps_order(sbx_home: Path) -> None:
    written = [
        record("run", "alpha"),
        record("dataset", "bravo"),
        record("run", "charlie"),
        record("dataset", "delta"),
        record("run", "echo"),
    ]
    for entry in written:
        ledger.append(entry)

    runs = [e for e in written if e["kind"] == "run"]
    datasets = [e for e in written if e["kind"] == "dataset"]
    assert ledger.entries_of("run") == runs
    assert ledger.entries_of("dataset") == datasets
    assert len(runs) + len(datasets) == len(ledger.entries())


@pytest.mark.parametrize("kind", EXPECTED_KINDS)
def test_entries_of_a_kind_with_no_records_is_empty(sbx_home: Path, kind: str) -> None:
    other = next(k for k in EXPECTED_KINDS if k != kind)
    ledger.append(record(other, "alpha"))
    assert ledger.entries_of(kind) == []
    assert ledger.entries_of(other) == [record(other, "alpha")]


# ---------------------------------------------------------------------------
# framing and size — the payload can never break the file
# ---------------------------------------------------------------------------


def test_payloads_cannot_break_the_framing(sbx_home: Path) -> None:
    written = [
        {"kind": "run", "note": NASTY_TEXT},
        {"kind": "dataset", "note": "line one\nline two\r\nline three"},
        {"kind": "run", "tags": ["é", "🙂", 'has "quotes"', "back\\slash"]},
    ]
    for entry in written:
        ledger.append(entry)

    stored = ledger.path().read_bytes()
    assert stored.count(b"\n") == len(written)  # one line per record, exactly
    assert max(stored) < 128
    assert ledger.entries() == written


def test_a_large_record_survives_intact(sbx_home: Path) -> None:
    # Position-tagged so a truncation or a splice shows up as wrong content,
    # not merely as a wrong length.
    blob = "".join(f"{index:07d}" for index in range(150_000))
    assert len(blob) > 1_000_000

    ledger.append({"kind": "dataset", "blob": blob})

    [entry] = ledger.entries()
    assert entry["blob"] == blob
    assert ledger.path().read_bytes().count(b"\n") == 1


def test_a_large_record_does_not_block_later_appends(sbx_home: Path) -> None:
    ledger.append({"kind": "dataset", "blob": "x" * (1 << 20)})
    ledger.append(record("run", "alpha"))
    assert [e["kind"] for e in ledger.entries()] == ["dataset", "run"]
    assert ledger.entries()[-1] == record("run", "alpha")


# ---------------------------------------------------------------------------
# the real operating system — a crash mid-append, and eight writers at once
#
# No mocks in this section, on purpose. os.write, os.fsync and O_APPEND are the
# exact mechanisms under test; patching one would assert something about the
# patch instead of about durability. The children inherit HOME from os.environ
# (the sbx_home fixture sets it), which is also what proves path() reads the
# environment fresh in a process that never saw the fixture.
# ---------------------------------------------------------------------------

TORN_WRITE_THEN_SIGKILL = """\
import os
import signal

from sbx import ledger

target = ledger.path()
target.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
os.write(fd, b'{"kind":"run","name":"never-committed"')  # note: no newline
os.fsync(fd)
os.kill(os.getpid(), signal.SIGKILL)
"""

APPEND_ONE_RECORD = """\
import sys
import time

from sbx import ledger

letter, start_at = sys.argv[1], float(sys.argv[2])
time.sleep(max(0.0, start_at - time.time()))  # all eight aim at the same instant
ledger.append({"kind": "run", "worker": letter, "payload": letter * 60000})
"""

COUNT_ENTRIES = """\
import sys

from sbx import ledger

sys.stdout.write(str(len(ledger.entries())))
"""


def run_child(program: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", program, *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_an_appended_record_is_visible_to_a_fresh_process(sbx_home: Path) -> None:
    # append() has to reach the file before it returns: a record still sitting
    # in this process's buffer is not a record anyone else can see.
    ledger.append(record("run", "alpha"))
    ledger.append(record("dataset", "bravo"))

    result = run_child(COUNT_ENTRIES)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "2"


def test_a_process_killed_mid_append_commits_nothing(sbx_home: Path) -> None:
    committed = [record("run", "alpha"), record("dataset", "bravo")]
    for entry in committed:
        ledger.append(entry)
    before = ledger.path().read_bytes()

    result = run_child(TORN_WRITE_THEN_SIGKILL)

    assert result.returncode == -9, result.stderr  # really SIGKILL, not a clean exit
    after = ledger.path().read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)  # the fragment genuinely landed on disk
    assert not after.endswith(b"\n")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ledger.entries() == committed


def test_a_ledger_torn_by_a_kill_can_still_be_appended_to(sbx_home: Path) -> None:
    committed = [record("run", "alpha"), record("dataset", "bravo")]
    for entry in committed:
        ledger.append(entry)

    assert run_child(TORN_WRITE_THEN_SIGKILL).returncode == -9

    added = ledger.append(record("run", "charlie"))

    assert ledger.entries() == [*committed, added]
    assert ledger.entries_of("run") == [record("run", "alpha"), added]
    assert ledger.entries_of("dataset") == [record("dataset", "bravo")]
    assert ledger.next_run_id() == "run-0003"


def test_concurrent_appends_do_not_interleave(sbx_home: Path) -> None:
    letters = "abcdefgh"
    expected = [
        {"kind": "run", "worker": letter, "payload": letter * 60000}
        for letter in letters
    ]
    start_at = time.time() + 2.0  # long enough for eight interpreters to boot

    children = [
        subprocess.Popen(
            [sys.executable, "-c", APPEND_ONE_RECORD, letter, repr(start_at)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for letter in letters
    ]
    for child in children:
        _, stderr = child.communicate(timeout=60)
        assert child.returncode == 0, stderr

    # The proof of the single write() under O_APPEND: every line is exactly one
    # record's canonical bytes, so nothing was spliced into anything else and
    # nothing was overwritten by a stale offset.
    raw = ledger.path().read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == len(expected)
    assert sorted(raw.splitlines(keepends=True)) == sorted(
        encode_line(entry) for entry in expected
    )

    stored = ledger.entries()
    assert len(stored) == len(expected)
    assert {entry["worker"] for entry in stored} == set(letters)
    for entry in expected:
        assert entry in stored
    for entry in stored:
        assert entry in expected
