"""Milestone 2 — sealed datasets: the content-addressed store.

A sealed dataset is an immutable snapshot of an upstream journal, named by the
sha256 of the exact bytes it was cut from. Two properties are the whole point,
and both are easy to break by accident:

* **Idempotence.** Sealing the same bytes twice — under a different name, from
  a different directory, on a different day — yields the same dataset and
  writes nothing new. That is what forbids the manifest from carrying
  provenance or a timestamp, however useful either would look in `sbx ls`.
* **Tamper evidence.** A sealed dataset edited by a single byte must fail
  `verify()`, because every run recorded against it claims to have seen those
  exact bytes.

Immutability is asserted against the real filesystem — real mode bits, a real
`PermissionError`, a real `pwrite` — and never a mock. A test that patches
`os.chmod` asserts something about the patch.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

import journals
from sbx import paths
from sbx.canonical import hash_file
from sbx.errors import SbxError
from sbx.store import SealedDataset, all_datasets, load, resolve, seal

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

# The two file names are part of the contract: `sbx verify` and anything else
# that ever reads a dataset reaches for them by name.
DATASET_ENTRIES = ["data.jsonl", "manifest.json"]

# Everything a manifest is allowed to say. See the manifest test for why the
# obvious fourth field cannot exist.
MANIFEST_KEYS = {"bytes", "records", "sha256"}

# A fixed timestamp for generated trades: none of these tests are about time.
AT = journals.TRADE["executed_at"]


def sha256_of(path: Path) -> str:
    """The digest a dataset must be named after, computed independently of sbx."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tamper(path: Path, mutate: Callable[[Path], None]) -> None:
    """Edit a sealed file behind the store's back, then put the mode back.

    Anyone with a shell has exactly this much power, so `verify` is tested
    against it rather than against a patched `open`.
    """
    original_mode = path.stat().st_mode & 0o777
    os.chmod(path, 0o644)
    try:
        mutate(path)
    finally:
        os.chmod(path, original_mode)


def journals_sharing_a_prefix(
    directory: Path, length: int = 4, attempts: int = 1500
) -> tuple[Path, Path]:
    """Two journals whose digests share their first `length` hex characters.

    Ambiguity is only reachable at four characters or more, since `resolve`
    refuses anything shorter — so a shared *first* character is not enough to
    provoke it. Over 16**4 buckets, 1500 candidates make a collision
    effectively certain; the bound exists so a wrong `write_journal` cannot
    turn this into an infinite loop, not because a skip is expected.
    """
    probe = directory / "probe.jsonl"
    seen: dict[str, list[dict]] = {}
    for index in range(attempts):
        records = [journals.trade(price=f"{index}.00", at=AT, sequence=index)]
        journals.write_journal(probe, records)
        bucket = sha256_of(probe)[:length]
        if bucket in seen:
            first = journals.write_journal(directory / "first.jsonl", seen[bucket])
            second = journals.write_journal(directory / "second.jsonl", records)
            probe.unlink()
            return first, second
        seen[bucket] = records
    pytest.skip(f"no two of {attempts} journals shared a {length}-character prefix")


# ---------------------------------------------------------------------------
# sbx.paths — where everything lives, resolved fresh every time
# ---------------------------------------------------------------------------


def test_sbx_home_is_dot_sbx_under_the_current_home(sbx_home: Path) -> None:
    assert paths.sbx_home() == sbx_home
    assert paths.sbx_home() == Path(os.environ["HOME"]) / ".sbx"
    assert paths.sbx_home().name == ".sbx"


def test_datasets_dir_and_ledger_path_sit_under_sbx_home(sbx_home: Path) -> None:
    assert paths.datasets_dir() == sbx_home / "datasets"
    assert paths.ledger_path() == sbx_home / "ledger.jsonl"
    assert paths.datasets_dir().parent == paths.sbx_home()
    assert paths.ledger_path().parent == paths.sbx_home()


def test_asking_for_a_path_creates_nothing(sbx_home: Path) -> None:
    # Path functions answer a question; only `seal` is allowed to write. `sbx
    # ls` on a machine that has never sealed anything must leave no trace.
    for path in (paths.sbx_home(), paths.datasets_dir(), paths.ledger_path()):
        assert not path.exists()
    assert not sbx_home.exists()


