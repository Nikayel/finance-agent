"""Milestone 3 — the sealed execution cell.

The cell is the only thing standing between an untrusted strategy and the
machine sbx runs on, so these tests are about what the *operating system*
actually did, never about what the cell meant to do. Every attack here is a
real fixture from ``tests/hostile/``, handed to :func:`sbx.cell.run` and to
nothing else, and every containment claim is checked against a real kernel:
a test that patched ``setrlimit``, ``os.fork`` or ``signal`` would assert
something about the patch.

Four properties carry the milestone:

* **The attack never wins.** Every hostile fixture exits ``97`` only when its
  attack succeeded, so ``exit_code == 97`` is the loudest failure this suite
  can report — louder than a wrong outcome name.
* **The cell tells the truth about what it enforced.** ``containment`` names
  the mechanisms that were *actually* applied to this run, matching the
  "Containment, precisely" table in the README. Network and filesystem
  confinement come from ``sandbox-exec``, which is macOS-only, so on a platform
  that cannot deliver them the tests that would "prove" them are **skipped with
  a reason** rather than passing on a guarantee nobody made.
* **The accounting is honest.** CPU time is integer microseconds and memory is
  bytes, measured from the child, because a float has no canonical encoding
  and a run's cost ends up in the ledger.
* **The host survives.** After a fork bomb, a spin and a process that refuses
  to die, a trivial strategy still completes.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from sbx.cell import OUTCOMES, CellReport, Limits, run
from sbx.errors import SbxError

# The attack fixtures are inputs, not tests. Nothing in this file may execute
# one directly — a fork bomb or a 10 GB allocation outside the cell hits this
# machine, not a subprocess. They only ever travel as a path into `run`.
HOSTILE = Path(__file__).resolve().parent / "hostile"

# `97` is the fixtures' shared convention: reaching it means the sandbox failed
# to stop the attack. See tests/hostile/README.md.
ATTACK_SUCCEEDED = 97

EXPECTED_OUTCOMES = (
    "completed",
    "failed",
    "cpu_exhausted",
    "memory_exhausted",
    "timed_out",
    "killed",
)

# Everything `containment` is allowed to name. A mechanism outside this set is
# either a typo or a guarantee nobody agreed to.
MECHANISMS = ("rlimits", "watchdog", "network", "filesystem")

# A minimal environment is a handful of variables, not the host's. If this
# ceiling ever needs raising, the new variable needs a reason.
MAX_ENV_VARS = 12

# Names whose presence would mean the host's environment leaked through: the
# strategy's home, the developer's account and shell, an agent socket it could
# authenticate with, and the paths that give away the repo it is running in.
LEAKY_NAMES = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "SSH_AUTH_SOCK",
    "VIRTUAL_ENV",
    "PWD",
    "OLDPWD",
    "PYTHONPATH",
)

# One short wall budget, reused, so the suite stays cheap enough for CI.
WALL = 1.5
QUICK = Limits(cpu_seconds=2, wall_seconds=WALL)

# The refusal fixtures carry their own timeouts (four TCP targets at 3 s each,
# plus DNS), so their wall budget has to exceed the worst case. A shorter one
# would report `timed_out` and hide whether the attempt was refused at all.
# RLIMIT_NPROC is per-user, so a cap of 1 is the only value that fails on every
# machine rather than only on busy ones — see "Containment, precisely" in the
# README. It is what stops the shell escape.
REFUSAL = Limits(processes=1, cpu_seconds=5, wall_seconds=20.0)

MARKER = "sbx-cell-marker-4b1c"

TRIVIAL = f"""\
print({MARKER!r})
"""

# A probe, not an attack: the only witness to where the cell put the strategy,
# since CellReport deliberately exposes no workdir path.
REPORT_WORKDIR = """\
import json
import os

print(json.dumps({
    "cwd": os.path.realpath(os.getcwd()),
    "file": os.path.realpath(__file__),
    "listing": sorted(os.listdir(".")),
}))
"""

ENV_VALUES = """\
import json
import os

