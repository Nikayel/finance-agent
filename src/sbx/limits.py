"""Resource caps, and honesty about which ones a kernel will accept.

Two measured facts shape this module.

**macOS refuses `RLIMIT_AS` and `RLIMIT_DATA` outright.** `setrlimit` returns an
error rather than a smaller limit, so address space cannot be capped there at
all. Rather than cap memory one way on Linux and another on macOS — and report
two different outcomes for the same runaway allocation — memory is policed in
one place for everyone: the host watches the cell's resident size and kills it,
exactly as it already does for wall clock. One mechanism, one outcome, one
thing to explain.

**`RLIMIT_NPROC` is per-user, not per-process.** The account running sbx
already has hundreds of processes, so any small cap means the cell cannot fork
at all. That is precisely what we want from a strategy sandbox, and it is why
the default is 1: at a cap of one, `fork` fails with `EAGAIN` on every machine
instead of only on busy ones.
"""

from __future__ import annotations

import resource
from dataclasses import dataclass

# Caps that exist on every POSIX target and that both kernels honour.
_RLIMITS = (
    ("open_files", "RLIMIT_NOFILE"),
    ("processes", "RLIMIT_NPROC"),
    ("file_size_bytes", "RLIMIT_FSIZE"),
)


@dataclass(frozen=True)
class Limits:
    """What a single cell run is allowed to consume."""

    cpu_seconds: int = 5
    memory_bytes: int = 512 * 1024 * 1024
    open_files: int = 64
    processes: int = 1
    file_size_bytes: int = 8 * 1024 * 1024
    wall_seconds: float = 10.0


def apply_rlimits(limits: Limits) -> None:
    """Set every cap the kernel accepts.

    Runs in the child between `fork` and `exec`, so it must not raise for a
    limit this platform declines — the caller cannot see the exception in any
    useful form, and the containment that *did* apply is still worth having.
    """
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    # CPU gets a second of headroom between its soft and hard limits. With them
    # equal, Linux reaches both in the same instant and delivers SIGKILL, so
    # the kill is reported as an anonymous "killed" rather than as the CPU
    # limit doing its job. A second of grace gets SIGXCPU on both kernels, and
    # the wall-clock watchdog is still there if the process ignores it.
    try:
        resource.setrlimit(
            resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 1)
        )
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        pass

    for field, rlimit_name in _RLIMITS:
        rlimit = getattr(resource, rlimit_name, None)
        if rlimit is None:  # pragma: no cover - every target has these
            continue
        value = getattr(limits, field)
        try:
            resource.setrlimit(rlimit, (value, value))
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            continue