def test_sbx_home_follows_a_home_that_moves(
    sbx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert paths.sbx_home() == sbx_home

    moved = tmp_path / "second-home"
    moved.mkdir()
    monkeypatch.setenv("HOME", str(moved))

    # Nothing is captured at import time. That is what lets the test suite
    # redirect the home at all, and what lets a subprocess inherit the redirect.
    assert paths.sbx_home() == moved / ".sbx"
    assert paths.sbx_home() != sbx_home
    assert paths.datasets_dir() == moved / ".sbx" / "datasets"
    assert paths.ledger_path() == moved / ".sbx" / "ledger.jsonl"


# ---------------------------------------------------------------------------
# content addressing — the directory name *is* the hash of the source bytes
# ---------------------------------------------------------------------------


def test_seal_names_the_dataset_after_the_source_bytes(
    sbx_home: Path, journal: Path
) -> None:
    digest = sha256_of(journal)

    dataset, newly_created = seal(journal)

    assert isinstance(dataset, SealedDataset)
    assert newly_created is True
    assert dataset.sha256 == digest
    assert HEX64.match(dataset.sha256)
    assert dataset.sha256 == hash_file(journal)
    assert dataset.directory == paths.datasets_dir() / digest
    assert dataset.directory.name == digest
    assert dataset.directory.is_dir()


def test_the_dataset_directory_holds_exactly_data_and_manifest(
    sbx_home: Path, journal: Path
) -> None:
    dataset, _ = seal(journal)

    assert dataset.data_path == dataset.directory / "data.jsonl"
    assert dataset.manifest_path == dataset.directory / "manifest.json"
    assert dataset.data_path.is_file()
    assert dataset.manifest_path.is_file()
    assert sorted(p.name for p in dataset.directory.iterdir()) == DATASET_ENTRIES


def test_short_is_the_first_twelve_characters_of_the_hash(
    sbx_home: Path, journal: Path
) -> None:
    dataset, _ = seal(journal)

    assert dataset.short == dataset.sha256[:12]
    assert len(dataset.short) == 12


def test_seal_accepts_the_string_path_the_cli_hands_it(
    sbx_home: Path, journal: Path
) -> None:
    dataset, newly_created = seal(str(journal))

    assert newly_created is True
    assert dataset.sha256 == sha256_of(journal)


def test_the_stored_copy_is_byte_identical_to_the_source(
    sbx_home: Path, journal: Path
) -> None:
    source = journal.read_bytes()
    # The upstream dialect is *not* canonical — json.dumps' ", " spacing is
    # right there in the file — and a snapshot that normalised it would no
    # longer be evidence of what upstream actually wrote.
    assert b'", "' in source

    dataset, _ = seal(journal)

    assert dataset.data_path.read_bytes() == source
    assert sha256_of(dataset.data_path) == dataset.sha256
    assert dataset.bytes == len(source) == journal.stat().st_size
    assert isinstance(dataset.bytes, int)


def test_a_sealed_dataset_is_an_immutable_value(
    sbx_home: Path, journal: Path
) -> None:
    dataset, _ = seal(journal)

    assert dataclasses.is_dataclass(dataset)
    assert isinstance(dataset.directory, Path)
    assert isinstance(dataset.records, int)
    with pytest.raises(dataclasses.FrozenInstanceError):
        dataset.sha256 = "0" * 64  # type: ignore[misc]


# ---------------------------------------------------------------------------
# records — complete lines only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 7])
def test_records_counts_newline_terminated_lines(
    sbx_home: Path, tmp_path: Path, count: int
) -> None:
    records = journals.sample_records()[:count]
    source = journals.write_journal(tmp_path / f"{count}-records.jsonl", records)

    dataset, _ = seal(source)

    assert dataset.records == count
    assert dataset.bytes == source.stat().st_size


def test_a_final_line_without_a_newline_is_not_counted(
    sbx_home: Path, tmp_path: Path
) -> None:
    complete = journals.write_journal(tmp_path / "complete.jsonl")
    payload = complete.read_bytes()
    assert payload.endswith(b"\n")

    # An append that never committed: the bytes are there, the record is not.
    torn_path = tmp_path / "torn.jsonl"
    torn_path.write_bytes(payload[:-1])

    whole, _ = seal(complete)
    torn, _ = seal(torn_path)

    assert whole.records == len(journals.sample_records())
    assert torn.records == whole.records - 1
    # The store copies bytes; it does not repair the torn line by putting the
    # newline back, so the two snapshots stay distinguishable forever.
    assert torn.data_path.read_bytes() == payload[:-1]
    assert torn.sha256 != whole.sha256
    assert torn.verify() is True


# ---------------------------------------------------------------------------
# idempotence — the headline property: same bytes, same dataset, no rewrite
# ---------------------------------------------------------------------------


def test_sealing_the_same_file_twice_writes_nothing_new(
    sbx_home: Path, journal: Path
) -> None:
    first, first_created = seal(journal)
    second, second_created = seal(journal)

    assert first_created is True
    assert second_created is False
    assert second == first
    assert second.directory == first.directory
    assert [p.name for p in paths.datasets_dir().iterdir()] == [first.sha256]


def test_the_same_bytes_under_another_name_are_the_same_dataset(
    sbx_home: Path, tmp_path: Path, journal: Path
) -> None:
    elsewhere = journals.write_journal(tmp_path / "archive" / "2026-07-29.jsonl")
    assert elsewhere.read_bytes() == journal.read_bytes()
    assert elsewhere.name != journal.name
    assert elsewhere.parent != journal.parent

    first, first_created = seal(journal)
    second, second_created = seal(elsewhere)

    assert first_created is True
    assert second_created is False
    # Identity is the content and nothing else: not the name, not the
    # directory, not the order the two were sealed in.
    assert second == first
    assert [p.name for p in paths.datasets_dir().iterdir()] == [first.sha256]


def test_the_manifest_carries_no_provenance_and_no_timestamp(
    sbx_home: Path, tmp_path: Path, journal: Path
) -> None:
    first, _ = seal(journal)
    written = first.manifest_path.read_bytes()

    elsewhere = journals.write_journal(tmp_path / "archive" / "renamed.jsonl")
    second, newly_created = seal(elsewhere)

    assert newly_created is False
    # A `sealed_at` or a `source` field would read well in `sbx ls` and would
    # destroy the property the store exists for: the same bytes sealed twice
    # would stop producing the same dataset.
    assert second.manifest_path.read_bytes() == written

    manifest = json.loads(written)
    assert set(manifest) == MANIFEST_KEYS
    assert manifest == {
        "sha256": first.sha256,
        "bytes": first.bytes,
        "records": first.records,
    }
    text = written.decode("utf-8")
    assert journal.name not in text
    assert elsewhere.name not in text


def test_one_byte_of_difference_is_a_different_dataset(
    sbx_home: Path, tmp_path: Path, journal: Path
) -> None:
    records = journals.sample_records()
    # Same field widths, one digit apart: 118200.75 becomes 118200.76.
    records[1] = journals.trade(price="118200.76", at=AT, sequence=2)
    nudged = journals.write_journal(tmp_path / "nudged.jsonl", records)

    original_bytes = journal.read_bytes()
    nudged_bytes = nudged.read_bytes()
    assert len(nudged_bytes) == len(original_bytes)
    assert sum(a != b for a, b in zip(original_bytes, nudged_bytes)) == 1

    first, first_created = seal(journal)
    second, second_created = seal(nudged)

    assert first_created is True
    assert second_created is True
    assert first.sha256 != second.sha256
    assert sorted(p.name for p in paths.datasets_dir().iterdir()) == sorted(
        [first.sha256, second.sha256]
    )
    assert first.data_path.read_bytes() == original_bytes
    assert second.data_path.read_bytes() == nudged_bytes


# ---------------------------------------------------------------------------
# immutability — enforced by the filesystem, not by convention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attribute", ["data_path", "manifest_path"])
def test_sealed_files_cannot_be_opened_for_writing(
    sbx_home: Path, journal: Path, attribute: str
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits, so read-only proves nothing here")

    dataset, _ = seal(journal)
    path = getattr(dataset, attribute)

    with pytest.raises(PermissionError):
        open(path, "wb")
    # Unreadable would be no use either: the point is a snapshot you can read
    # forever and change never.
    assert path.read_bytes()


@pytest.mark.parametrize("attribute", ["data_path", "manifest_path"])
def test_sealed_files_are_mode_444(
    sbx_home: Path, journal: Path, attribute: str
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits, so read-only proves nothing here")

    dataset, _ = seal(journal)

    assert getattr(dataset, attribute).stat().st_mode & 0o777 == 0o444


# ---------------------------------------------------------------------------
# tamper detection — the milestone's demo
# ---------------------------------------------------------------------------


def test_verify_is_true_for_a_freshly_sealed_dataset(
    sbx_home: Path, journal: Path
) -> None:
    dataset, _ = seal(journal)

    assert dataset.verify() is True
    assert load(dataset.sha256).verify() is True


def test_verify_catches_a_single_flipped_byte(sbx_home: Path, journal: Path) -> None:
    dataset, _ = seal(journal)
    assert dataset.verify() is True

    size_before = dataset.data_path.stat().st_size
    original = dataset.data_path.read_bytes()
    middle = size_before // 2

    def flip(path: Path) -> None:
        descriptor = os.open(path, os.O_WRONLY)
        try:
            os.pwrite(descriptor, bytes([original[middle] ^ 0x01]), middle)
        finally:
            os.close(descriptor)

    tamper(dataset.data_path, flip)

    tampered = dataset.data_path.read_bytes()
    # A genuine content edit rather than a truncation: same length, one byte
    # different — the case a size check or an mtime check would sail past.
    assert len(tampered) == size_before
    assert sum(a != b for a, b in zip(original, tampered)) == 1

    assert dataset.verify() is False
    # A dataset loaded fresh off disk reaches the same verdict, so `verify`
    # is re-hashing the file and not answering from something it remembers.
    assert load(dataset.sha256).verify() is False
    # The recorded hash is the promise, so the manifest keeps saying what the
    # bytes were meant to be.
    assert json.loads(dataset.manifest_path.read_text())["sha256"] == dataset.sha256


def test_verify_catches_a_truncated_file(sbx_home: Path, journal: Path) -> None:
    dataset, _ = seal(journal)
    size_before = dataset.data_path.stat().st_size

    tamper(dataset.data_path, lambda path: os.truncate(path, size_before - 1))

    assert dataset.data_path.stat().st_size == size_before - 1
    assert dataset.verify() is False


def test_verify_catches_an_appended_byte(sbx_home: Path, journal: Path) -> None:
    dataset, _ = seal(journal)
    size_before = dataset.data_path.stat().st_size

    def append(path: Path) -> None:
        with open(path, "ab") as handle:
            handle.write(b"\n")  # an "empty record" is still a different file

    tamper(dataset.data_path, append)

    assert dataset.data_path.stat().st_size == size_before + 1
    assert dataset.verify() is False


# ---------------------------------------------------------------------------
# errors — every expected failure is an SbxError the CLI can print
# ---------------------------------------------------------------------------


def test_sealing_a_path_that_does_not_exist_is_an_sbx_error(
    sbx_home: Path, tmp_path: Path
) -> None:
    with pytest.raises(SbxError) as caught:
        seal(tmp_path / "no-such-journal.jsonl")

    assert str(caught.value).strip() != ""
    assert all_datasets() == []


def test_sealing_a_directory_is_an_sbx_error(sbx_home: Path, tmp_path: Path) -> None:
    # Hashing a directory raises IsADirectoryError, which the CLI would let
    # traceback; a mistyped argument has to arrive as an expected failure.
    with pytest.raises(SbxError):
        seal(tmp_path)


def test_sealing_an_empty_file_is_an_sbx_error(
    sbx_home: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")

    with pytest.raises(SbxError):
        seal(empty)

    # A journal with no records is not a dataset, and the refusal leaves the
    # store exactly as it found it.
    assert all_datasets() == []
    assert not (paths.datasets_dir() / sha256_of(empty)).exists()


# ---------------------------------------------------------------------------
# load and resolve — looking a dataset up by hash, or by enough of one
# ---------------------------------------------------------------------------


def test_load_returns_the_dataset_that_was_sealed(
    sbx_home: Path, journal: Path
) -> None:
    dataset, _ = seal(journal)

    loaded = load(dataset.sha256)

    assert loaded == dataset
    assert loaded.records == dataset.records
    assert loaded.bytes == dataset.bytes
    assert loaded.data_path.read_bytes() == journal.read_bytes()


@pytest.mark.parametrize(
    "make_key",
    [
        pytest.param(lambda dataset: "0" * 64, id="unknown-hash"),
        pytest.param(lambda dataset: "", id="empty"),
        pytest.param(lambda dataset: "not-a-hash", id="not-hex"),
        pytest.param(lambda dataset: dataset.short, id="prefix-not-a-full-hash"),
        pytest.param(lambda dataset: "..", id="parent-directory"),
        pytest.param(lambda dataset: f"../{dataset.sha256}", id="traversal"),
    ],
)
def test_load_rejects_anything_that_is_not_a_stored_hash(
    sbx_home: Path, journal: Path, make_key: Callable[[SealedDataset], str]
) -> None:
    dataset, _ = seal(journal)

    # `load` is handed whatever the user typed, so "" and ".." have to be
    # refused rather than joined onto the datasets directory and followed.
    with pytest.raises(SbxError):
        load(make_key(dataset))


@pytest.mark.parametrize("length", [4, 8, 12, 40, 64])
def test_a_unique_prefix_resolves_to_its_dataset(
    sbx_home: Path, journal: Path, length: int
) -> None:
    dataset, _ = seal(journal)

    assert resolve(dataset.sha256[:length]) == dataset


@pytest.mark.parametrize("length", [0, 1, 2, 3])
def test_a_prefix_shorter_than_four_characters_is_refused(
    sbx_home: Path, journal: Path, length: int
) -> None:
    dataset, _ = seal(journal)

    # These all match the only sealed dataset. They are refused anyway: a
    # one-character reference stops being unique the moment a second dataset
    # is sealed, and `sbx run --data` must not mean different things on
    # different days.
    with pytest.raises(SbxError):
        resolve(dataset.sha256[:length])


def test_a_prefix_matching_nothing_is_refused(sbx_home: Path, journal: Path) -> None:
    dataset, _ = seal(journal)
    # Flip every character, so this cannot be a prefix of the sealed digest.
    missing = "".join("1" if c == "0" else "0" for c in dataset.sha256[:8])
    assert not dataset.sha256.startswith(missing)

    with pytest.raises(SbxError) as caught:
        resolve(missing)

    assert str(caught.value).strip() != ""


def test_resolve_refuses_an_ambiguous_prefix(sbx_home: Path, tmp_path: Path) -> None:
    first_path, second_path = journals_sharing_a_prefix(tmp_path / "search")

    first, _ = seal(first_path)
    second, _ = seal(second_path)
    assert first.sha256 != second.sha256

    shared = os.path.commonprefix([first.sha256, second.sha256])
    assert len(shared) >= 4

    with pytest.raises(SbxError) as caught:
        resolve(shared)

    # The message has to name the candidates, or the user has no next move.
    # `short` is a prefix of the full digest, so this holds whether the
    # message prints the short form or all 64 characters.
    message = str(caught.value)
    assert first.short in message
    assert second.short in message

    # Ambiguity ends exactly where the two digests diverge.
    assert resolve(first.sha256[: len(shared) + 1]) == first
    assert resolve(second.sha256[: len(shared) + 1]) == second


# ---------------------------------------------------------------------------
# all_datasets — the whole store, in one order, every time
# ---------------------------------------------------------------------------


def test_all_datasets_is_empty_before_anything_is_sealed(sbx_home: Path) -> None:
    assert not sbx_home.exists()

    assert all_datasets() == []

    # Listing an empty store is a read: `sbx ls` on a fresh machine must not
    # create the home directory as a side effect of being asked.
    assert not sbx_home.exists()


def test_all_datasets_returns_everything_sorted_by_hash(
    sbx_home: Path, tmp_path: Path
) -> None:
    sources = [
        journals.write_journal(
            tmp_path / "many" / f"journal-{index}.jsonl",
            [journals.trade(price=f"1182{index:02d}.00", at=AT, sequence=index)],
        )
        for index in range(6)
    ]
    digests = sorted(sha256_of(source) for source in sources)
    assert len(set(digests)) == len(sources)

    # Sealed in reverse hash order, so a store that reports insertion order or
    # raw directory order cannot pass by luck.
    for source in sorted(sources, key=sha256_of, reverse=True):
        seal(source)

    listed = all_datasets()

    assert [dataset.sha256 for dataset in listed] == digests
    assert listed == [load(digest) for digest in digests]


# ---------------------------------------------------------------------------
# atomicity — a dataset directory is complete or it is absent
# ---------------------------------------------------------------------------


def test_a_large_seal_leaves_no_temporary_files_behind(
    sbx_home: Path, tmp_path: Path
) -> None:
    records = [
        journals.trade(price=f"118{index % 1000:03d}.25", at=AT, sequence=index)
        for index in range(4000)
    ]
    big = journals.write_journal(tmp_path / "big.jsonl", records)

    dataset, newly_created = seal(big)

    assert newly_created is True
    assert dataset.records == len(records)
    assert dataset.bytes == big.stat().st_size
    assert dataset.verify() is True
    # Big enough that a copy cannot plausibly be one atomic write, so a
    # half-written `data.jsonl.tmp` or a scratch directory would surface here.
    assert sorted(p.name for p in dataset.directory.iterdir()) == DATASET_ENTRIES

    entries = list(paths.datasets_dir().iterdir())
    assert [entry.name for entry in entries] == [dataset.sha256]
    assert all(entry.is_dir() and HEX64.match(entry.name) for entry in entries)