print(json.dumps({
    "hashseed": os.environ.get("PYTHONHASHSEED"),
    "has_home": "HOME" in os.environ,
    "names": sorted(os.environ),
}))
"""

HASH_PROBE = """\
print(hash("sbx"), hash(("a", "b")))
"""

# The cell runs the base interpreter with `-S`, so site-packages never lands on
# sys.path and the host's own package is not importable from inside.
IMPORT_HOST = """\
import json

seen = {}
for name in ("sbx", "pytest", "_pytest"):
    try:
        __import__(name)
    except ImportError:
        seen[name] = "refused"
    else:
        seen[name] = "imported"
print(json.dumps(seen))
"""

RAISES = """\
print("stdout survived the crash", flush=True)
raise ValueError("deliberate detonation")
"""

# ~0.27 s of user time on any machine that can run this suite — enough that the
# gap against a trivial run cannot be measurement noise.
BUSY = """\
total = 0
for i in range(6_000_000):
    total += i * i
print(total)
"""

# Half a megabyte on each stream: past any pipe buffer, so a cell that waits
# for the child before draining the pipes deadlocks here instead of passing.
NOISY = """\
import sys

BLOCK = "o" * 1024
for _ in range(512):
    sys.stdout.write(BLOCK)
    sys.stderr.write(BLOCK.replace("o", "e"))
sys.stdout.write("\\nSTDOUT_END\\n")
sys.stderr.write("\\nSTDERR_END\\n")
"""

BIG_FILE = """\
BLOCK = b"x" * (64 * 1024)
with open("fat.bin", "wb") as handle:
    for _ in range(128):  # 8 MiB, far past the cap the test sets
        handle.write(BLOCK)
        handle.flush()  # so the limit is hit mid-loop, not at close
print("WROTE_IT_ALL")
"""

# The three that respectively multiply, burn and refuse to die. The memory bomb
# has its own test because it needs a much bigger wall budget than these.
NASTIEST = (
    ("strategy_fork_bomb.py", Limits(processes=1, wall_seconds=WALL)),
    ("strategy_cpu_spin.py", Limits(cpu_seconds=1, wall_seconds=5.0)),
    ("strategy_ignore_sigterm.py", Limits(wall_seconds=WALL)),
)

# Each of these ends in `exit 0` only because every attempt it made was
# refused, and in `exit 97` the moment one succeeds. The second element is the
# mechanism that has to be enforced for the question to mean anything.
REFUSED_ATTACKS = [
    pytest.param("strategy_socket_connect.py", "network", id="socket-connect"),
    pytest.param("strategy_dns_lookup.py", "network", id="dns-lookup"),
    pytest.param("strategy_read_outside_workdir.py", "filesystem", id="read-outside"),
    pytest.param("strategy_write_outside_workdir.py", "filesystem", id="write-outside"),
    # Shelling out is stopped by RLIMIT_NPROC, not by Seatbelt, so this one is
    # contained on every platform and must never skip.
    pytest.param("strategy_subprocess_escape.py", "rlimits", id="subprocess-escape"),
]


def hostile(name: str) -> Path:
    """The path of an attack fixture — handed to the cell, never executed here."""
    path = HOSTILE / name
    assert path.is_file(), f"missing hostile fixture: {path}"
    return path


def write_strategy(directory: Path, source: str, name: str = "probe.py") -> Path:
    """Put a strategy of our own on disk, well away from the hostile set."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def last_json_line(report: CellReport) -> dict[str, Any]:
    """The JSON object a probe strategy printed as its final line."""
    return json.loads(report.stdout.strip().splitlines()[-1])


def one_sentence(detail: str) -> bool:
    """A `detail` fit to print: non-empty, and a single line."""
    return detail.strip() != "" and "\n" not in detail and "\r" not in detail


@functools.lru_cache(maxsize=1)
def enforced() -> tuple[str, ...]:
    """What this machine actually contains, measured once by a real run."""
    with tempfile.TemporaryDirectory() as scratch:
        probe = write_strategy(Path(scratch), TRIVIAL)
        return run(probe).containment


