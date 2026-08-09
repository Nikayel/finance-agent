"""Component 2 — the sealed execution cell.

One subprocess per run, in a private empty working directory, under resource
caps, behind whatever OS confinement the platform can actually enforce, with a
host-side watchdog that escalates to `SIGKILL`.

Three decisions are worth knowing before reading the code.

**The strategy is copied into the workdir and run from there.** It never learns
its own original path, and the only directory it can reach is one that starts
empty. The interpreter is the *base* interpreter, not a virtualenv's, and it
runs with `-S`: no `site`, so no site-packages, so a strategy cannot import
sbx itself even on a platform with no filesystem confinement at all.

**Memory is policed by the host, not by `setrlimit`.** macOS refuses
`RLIMIT_AS` outright — see :mod:`sbx.limits` — so capping address space would
mean one mechanism and one reported outcome on Linux and different ones on
macOS. Instead the same loop that enforces the wall clock also watches
resident size. It costs a poll interval of latency and buys one behaviour to
reason about.

**Containment is reported, never assumed.** Every report carries the tuple of
mechanisms that were actually in force. A run on a platform without network
confinement says so, rather than quietly implying a guarantee it never had.
"""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import sandbox
from .errors import SbxError
from .limits import Limits, apply_rlimits

OUTCOMES: tuple[str, ...] = (
    "completed",
    "failed",
    "cpu_exhausted",
    "memory_exhausted",
    "timed_out",
    "killed",
)

STRATEGY_FILENAME = "strategy.py"

_POLL_SECONDS = 0.02
_RSS_POLL_SECONDS = 0.05
_DRAIN_GRACE_SECONDS = 2.0
_DIALOGUE_JOIN_SECONDS = 5.0
_READ_BYTES = 65536

# The host side of a conversation with the cell: given the cell's stdin and
# stdout, talk to it until it stops. Run on its own thread — see _talk.
Dialogue = Callable[[BinaryIO, BinaryIO], None]

# `ru_maxrss` is bytes on macOS and kilobytes on Linux. Getting this wrong is
# a factor of 1024 in a number people will quote.
_MAXRSS_SCALE = 1 if sys.platform == "darwin" else 1024


@dataclass(frozen=True)
class CellReport:
    """What one cell run did, and what stopped it."""

    outcome: str
    exit_code: int | None
    signal: int | None
    detail: str
    stdout: str
    stderr: str
    utime_us: int
    stime_us: int
    max_rss_bytes: int
    containment: tuple[str, ...]


def _interpreter() -> Path:
    """The base interpreter, never a virtualenv's shim.

    A venv's `python` lives in the project directory, which the sandbox denies
    reading — and the cell needs no site-packages anyway.
    """
    base = getattr(sys, "_base_executable", None) or sys.executable
    return Path(base).resolve()


def _environment(workdir: Path) -> dict[str, str]:
    """A deliberately small environment.

    `HOME` is absent on purpose: with it unset, expanding `~` yields the real
    home directory, which the sandbox then refuses — a more honest failure than
    pointing the strategy at a directory it is allowed to read.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(workdir),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _preexec(limits: Limits):
    """Runs in the child between fork and exec."""

    def preexec() -> None:
        # Its own session, so the watchdog can kill the whole group and not
        # just the process that happens to be on top.
        os.setsid()
        apply_rlimits(limits)

    return preexec


def _resident_bytes(pid: int) -> int | None:
    """The process's resident size, or None once it is gone."""
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    field = result.stdout.strip()
    if not field:
        return None
    try:
        return int(field.split()[0]) * 1024
    except ValueError:  # pragma: no cover
        return None


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
        pass


def _read_ready(
    selector: selectors.BaseSelector,
    chunks: dict[str, list[bytes]],
    timeout: float,
) -> bool:
    """Move whatever is readable into `chunks`; say whether anything was."""
    events = selector.select(timeout)
    for key, _ in events:
        data = key.fileobj.read1(_READ_BYTES)  # type: ignore[union-attr]
        if data:
            chunks[key.data].append(data)
        else:
            selector.unregister(key.fileobj)
    return bool(events)


def _drain(selector: selectors.BaseSelector, chunks: dict[str, list[bytes]]) -> None:
    """Collect what the child wrote before it died.

    Bounded, because a surviving grandchild can hold the write end of the pipe
    open and EOF would never arrive. Nothing readable means nothing left: the
    child has already been reaped, so everything it wrote is in the pipe.
    """
    deadline = time.monotonic() + _DRAIN_GRACE_SECONDS
    while selector.get_map() and time.monotonic() < deadline:
        if not _read_ready(selector, chunks, _POLL_SECONDS):
            break


def _classify(
    killed: tuple[str, str] | None,
    exit_code: int | None,
    term_signal: int | None,
    limits: Limits,
) -> tuple[str, str]:
    if killed is not None:
        return killed
    if term_signal == signal.SIGXCPU:
        return (
            "cpu_exhausted",
            f"killed by the kernel after {limits.cpu_seconds}s of CPU time",
        )
    if term_signal is not None:
        name = signal.Signals(term_signal).name
        return "killed", f"killed by signal {term_signal} ({name})"
    if exit_code == 0:
        return "completed", "ran to completion"
    return "failed", f"exited with status {exit_code}"


