# Changelog

Every milestone records what it built **and what it deliberately did not**.
The second half is the interesting one: this project is defined as much by the
fence around it as by the code inside it.

## Milestone 7 — the demo

**Built.** `demo/demo.sh`: one command on a fresh clone that installs into a
throwaway venv, points `HOME` at a throwaway directory, builds a deterministic
journal, seals it, and runs three strategies against it — one that hunts for
data it has not been shown and reports every route being refused, one whose
better-looking P&L came from the wall clock, and the honest version of the same
idea. Then `sbx verify` on both: the first diverges with the divergent field
named, the second reproduces exactly.

**Found while writing it.** The first cheater was accidentally reproducible:
`time_ns() % 1000` is always zero on macOS, whose clock has microsecond
granularity. A demo that proves the wrong thing is worse than no demo.

**Deliberately not built.** No recorded terminal session, no asciinema, no
screenshots — the output in the README is pasted from a real run and is
reproducible by anyone who runs the script.

## Milestone 5 — the adversarial harness, and nothing else

**Built.** `tests/adversarial/` with a README stating the rule and
`tests/test_adversarial.py`, which discovers `strategy_*.py` files there, runs
each through the real CLI, and prints how each one failed. The harness is
honest about the directory being empty rather than reading as a suite somebody
switched off.

**Deliberately not built: the attacks.** They are written by hand, by the
repo's owner, and never by an agent — not a whole attack, not an example, not a
commented-out sketch. The gate was built by agents; if the attacks on it were
authored by the same process, the milestone would be testing one imagination
against itself and calling the agreement evidence. Whatever a builder failed to
think of while building, it will fail to think of again while attacking.

## Milestone 6 — determinism and `sbx verify`

**Built.** `sbx verify <run-id>` re-executes a recorded tuple and byte-diffs
the canonical result. Three verdicts, each a different claim: REPRODUCED;
DIVERGED with the first differing field named, searched in a fixed order so the
answer is the most summary one; and TAMPERED when the sealed dataset no longer
hashes to what the run recorded — in which case nothing was re-executed, so
nothing may be said about the code. Verify always reports whether the
environment matched, because claiming a clean reproduction while omitting that
it happened on another machine is exactly the small lie this project exists to
make impossible. Strategy sources are now kept content-addressed and read-only,
so a tuple stays re-executable after the original file is edited or deleted.
The seed is injected into the cell, which seeds both the generator handed to
the strategy and the module-level one, so a careless `import random` is pinned
too.

**Hardened, from an adversarial review of milestones 3 and 4.** The host's
output capture had no bound — a strategy writing to stderr in a loop buffered a
measured 11.8 GiB in three seconds, killing the machine before the watchdog's
verdict was returned. A kill that failed to land permanently disarmed the
deadline that issued it. An exception escaping the host-cell conversation was
swallowed, so a cell that wrote half a frame and exited zero was recorded as
having run to completion. Fills were stamped with the exchange's clock rather
than the simulated one, producing fills dated before the decisions that caused
them — the audit trail manufacturing the artefact it exists to disprove. An
order size of `Decimal("1E-20000000")` arrived in 22 bytes and rendered to a
gigabyte of text inside the host, which has no resource limits. A decision now
names the tick it answers, and the number of ticks a run answered is hashed, so
a strategy that stops on a peak is not indistinguishable from one that ran the
data out.

**Deliberately not built.** No `--force`, no re-recording, no way to overwrite
a run: a ledger you can edit is not a ledger. No statistical "close enough"
comparison — byte-identical or it diverged. No attempt to *prevent* a strategy
reading the clock: nothing running real code can, and the guarantee sbx offers
there is detection, which is why the determinism suite's mutation test asserts
that such a strategy's hashes differ.

## Milestone 4 — the time gate

**Built.** A framed wire between host and cell — 4-byte big-endian length,
then that many bytes of canonical JSON — where an oversized declared length is
refused *before* the body is read, and a truncated frame is an error while a
clean boundary is not. A feeder that turns a sealed journal into ticks on a
clock that only moves forward, carrying the journal's own timestamp text
rather than re-rendering it. A strategy-side `Market` whose only method that
returns anything is an iterator, and whose advance is what commits the current
tick's orders. `sbx run` end to end: seal, execute, record, and the run shows
up in `sbx ls`.

The gate is that the host sends one tick and then blocks. The next record is
never written to the pipe, so it is not in the cell's process memory, so a
strategy walking `dir()`, `gc.get_objects()` or `__subclasses__()` finds
nothing — there is nothing to find. Look-ahead is not detected here; it is
unrepresentable.

**Deliberately not built.** No order types beyond a market order, no partial
fills, no fees, no slippage model, no book reconstruction, no limit orders and
no cancels. An order fills at the price of the next trade after the tick it
was placed in, or it never fills at all — which is the honest answer for a
decision made at the end of the data, and one of the places a backtest usually
lies. No multi-record ticks, no history window in `Market`: a strategy that
wants history keeps its own, because state the host does not hold is state the
host does not have to be trusted about.

## Milestone 3 — the execution cell

**Built.** One subprocess per run in a private empty working directory, with
the strategy copied in so it never learns its own path, running the *base*
interpreter with `-S` so a strategy cannot import sbx even where there is no
filesystem confinement. On macOS a generated Seatbelt profile denies the
network, denies writes outside the workdir, and denies reads of everything the
user owns — `$HOME`, `/etc`, `/private/var`, other temp directories, and above
all `~/.sbx`, where the sealed datasets live. `RLIMIT_CPU`, `NOFILE`, `NPROC`
and `FSIZE` are applied between fork and exec; a host watchdog enforces wall
clock and resident memory and escalates to `killpg` + `SIGKILL`, which beats a
strategy that swallows `SIGTERM`. Every report carries the tuple of mechanisms
that were actually in force. All twelve hostile fixtures are contained.

**Two deviations from the original design, on purpose.** `setrlimit` does not
cap address space: macOS refuses `RLIMIT_AS` and `RLIMIT_DATA` outright, so
memory is policed by the host watchdog on every platform rather than one way
here and another way there. And Linux gets no network or filesystem
confinement in v1 — there is no unprivileged equivalent of Seatbelt on the
distributions that matter — so the cell reports `("rlimits", "watchdog")`
there and the tests that need more skip with a stated reason.

**Deliberately not built.** No container backend (that is the escalation
milestone, behind this same interface). No seccomp, no `chroot`, no attempt at
privilege separation — all need root. No in-process defences: no audit hooks,
no stripped builtins, no import allow-list. They are escapable by construction
and shipping them would suggest a boundary that is not there.

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