def require(mechanism: str) -> None:
    """Skip — loudly, and only for the one honest reason — when v1 has no `mechanism`.

    Network and filesystem confinement come from ``sandbox-exec``, which exists
    only on macOS. Where it is missing the guarantee is *absent*, not merely
    unverified, so the attack below was never actually refused and a green test
    would be a lie. The reason lives here so it is written once.
    """
    delivered = enforced()
    if mechanism not in delivered:
        pytest.skip(
            f"{mechanism} confinement is not delivered on {sys.platform}: v1 gets "
            f"it from sandbox-exec, which is macOS-only, and this run enforced "
            f"only {list(delivered)}. Skipped, not passed — the guarantee is "
            f"absent, so the attack was never refused."
        )


# ---------------------------------------------------------------------------
# the shape of the contract — outcomes, limits, and an immutable report
# ---------------------------------------------------------------------------


def test_outcomes_is_exactly_the_six_names() -> None:
    assert isinstance(OUTCOMES, tuple)
    assert OUTCOMES == EXPECTED_OUTCOMES
    assert len(set(OUTCOMES)) == len(OUTCOMES)


def test_limits_defaults_are_the_documented_ones() -> None:
    limits = Limits()

    assert limits.cpu_seconds == 5
    assert limits.memory_bytes == 512 * 1024 * 1024
    assert limits.open_files == 64
    # One, not a comfortable-looking 32: RLIMIT_NPROC counts processes per
    # *user*, and the account running sbx already has hundreds. Any larger cap
    # is one the cell never actually receives, so it would block forking on a
    # busy machine and quietly permit it on an idle one.
    assert limits.processes == 1
    assert limits.file_size_bytes == 8 * 1024 * 1024
    assert limits.wall_seconds == 10.0


def test_limits_is_a_frozen_value() -> None:
    limits = Limits()

    assert dataclasses.is_dataclass(limits)
    # A caller that could edit the limits mid-run could relax them mid-run.
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.cpu_seconds = 3600  # type: ignore[misc]
    assert Limits() == Limits()  # a plain value, comparable by its fields


def test_a_report_is_an_immutable_value(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, TRIVIAL), limits=QUICK)

    assert isinstance(report, CellReport)
    assert dataclasses.is_dataclass(report)
    assert report.outcome in OUTCOMES
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.outcome = "completed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# a well-behaved strategy — the case everything else is measured against
# ---------------------------------------------------------------------------


def test_a_well_behaved_strategy_completes(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, TRIVIAL), limits=QUICK)

    assert report.outcome == "completed"
    assert report.exit_code == 0
    assert report.signal is None
    assert MARKER in report.stdout
    assert report.stderr == ""
    assert one_sentence(report.detail)


def test_run_accepts_the_string_path_the_cli_hands_it(tmp_path: Path) -> None:
    report = run(str(write_strategy(tmp_path, TRIVIAL)), limits=QUICK)

    assert report.outcome == "completed"
    assert MARKER in report.stdout


def test_run_uses_the_default_limits_when_none_are_given(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, TRIVIAL))

    assert report.outcome == "completed"
    assert report.exit_code == 0


# ---------------------------------------------------------------------------
# the private working directory — empty, anonymous, and gone afterwards
# ---------------------------------------------------------------------------


def test_the_strategy_is_copied_in_as_strategy_py_and_run_from_there(
    tmp_path: Path,
) -> None:
    source = write_strategy(tmp_path, REPORT_WORKDIR, name="apex_reversal_9f3a.py")

    report = run(source, limits=QUICK)

    assert report.outcome == "completed"
    seen = last_json_line(report)
    assert Path(seen["file"]).name == "strategy.py"
    assert Path(seen["file"]).parent == Path(seen["cwd"])  # it runs from the copy
    # Empty apart from the copy: nothing of the host's, and nothing left over
    # from the run before it.
    assert seen["listing"] == ["strategy.py"]
    assert Path(seen["cwd"]) != tmp_path
    # A copy, not a move: the caller's file is still where the caller put it.
    assert source.read_text(encoding="utf-8") == REPORT_WORKDIR


