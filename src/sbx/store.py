"""Component 3, the immutable half — the content-addressed dataset store.

Sealing a journal copies its bytes into ``~/.sbx/datasets/<sha256>/`` and makes
them read-only. The directory name *is* the hash of the contents, so the store
has no notion of a dataset changing: a different byte is a different dataset.

The manifest deliberately records only what the content itself determines —
size, record count, digest. No original filename, no path, no "sealed at"
timestamp. That is what makes sealing genuinely idempotent: the same bytes
arriving twice, under different names, from different directories, produce
byte-identical manifests and a single stored copy. Provenance would quietly
turn two identical datasets into two different ones, and a timestamp would put
ambient nondeterminism inside the one artefact that must never have any.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import canonical, paths
from .errors import SbxError

DATA_FILENAME = "data.jsonl"
MANIFEST_FILENAME = "manifest.json"

_HASH_CHARS = 64
_HASH_ALPHABET = frozenset("0123456789abcdef")
_SHORT_CHARS = 12
_MIN_PREFIX_CHARS = 4
_READ_ONLY = 0o444
_CHUNK_BYTES = 1 << 20
_STAGING_PREFIX = ".sealing-"


@dataclass(frozen=True)
class SealedDataset:
    """An immutable snapshot of a journal, identified by its own contents."""

    sha256: str
    bytes: int
    records: int
    directory: Path

    @property
    def data_path(self) -> Path:
        return self.directory / DATA_FILENAME

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_FILENAME

    @property
    def short(self) -> str:
        """The prefix humans actually type."""
        return self.sha256[:_SHORT_CHARS]

    def verify(self) -> bool:
        """Re-hash the stored bytes and check they are still what we sealed."""
        try:
            return canonical.hash_file(self.data_path) == self.sha256
        except OSError:
            return False


def _check_hash(value: str) -> str:
    """Reject anything that is not a full digest, path separators included."""
    if len(value) != _HASH_CHARS or not _HASH_ALPHABET.issuperset(value):
        raise SbxError(f"not a dataset hash: {value!r}")
    return value


def _count_records(source: Path) -> int:
    """Complete, newline-terminated lines. A torn final line is not a record."""
    count = 0
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            count += chunk.count(b"\n")
    return count


def seal(journal_path: str | Path) -> tuple[SealedDataset, bool]:
    """Snapshot a journal into the store.

    Returns the dataset and whether this call is what created it, so callers
    can record a first sealing in the ledger without recording re-seals.
    """
    source = paths.require_file(journal_path, "journal")

    size = source.stat().st_size
    if size == 0:
        raise SbxError(f"{source} is empty: a journal with no records cannot be sealed")

    digest = canonical.hash_file(source)
    directory = paths.datasets_dir() / digest
    if directory.exists():
        return load(digest), False

    manifest = {"bytes": size, "records": _count_records(source), "sha256": digest}

    datasets = paths.datasets_dir()
    datasets.mkdir(parents=True, exist_ok=True)
    # Stage in a sibling directory and rename into place, so a dataset
    # directory never exists in a half-written state for another process — or
    # a `sbx ls` — to find.
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=datasets))
    created = True
    try:
        data = staging / DATA_FILENAME
        shutil.copyfile(source, data)
        manifest_file = staging / MANIFEST_FILENAME
        manifest_file.write_bytes(canonical.encode_line(manifest))
        os.chmod(data, _READ_ONLY)
        os.chmod(manifest_file, _READ_ONLY)
        try:
            os.rename(staging, directory)
        except OSError:
            # Another sealer won the race. Its copy is byte-identical by
            # construction — that is the whole point of content addressing.
            created = False
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return load(digest), created


def keep_code(strategy_path: str | Path) -> tuple[str, Path]:
    """Copy a strategy into the store, content-addressed and read-only.

    A run tuple names its code by hash. Without a kept copy that name resolves
    to nothing the moment someone edits or deletes the file, and `sbx verify`
    could only ever check runs whose source happened to survive.
    """
    source = paths.require_file(strategy_path, "strategy")

    digest = canonical.hash_file(source)
    directory = paths.code_dir()
    target = directory / f"{digest}.py"
    if target.exists():
        return digest, target

    directory.mkdir(parents=True, exist_ok=True)
    handle, staged_name = tempfile.mkstemp(prefix=_STAGING_PREFIX, dir=directory)
    os.close(handle)
    staged = Path(staged_name)
    try:
        shutil.copyfile(source, staged)
        os.chmod(staged, _READ_ONLY)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return digest, target


def code_path(sha256: str) -> Path:
    """The kept copy of a strategy, by the hash a run recorded."""
    _check_hash(sha256)
    target = paths.code_dir() / f"{sha256}.py"
    if not target.is_file():
        raise SbxError(
            f"the strategy for {sha256[:12]} is no longer in the store: {target}"
        )
    return target


def load(sha256: str) -> SealedDataset:
    """Load a sealed dataset by its full digest."""
    _check_hash(sha256)
    directory = paths.datasets_dir() / sha256
    try:
        manifest = canonical.decode((directory / MANIFEST_FILENAME).read_bytes())
    except FileNotFoundError:
        raise SbxError(f"no sealed dataset {sha256}") from None

    try:
        return SealedDataset(
            sha256=manifest["sha256"],
            bytes=manifest["bytes"],
            records=manifest["records"],
            directory=directory,
        )
    except (KeyError, TypeError) as error:
        raise SbxError(f"manifest for {sha256} is malformed: {error}") from error


def resolve(prefix: str) -> SealedDataset:
    """Load a sealed dataset by an unambiguous prefix of its digest."""
    prefix = prefix.lower()
    if len(prefix) < _MIN_PREFIX_CHARS:
        raise SbxError(
            f"dataset prefix {prefix!r} is too short; "
            f"use at least {_MIN_PREFIX_CHARS} characters"
        )

    matches = [dataset for dataset in all_datasets() if dataset.sha256.startswith(prefix)]
    if not matches:
        raise SbxError(f"no sealed dataset matches {prefix!r}")
    if len(matches) > 1:
        candidates = ", ".join(dataset.sha256 for dataset in matches)
        raise SbxError(f"dataset prefix {prefix!r} is ambiguous: {candidates}")
    return matches[0]


def all_datasets() -> list[SealedDataset]:
    """Every sealed dataset, ordered by digest."""
    try:
        names = sorted(entry.name for entry in paths.datasets_dir().iterdir())
    except FileNotFoundError:
        return []
    return [
        load(name)
        for name in names
        if len(name) == _HASH_CHARS and _HASH_ALPHABET.issuperset(name)
    ]
