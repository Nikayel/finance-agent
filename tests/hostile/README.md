# Hostile strategy fixtures

These files are **deliberately hostile**. Each one is arbitrary, untrusted "user
strategy" code whose only purpose is to try to break out of `sbx`'s sealed
execution cell — burning CPU, exhausting memory, forking without bound, reaching
the network, escaping the working directory, refusing to die, and so on.

## Rules of engagement

- **Never run these directly.** They are only ever executed *inside* the sealed
  cell (`sbx.cell`), one per subprocess, by the project's own test suite, under
  `setrlimit` caps, a scrubbed environment, no network, and a host-side
  wall-clock watchdog. Running one outside that harness will do exactly what it
  says on the tin (spin a core, fork-bomb your machine, allocate ~10 GB, ...).
- **They are not collected by pytest.** Every filename starts with `strategy_`
  (not `test_`), so pytest never imports or collects them. The real tests load
  them by path and feed them to the cell.
- **stdlib only.** None of them import `sbx`; from the cell's point of view they
  are opaque third-party code.

## Exit-code convention

The distinctive exit code **`97`** means **"the attack SUCCEEDED — containment is
broken."** A fixture reaches its own `sys.exit(97)` only when the sandbox failed
to stop it. When containment works, the fixture is killed by the OS / watchdog
and **never reaches its exit call at all**, or it observes that its attempt was
refused and exits `0`. So the tests assert:

- **`exit 97` is always a failure** of the sandbox.
- Killed-by-signal, `MemoryError`, or `exit 0` (attempt refused) are the
  expected, contained outcomes, depending on the fixture.

## Fixtures

| File | Attacks (one thing each) | Expected containment mechanism |
| --- | --- | --- |
| `strategy_cpu_spin.py` | Tight, un-yielding CPU loop that never terminates | Killed via `RLIMIT_CPU` |
| `strategy_sleep_forever.py` | Blocks ~forever using zero CPU (so `RLIMIT_CPU` can't fire) | Killed by the host wall-clock watchdog |
| `strategy_memory_bomb.py` | Commits ~10 GB (touches every page) | Killed by the host's resident-size watchdog, not `RLIMIT_AS` — macOS refuses to set that limit; `exit 97` if it allocates fine |
| `strategy_fork_bomb.py` | Unbounded `os.fork()` where every child forks again | Contained via `RLIMIT_NPROC` |
| `strategy_socket_connect.py` | Outbound TCP to a public IP and localhost ports | No egress; `exit 97` if any connect succeeds |
| `strategy_dns_lookup.py` | Name resolution via `socket.getaddrinfo` | No DNS; `exit 97` if any name resolves |
| `strategy_read_outside_workdir.py` | Reads `/etc/passwd`, `$HOME`, `~/.sbx`, `/tmp`, and the parent of its own cwd | Reads denied; `exit 97` (prints which) if any read succeeds |
| `strategy_write_outside_workdir.py` | Creates files in `/tmp` and the parent of cwd | Writes denied; `exit 97` if any write succeeds |
| `strategy_ignore_sigterm.py` | Swallows `SIGTERM`/`SIGINT`, then loops forever | `killpg` + `SIGKILL` — the watchdog has no polite phase to swallow |
| `strategy_fd_exhaustion.py` | Opens file descriptors without bound | Contained via `RLIMIT_NOFILE`; `exit 97` if it opens > 100000 |
| `strategy_subprocess_escape.py` | Shells out via `subprocess` to `/bin/sh -c "id"` | No external processes; `exit 97` if the shell runs |
| `strategy_env_snoop.py` | Prints every visible env var name (probe, not attack) | Environment is scrubbed; `exit 0` and the test asserts on the names |