def test_the_strategy_never_learns_where_it_came_from(tmp_path: Path) -> None:
    source = write_strategy(tmp_path, REPORT_WORKDIR, name="apex_reversal_9f3a.py")

    report = run(source, limits=QUICK)

    # The original name and directory are provenance for the ledger to record.
    # Inside the cell they are one more thing a strategy could key a cheat off,
    # and the fixed name `strategy.py` is what makes the copy anonymous.
    assert "apex_reversal_9f3a" not in report.stdout
    assert str(tmp_path) not in report.stdout


def test_every_run_gets_a_fresh_workdir_and_none_of_them_survive(
    tmp_path: Path,
) -> None:
    strategy = write_strategy(tmp_path, REPORT_WORKDIR)

    workdirs = []
    for _ in range(5):
        report = run(strategy, limits=QUICK)
        assert report.outcome == "completed"
        workdirs.append(Path(last_json_line(report)["cwd"]))

    # CellReport exposes no workdir path, so the strategy's own os.getcwd() is
    # the only honest witness — and it settles both halves of the claim at once.
    # Counting entries in the system temp directory would settle neither: other
    # processes create and remove files there while this test runs.
    assert len(set(workdirs)) == 5
    for workdir in workdirs:
        assert not workdir.exists()


# ---------------------------------------------------------------------------
# the scrubbed environment — HOME absent, hash seed pinned, nothing of the host
# ---------------------------------------------------------------------------


def test_the_environment_handed_to_a_strategy_is_scrubbed() -> None:
    report = run(hostile("strategy_env_snoop.py"), limits=QUICK)

    assert report.outcome == "completed"
    assert report.exit_code == 0

    lines = report.stdout.splitlines()
    names, count = lines[:-1], lines[-1]
    assert count.startswith("ENV_VAR_COUNT=")
    assert len(names) == int(count.removeprefix("ENV_VAR_COUNT="))

    for leak in LEAKY_NAMES:
        assert leak not in names
    assert "PYTHONHASHSEED" in names
    assert len(names) <= MAX_ENV_VARS
    assert len(names) < len(os.environ)  # emphatically not the host's environment


def test_pythonhashseed_is_zero_and_home_is_absent(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, ENV_VALUES), limits=QUICK)

    assert report.outcome == "completed"
    seen = last_json_line(report)
    assert seen["hashseed"] == "0"
    # Absent, not empty: a strategy that finds HOME="" still finds HOME, and
    # os.path.expanduser would happily build a path out of it.
    assert seen["has_home"] is False
    assert "HOME" not in seen["names"]


def test_the_strategy_cannot_import_the_host_package(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, IMPORT_HOST), limits=QUICK)

    assert report.outcome == "completed"
    # `import sbx` inside the cell would hand a strategy the ledger, the sealed
    # datasets and the cell's own source. The base interpreter run with `-S`
    # keeps site-packages off sys.path, which is what makes that an ImportError
    # rather than a policy.
    assert last_json_line(report) == {
        "sbx": "refused",
        "pytest": "refused",
        "_pytest": "refused",
    }


def test_string_hashing_is_identical_across_runs(tmp_path: Path) -> None:
    strategy = write_strategy(tmp_path, HASH_PROBE)

    first = run(strategy, limits=QUICK)
    second = run(strategy, limits=QUICK)

    assert first.outcome == "completed"
    # The point of PYTHONHASHSEED=0 is not that the variable is set but that it
    # reaches the interpreter before it starts — too late from inside, and two
    # runs of the same strategy would disagree about dict and set ordering.
    assert first.stdout == second.stdout
    assert first.stdout.strip() != ""


# ---------------------------------------------------------------------------
# containment, one test per attack — rlimits and the watchdog
# ---------------------------------------------------------------------------


def test_a_cpu_spin_is_killed_by_the_cpu_limit() -> None:
    limits = Limits(cpu_seconds=2)  # the default 10 s wall leaves the cap room

    started = time.monotonic()
    report = run(hostile("strategy_cpu_spin.py"), limits=limits)
    elapsed = time.monotonic() - started

    assert report.outcome == "cpu_exhausted"
    assert report.signal == signal.SIGXCPU
    assert int(signal.SIGXCPU) == 24
    assert report.exit_code is None
    # Single-threaded, so two CPU-seconds cannot elapse in less than two wall
    # seconds; anything under ten means the watchdog is not what stopped it.
    assert 1.5 <= elapsed < 10.0
    assert report.utime_us >= 1_000_000
    assert "cpu" in report.detail.lower()
    assert one_sentence(report.detail)


