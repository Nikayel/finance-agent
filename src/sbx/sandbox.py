"""The OS boundary around a cell.

On macOS the cell is wrapped in `sandbox-exec` with a generated Seatbelt
profile that denies the network outright, denies every write outside the
private working directory, and denies reads of everything belonging to the
user — home, `/etc`, `/private/var`, the rest of `/private/tmp` — while
leaving the system libraries the interpreter needs readable. Measured on
Darwin 25.5 (arm64), unprivileged: outbound TCP and DNS both fail with a
catchable `OSError`, `/etc/passwd` and `$HOME` are unreadable, `/tmp` is
unwritable, and the workdir still works. Cost is about 4 ms per spawn.

The profile is written outside the working directory. `sandbox-exec` reads it
before the sandbox applies, so putting it somewhere the cell cannot read costs
nothing and means a strategy cannot inspect the fence it is inside.

Everywhere else — Linux included — there is no equivalent that works
unprivileged, and v1 does not pretend there is: :func:`wrap` returns the
command unchanged and :func:`mechanisms` omits ``network`` and ``filesystem``.
The cell records that tuple with the run, so a result can never claim
containment it did not have, and the tests that depend on those guarantees
skip with a stated reason rather than passing quietly.
"""

from __future__ import annotations

import functools
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")

_PROBE_PROFILE = "(version 1)(allow default)"
_PROBE_SECONDS = 10

# Reads of these subtrees are refused. Everything a user owns or configures is
# here; what is left is the system's own read-only libraries, which the
# interpreter needs to exist at all.
_DENIED_READ_SUBPATHS = (
    "/Users",
    "/home",
    "/private/etc",
    "/private/var",
    "/private/tmp",
    "/opt",
    "/Volumes",
)

_BASE_MECHANISMS = ("rlimits", "watchdog")
_SANDBOX_MECHANISMS = ("network", "filesystem")


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """Whether this platform can enforce network and filesystem confinement.

    The binary existing is not the question — `sandbox-exec` cannot nest inside
    an already-sandboxed process, and Apple has deprecated it without naming a
    removal date. So this actually runs it once and believes the result, rather
    than asserting a guarantee from a file listing.
    """
    if sys.platform != "darwin" or not SANDBOX_EXEC.exists():
        return False
    try:
        probe = subprocess.run(
            [str(SANDBOX_EXEC), "-p", _PROBE_PROFILE, "/usr/bin/true"],
            capture_output=True,
            timeout=_PROBE_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def mechanisms() -> tuple[str, ...]:
    """The containment actually in force here, named honestly."""
    if available():
        return _BASE_MECHANISMS + _SANDBOX_MECHANISMS
    return _BASE_MECHANISMS


def _quote(path: Path) -> str:
    """Escape a path for a Seatbelt string literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def profile(workdir: Path, readable: Iterable[Path] = ()) -> str:
    """A Seatbelt profile confining a cell to `workdir`.

    `readable` names trees the cell may still read — in practice the
    interpreter's own installation, which may itself sit under a denied prefix
    (a Homebrew, pyenv or CI toolcache Python all live under a user directory).
    Every path must already be resolved: macOS temp directories live behind a
    `/var` symlink and the kernel matches on the real path.
    """
    allowed = "\n  ".join(
        f'(subpath "{_quote(path)}")' for path in (workdir, *readable)
    )
    denied = "\n  ".join(f'(subpath "{path}")' for path in _DENIED_READ_SUBPATHS)
    return f"""(version 1)
(allow default)

; No egress of any kind. Connect and resolve both fail with a catchable error,
; which is more useful to a strategy author than a mysterious kill.
(deny network*)

; Nothing may be written outside the private working directory.
(deny file-write*)
(allow file-write* (subpath "{_quote(workdir)}") (literal "/dev/null"))

; Nothing belonging to the user may be read -- including the sealed datasets,
; which is the whole point: a strategy that could open the journal could read
; its own future. Reads are re-allowed afterwards for the workdir and the
; interpreter, because a later rule wins.
(deny file-read*
  {denied})
(allow file-read*
  {allowed})
"""


def wrap(argv: list[str], profile_path: Path | None) -> list[str]:
    """Return `argv` wrapped in the platform's confinement, if it has one."""
    if profile_path is None or not available():
        return argv
    return [str(SANDBOX_EXEC), "-f", str(profile_path), *argv]