def _talk(dialogue: Dialogue, child: subprocess.Popen) -> None:
    """Run the host side of the conversation on its own thread.

    Blocking reads and writes are far easier to get right than a protocol state
    machine woven into the watchdog loop, and this way the watchdog remains the
    only thing that owns a deadline: when it kills the cell, these pipes break
    and this thread falls out of its loop on its own.

    A dialogue is expected to record its own failures — by the time an
    exception escapes to here there is no one left to tell, and a thread that
    dies noisily would take nothing useful with it.
    """
    try:
        dialogue(child.stdin, child.stdout)
    except BaseException:  # noqa: BLE001 - see docstring
        pass
    finally:
        try:
            if child.stdin is not None:
                child.stdin.close()
        except OSError:
            pass


def run(
    strategy_path: str | Path,
    *,
    limits: Limits = Limits(),
    attachments: Mapping[str, str | Path] | None = None,
    entry: str = STRATEGY_FILENAME,
    dialogue: Dialogue | None = None,
) -> CellReport:
    """Execute a strategy file in a sealed cell and report what happened.

    `attachments` are extra files to place in the working directory, keyed by
    the relative path they should take there — how the runtime and the wire
    format reach the cell without it having any way to import them from
    outside. `dialogue`, if given, is handed the cell's stdin and stdout and
    talks to it while the watchdog runs.
    """
    source = Path(strategy_path)
    if source.is_dir():
        raise SbxError(f"{source} is a directory, not a strategy")
    if not source.is_file():
        raise SbxError(f"no such strategy: {source}")

    workdir = Path(tempfile.mkdtemp(prefix="sbx-cell-")).resolve()
    profile_path: Path | None = None
    try:
        shutil.copyfile(source, workdir / STRATEGY_FILENAME)
        for relative, origin in (attachments or {}).items():
            target = workdir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
        if sandbox.available():
            handle, name = tempfile.mkstemp(prefix="sbx-profile-", suffix=".sb")
            profile_path = Path(name)
            # Written outside the workdir: sandbox-exec reads it before the
            # sandbox applies, so the cell never gets to see its own fence.
            with os.fdopen(handle, "w") as sb_file:
                sb_file.write(
                    sandbox.profile(workdir, readable=[_interpreter().parent.parent])
                )
        return _execute(workdir, profile_path, limits, entry, dialogue)
    finally:
        if profile_path is not None:
            profile_path.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)


def _execute(
    workdir: Path,
    profile_path: Path | None,
    limits: Limits,
    entry: str,
    dialogue: Dialogue | None,
) -> CellReport:
    argv = sandbox.wrap([str(_interpreter()), "-S", entry], profile_path)
    child = subprocess.Popen(
        argv,
        cwd=workdir,
        env=_environment(workdir),
        stdin=subprocess.PIPE if dialogue is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=_preexec(limits),
    )

    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    selector = selectors.DefaultSelector()
    if dialogue is None:
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
    # With a dialogue, stdout *is* the wire and belongs to the conversation.
    # The cell's runtime points the strategy's own prints at stderr, so
    # diagnostics still arrive here and a stray print cannot desynchronise it.
    selector.register(child.stderr, selectors.EVENT_READ, "stderr")

    talker: threading.Thread | None = None
    if dialogue is not None:
        talker = threading.Thread(
            target=_talk, args=(dialogue, child), name="sbx-dialogue", daemon=True
        )
        talker.start()

    deadline = time.monotonic() + limits.wall_seconds
    next_rss_poll = time.monotonic()
    killed: tuple[str, str] | None = None
    status: int | None = None
    usage = None

    try:
        while True:
            # Drain first: a child that fills its pipe and blocks would never
            # reach the exit the watchdog is waiting for.
            _read_ready(selector, chunks, _POLL_SECONDS)

            reaped, status, usage = os.wait4(child.pid, os.WNOHANG)
            if reaped == child.pid:
                break

            now = time.monotonic()
            if killed is not None:
                continue
            if now >= deadline:
                killed = (
                    "timed_out",
                    f"killed after {limits.wall_seconds:g}s of wall clock",
                )
            elif now >= next_rss_poll:
                next_rss_poll = now + _RSS_POLL_SECONDS
                resident = _resident_bytes(child.pid)
                if resident is not None and resident > limits.memory_bytes:
                    killed = (
                        "memory_exhausted",
                        f"killed after resident memory passed "
                        f"{limits.memory_bytes} bytes",
                    )
            if killed is not None:
                _kill_group(child.pid)

        _drain(selector, chunks)
    finally:
        selector.close()
        if talker is not None:
            # The cell is gone, so its end of the pipes is closed and the
            # conversation cannot still be blocked on one.
            talker.join(timeout=_DIALOGUE_JOIN_SECONDS)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:  # pragma: no cover - already broken
                    pass
        if status is None:
            # Leaving through an exception. Do not leave a live cell behind.
            _kill_group(child.pid)
            _, status, usage = os.wait4(child.pid, 0)
        # Popen must not try to reap a child os.wait4 already collected.
        child.returncode = os.waitstatus_to_exitcode(status)

    exit_code = None if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)
    term_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
    outcome, detail = _classify(killed, exit_code, term_signal, limits)

    return CellReport(
        outcome=outcome,
        exit_code=exit_code,
        signal=term_signal,
        detail=detail,
        stdout=b"".join(chunks["stdout"]).decode("utf-8", "replace"),
        stderr=b"".join(chunks["stderr"]).decode("utf-8", "replace"),
        utime_us=int(round(usage.ru_utime * 1_000_000)),
        stime_us=int(round(usage.ru_stime * 1_000_000)),
        max_rss_bytes=usage.ru_maxrss * _MAXRSS_SCALE,
        containment=sandbox.mechanisms(),
    )