def test_a_process_that_only_sleeps_is_killed_by_the_wall_clock_watchdog() -> None:
    started = time.monotonic()
    report = run(hostile("strategy_sleep_forever.py"), limits=Limits(wall_seconds=WALL))
    elapsed = time.monotonic() - started

    assert report.outcome == "timed_out"
    # The watchdog kills hard — `killpg` plus SIGKILL, never a polite SIGTERM
    # phase first, which would report SIGTERM here and is exactly what
    # strategy_ignore_sigterm proves is worthless.
    assert report.signal == signal.SIGKILL
    assert report.exit_code is None
    assert elapsed >= WALL  # it really did get its whole budget
    assert elapsed < 2 * WALL  # and the watchdog did not oversleep
    # It burned no CPU at all, so RLIMIT_CPU could never have ended it.
    assert report.utime_us < 1_000_000
    assert "wall" in report.detail.lower()
    assert one_sentence(report.detail)


def test_a_process_that_swallows_sigterm_is_killed_anyway() -> None:
    started = time.monotonic()
    report = run(
        hostile("strategy_ignore_sigterm.py"), limits=Limits(wall_seconds=WALL)
    )
    elapsed = time.monotonic() - started

    assert report.outcome == "timed_out"
    assert report.signal == signal.SIGKILL
    assert report.exit_code is None
    # Output written before the process became unkillable politely still has to
    # come back: a cell that only reads the pipes after a clean exit loses it.
    assert "HANDLERS_INSTALLED sigterm+sigint swallowed" in report.stdout
    assert elapsed >= WALL
    assert elapsed < 2 * WALL
    assert one_sentence(report.detail)


def test_a_memory_bomb_is_contained() -> None:
    limits = Limits(memory_bytes=256 * 1024 * 1024, wall_seconds=20.0)

    report = run(hostile("strategy_memory_bomb.py"), limits=limits)

    assert report.exit_code != ATTACK_SUCCEEDED, (
        "the strategy committed ~10 GB and exited 97: the memory limit did not "
        "hold and containment is broken"
    )
    assert report.outcome == "memory_exhausted"
    # macOS refuses RLIMIT_AS outright, so memory is the host watchdog polling
    # RSS and killing — one mechanism and one outcome on both platforms, at the
    # cost of overshooting the cap by a poll interval.
    assert report.signal == signal.SIGKILL
    assert report.exit_code is None
    assert "ALLOCATED" not in report.stdout
    # It really did get big before it died: a cap enforced at the first byte
    # would be reported the same way and would be a different mechanism.
    assert report.max_rss_bytes > 64 * 1024 * 1024
    assert "memory" in report.detail.lower()
    assert one_sentence(report.detail)


def test_a_fork_bomb_is_contained_and_the_host_survives(tmp_path: Path) -> None:
    # RLIMIT_NPROC counts every process the *user* owns, and this account
    # already has hundreds, so 1 is the only cap that makes `fork` fail on a
    # quiet machine as reliably as on a busy one.
    limits = Limits(processes=1, wall_seconds=WALL)

    started = time.monotonic()
    report = run(hostile("strategy_fork_bomb.py"), limits=limits)
    elapsed = time.monotonic() - started

    assert report.exit_code != ATTACK_SUCCEEDED
    assert report.outcome in OUTCOMES
    # The cell has to reap the whole tree rather than wait on a child that keeps
    # producing new children, so `run` returns on roughly the wall budget.
    assert elapsed < 4 * WALL
    assert one_sentence(report.detail)

    # The only proof that matters: this machine is still able to run anything.
    survivor = run(write_strategy(tmp_path, TRIVIAL), limits=QUICK)
    assert survivor.outcome == "completed"
    assert MARKER in survivor.stdout


