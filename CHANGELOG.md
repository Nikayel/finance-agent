# Changelog

Every milestone records what it built **and what it deliberately did not**.
The second half is the interesting one: this project is defined as much by the
fence around it as by the code inside it.

## Milestone 2 — sealed datasets and the ledger

**Built.** Canonical encoding (`sbx.canonical`): sorted keys, no whitespace,
pure ASCII, floats refused at any depth, `Decimal` kept as text with its
trailing zeros intact. A content-addressed dataset store: `sbx seal` copies a
journal into `~/.sbx/datasets/<sha256>/`, marks it `0o444`, and writes a
manifest holding only content-derived facts — no filename, no path, no
timestamp — which is exactly what makes re-sealing the same bytes a no-op.
An append-only ledger where **the newline is the commit marker**: an
unterminated tail is an append that never happened and readers skip it in
silence, while damage to a committed line raises. The next append discards
that tail under an exclusive `flock`, because otherwise one interrupted write
would fuse with the following record and brick the ledger permanently.
`sbx ls` re-hashes every dataset as it lists it, so a single flipped byte
shows up as `TAMPERED` in one command.

**Deliberately not built.** No timestamps anywhere in the ledger — append
order is the only ordering that cannot disagree with itself, and a clock is
ambient nondeterminism in the one artefact that must not have any. No
compression, no dataset deletion or garbage collection, no `--force`, no
index or cache in front of the store: `ls` re-hashes from disk every time
because a store that only reports what it was told is not a store you can
audit.

## Milestone 1 — package and CLI shell

**Built.** An installable package with a `sbx` console script and four
argparse subcommands (`seal`, `run`, `verify`, `ls`), each with its real
argument surface, plus `--version`. The verb list has exactly one definition —
a command registry that the parser, the tests and `scripts/smoke.sh` all read
from — so a fifth verb cannot appear without a test failing. Expected failures
raise `SbxError` and surface as a one-line message on stderr with exit 1;
anything else is a bug and is allowed to traceback. Added a fresh-venv install
smoke script and CI on macOS and Linux across Python 3.11 and 3.13.

**Deliberately not built.** Any actual behaviour: every verb parses correctly
and then reports that it is not implemented yet. No logging framework, no
config file, no `--verbose`, no plugin system, no lint or type-check
dependency — the ground rule is stdlib-only with `pytest` as the single dev
extra, and adding tooling deps would break the claim the README makes.