def test_file_descriptor_exhaustion_is_contained() -> None:
    limits = Limits(open_files=64, wall_seconds=10.0)

    report = run(hostile("strategy_fd_exhaustion.py"), limits=limits)

    assert report.exit_code != ATTACK_SUCCEEDED, (
        "the strategy opened more than 100000 descriptors: RLIMIT_NOFILE did "
        "not hold and containment is broken"
    )
    assert report.outcome == "completed"
    assert report.exit_code == 0  # the fixture exits 0 the moment it hits EMFILE
    assert "OPENED" not in report.stdout


def test_a_strategy_cannot_write_a_file_past_the_size_limit(tmp_path: Path) -> None:
    limits = Limits(file_size_bytes=1024 * 1024, wall_seconds=10.0)

    report = run(write_strategy(tmp_path, BIG_FILE), limits=limits)

    # Which outcome a SIGXFSZ maps to is not specified, so only the containment
    # itself is pinned: the 8 MiB file never finished being written.
    assert "WROTE_IT_ALL" not in report.stdout
    assert report.outcome != "completed"
    assert report.exit_code != 0  # None when it was killed by the signal
    assert one_sentence(report.detail)


# ---------------------------------------------------------------------------
# escapes — the network, the filesystem, and the shell
#
# Network and filesystem confinement come from sandbox-exec, which is macOS
# only; there is no unprivileged v1 equivalent on Linux, so `containment` omits
# them there and these tests skip with that reason rather than passing on a
# guarantee that was never made. The shell escape is different: RLIMIT_NPROC
# stops it everywhere, so that one must never skip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "mechanism"), REFUSED_ATTACKS)
def test_every_escape_attempt_is_refused(fixture: str, mechanism: str) -> None:
    require(mechanism)

    report = run(hostile(fixture), limits=REFUSAL)

    assert report.exit_code != ATTACK_SUCCEEDED, (
        f"{fixture} exited {ATTACK_SUCCEEDED}: its attack SUCCEEDED and "
        f"{mechanism} containment is BROKEN. The strategy said: "
        f"{report.stdout.strip()!r}"
    )
    assert report.exit_code == 0
    assert report.outcome == "completed"
    assert report.signal is None
    # Each of these prints only on a breach, so silence is the second signature
    # of a refusal and does not depend on reading the exit code right.
    assert report.stdout.strip() == ""
    assert mechanism in report.containment


def test_containment_names_only_mechanisms_that_were_enforced(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, TRIVIAL), limits=QUICK)

    assert isinstance(report.containment, tuple)
    assert all(isinstance(name, str) for name in report.containment)
    assert len(set(report.containment)) == len(report.containment)
    assert set(report.containment) <= set(MECHANISMS)
    # These two need no help from the platform, so a run that cannot claim them
    # is not a cell at all.
    assert "rlimits" in report.containment
    assert "watchdog" in report.containment


def test_network_and_filesystem_are_claimed_exactly_where_they_are_delivered(
    tmp_path: Path,
) -> None:
    report = run(write_strategy(tmp_path, TRIVIAL), limits=QUICK)
    optional = {"network", "filesystem"} & set(report.containment)

    if sys.platform == "darwin":
        # sandbox-exec ships with macOS, so a run here that silently dropped it
        # would leave the strategies above unconfined and this suite green.
        assert optional == {"network", "filesystem"}
    else:
        # Claiming a guarantee v1 does not implement is the exact failure this
        # tuple exists to prevent.
        assert optional == set()


# ---------------------------------------------------------------------------
# the host never dies — the whole point, asserted end to end
# ---------------------------------------------------------------------------


def test_the_host_survives_the_nastiest_fixtures_back_to_back(tmp_path: Path) -> None:
    for name, limits in NASTIEST:
        report = run(hostile(name), limits=limits)
        assert report.exit_code != ATTACK_SUCCEEDED, f"{name} broke containment"
        assert report.outcome in OUTCOMES
        assert one_sentence(report.detail)

    survivor = run(write_strategy(tmp_path, TRIVIAL), limits=QUICK)

    assert survivor.outcome == "completed"
    assert survivor.exit_code == 0
    assert survivor.signal is None
    assert MARKER in survivor.stdout


# ---------------------------------------------------------------------------
# honest accounting — integers, from the child, in the units they claim
# ---------------------------------------------------------------------------


def test_resource_accounting_is_integers_and_never_floats(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, TRIVIAL), limits=QUICK)

    for value in (report.utime_us, report.stime_us, report.max_rss_bytes):
        # `isinstance(True, int)` is True, and a bool would sail through the
        # type check while being nonsense as a measurement.
        assert isinstance(value, int)
        assert not isinstance(value, bool)
        assert value >= 0

    # A whole CPython interpreter started, so anything under a megabyte means
    # the number was copied out of `ru_maxrss` without converting the kilobytes
    # Linux reports into the bytes this field promises.
    assert report.max_rss_bytes > 1024 * 1024


def test_a_cpu_heavy_run_reports_meaningfully_more_user_time(tmp_path: Path) -> None:
    idle = run(write_strategy(tmp_path / "idle", TRIVIAL), limits=QUICK)
    busy = run(write_strategy(tmp_path / "busy", BUSY), limits=Limits())

    assert busy.outcome == "completed"
    assert busy.utime_us > idle.utime_us
    # The arithmetic loop costs roughly a quarter-second of user time, so a cell
    # reporting the host's usage, a constant, or milliseconds mislabelled as
    # microseconds cannot produce this gap.
    assert busy.utime_us - idle.utime_us >= 100_000


# ---------------------------------------------------------------------------
# failure and output — a strategy that raises, and one that shouts
# ---------------------------------------------------------------------------


def test_a_strategy_that_raises_fails_with_its_traceback(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, RAISES), limits=QUICK)

    assert report.outcome == "failed"
    assert report.exit_code is not None
    assert report.exit_code != 0
    assert report.signal is None
    # Whatever it managed to say before it died is still evidence.
    assert "stdout survived the crash" in report.stdout
    assert "Traceback (most recent call last)" in report.stderr
    assert "ValueError" in report.stderr
    assert "deliberate detonation" in report.stderr
    assert one_sentence(report.detail)


def test_output_larger_than_a_pipe_buffer_is_captured_whole(tmp_path: Path) -> None:
    report = run(write_strategy(tmp_path, NOISY), limits=Limits())

    # A cell that waits on the child before draining its pipes fills the buffer
    # and deadlocks, which surfaces here as `timed_out` rather than a hang.
    assert report.outcome == "completed"
    assert report.exit_code == 0
    assert report.stdout.endswith("STDOUT_END\n")
    assert report.stderr.endswith("STDERR_END\n")
    assert len(report.stdout) > 512 * 1024
    assert len(report.stderr) > 512 * 1024


# ---------------------------------------------------------------------------
# errors — every expected failure is an SbxError the CLI can print
# ---------------------------------------------------------------------------


def test_running_a_path_that_does_not_exist_is_an_sbx_error(tmp_path: Path) -> None:
    with pytest.raises(SbxError) as caught:
        run(tmp_path / "no-such-strategy.py")

    assert str(caught.value).strip() != ""


def test_running_a_directory_is_an_sbx_error(tmp_path: Path) -> None:
    # Copying a directory in would raise IsADirectoryError, which the CLI lets
    # traceback; a mistyped argument has to arrive as an expected failure.
    with pytest.raises(SbxError):
        run(tmp_path)


# ---------------------------------------------------------------------------
# detail — one printable sentence, whatever happened
# ---------------------------------------------------------------------------


def test_detail_is_one_printable_sentence_for_a_clean_and_a_failed_run(
    tmp_path: Path,
) -> None:
    completed = run(write_strategy(tmp_path / "ok", TRIVIAL), limits=QUICK)
    failed = run(write_strategy(tmp_path / "bad", RAISES), limits=QUICK)

    assert completed.outcome == "completed"
    assert failed.outcome == "failed"
    for report in (completed, failed):
        # `detail` goes straight into a one-line summary and into the ledger, so
        # an embedded newline would break the framing of both.
        assert isinstance(report.detail, str)
        assert one_sentence(report.detail)
    # The two outcomes cannot share one sentence and still be saying why.
    assert completed.detail != failed.detail
