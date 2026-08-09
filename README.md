# sbx — a cheat-proof research sandbox

[![ci](https://github.com/Nikayel/finance-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Nikayel/finance-agent/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![dependencies](https://img.shields.io/badge/runtime%20deps-0-lightgrey)](pyproject.toml)

**A harness that runs untrusted — AI- or human-written — strategy code against
journaled market data, where look-ahead bias is impossible by construction and
every run is byte-for-byte replayable.**

## The problem

AI-generated quant research cannot be trusted. Anyone can prompt a model into a
beautiful Sharpe ratio, and almost all such results are garbage for two
specific reasons:

- **Look-ahead bias** — the strategy read data from after the decision point.
  The most common and most expensive failure in quant research.
- **Irreproducibility** — the result cannot be re-derived from its inputs, so
  nobody can audit it.

Neither is fixable with better prompts. Both are infrastructure problems.

The usual answer is to *detect* look-ahead after the fact — audit the code,
diff the timestamps, hope. sbx takes the other route: the strategy is never
given a way to express it. It runs in a subprocess with no data in its memory
except what the host has already decided it is allowed to see, and the host
only advances the clock once the strategy has committed to a decision.

## How it works

```
          ┌──────────────────────── host process (sbx CLI) ───────────────┐
          │                                                               │
 sealed   │  ledger ──── run tuple (data_hash, code_hash, seed)           │
 dataset ─┼──► feeder ═══ time-gated protocol ═══► ┌─ execution cell ─┐   │
(immutable│      │        (pipe, one msg/tick)     │  strategy.py     │   │
 snapshot)│      └── advances sim-clock only when  │  no net, no fs,  │   │
          │          the cell yields a decision    │  rlimits, killed │   │
          │                                        │  on violation    │   │
          │  results ◄── fills, PnL, resource acct └──────────────────┘   │
          └───────────────────────────────────────────────────────────────┘
```

Three components, and a hard fence around them.

**1. Time-gated data API.** Strategy code never touches files. It receives one
object that serves data only up to the simulated *now*. There is no API whose
answer depends on data after `T`, and the future is not merely hidden — it has
not been written into the cell's process memory yet. A strategy that walks
`dir()`, `gc.get_objects()` or `__subclasses__()` finds nothing, because there
is nothing to find.

**2. Sealed execution cell.** One subprocess per run: an empty private working
directory, `setrlimit` caps, no network, and a host-side watchdog that
escalates to `SIGKILL`. Isolation is enforced at the OS boundary, never by
in-process tricks like stripping builtins — those are escapable by
construction and are treated as defence in depth at most. What is actually
enforced is [spelled out below](#containment-precisely) rather than asserted.

**3. Reproducibility ledger.** Every run is recorded as
`(data_hash, code_hash, seed, sbx_version, env_fingerprint) → result`.
`sbx verify` re-executes the tuple and byte-diffs the canonical encoding,
reporting `REPRODUCED` or `DIVERGED` with the first divergent record.

## The CLI

Four verbs. A fifth verb means the scope has bled, and there is a test that
fails if one appears.

| Verb | Contract |
|---|---|
| `sbx seal <journal>` | Snapshot a dataset, content-hash it, register it immutably. |
| `sbx run <strategy.py> --data <hash> --seed <n>` | Execute the strategy in the cell against the time-gated API; record the run tuple and result. |
| `sbx verify <run-id>` | Re-execute the tuple; diff fills and PnL byte-for-byte; report `REPRODUCED` or `DIVERGED`. |
| `sbx ls` | List sealed datasets and recorded runs. |

## Install

Python 3.11+. The runtime core is **stdlib-only** — the only dependency in the
project is `pytest`, and it lives in the dev extra.

```console
$ git clone https://github.com/Nikayel/finance-agent.git
$ cd finance-agent
$ python3 -m venv .venv && source .venv/bin/activate
$ pip install -e ".[dev]"
$ sbx --version
sbx 0.1.0
```

Verify a clean checkout end to end — throwaway venv, install, verb check,
full test suite:

```console
$ scripts/smoke.sh
```

## What works today

Sealing and tamper detection, verbatim from a terminal:

```console
$ sbx seal ~/finnce/phase1/data/events.jsonl
sealed ae76ece753ef6a6238ea16d216155bbb827813e361ba0e7091e5b615dcd4be95
  280 records, 39.7 KiB
  /Users/you/.sbx/datasets/ae76ece753ef6a6238ea16d216155bbb827813e361ba0e7091e5b615dcd4be95

$ sbx ls
DATASETS
  ae76ece753ef      280 records   39.7 KiB  ok

RUNS
  (none)

$ # flip one byte in the middle of the sealed copy, same file length
$ sbx ls
DATASETS
  ae76ece753ef      280 records   39.7 KiB  TAMPERED
```

`ls` re-hashes the stored bytes every time rather than trusting the manifest.
A store that only reports what it was told is not a store you can audit.

## Status

Built milestone by milestone, each one gated on a human review. See
[`CHANGELOG.md`](CHANGELOG.md) for what each milestone delivered and what it
deliberately did not.

| # | Milestone | State |
|---|---|---|
| 1 | Package + CLI shell | ✅ done |
| 2 | Sealed datasets + ledger store | ✅ done |
| 3 | Execution cell (isolation, rlimits, watchdog) | ✅ done |
| 4 | Time-gated protocol + `Market` client | 🚧 in progress |
| 5 | Adversarial suite — *human-written attacks* | ⬜ |
| 6 | Determinism hunt + `sbx verify` | ⬜ |
| 7 | The one-command demo | ⬜ |

## How this repo is built, and why that is the point

This project consumes the journals produced by
[`finnce`](https://github.com/Nikayel), a market-data ingestor built by hand.
The division of labour is deliberate: **finnce is hand-written by me**, and
**sbx is implemented by AI agents** against a written build plan, with a human
review gate after every milestone.

The exception is milestone 5. The adversarial suite — the strategies that try
to seek ahead, hold references across ticks, replay the pipe, monkeypatch the
client, import host modules, read `/proc`, or probe the resource limits — is
written by hand, by me, and no agent is allowed to write it. A gate whose
attacks were authored by the same process that built the gate proves nothing.

That is the thesis of the product applied to its own construction: **AI-written
code is worth exactly as much as the adversarial verification you put it
through.**

## Containment, precisely

Every run records the containment that was **actually in force**, and the cell
reports it back rather than implying it. A guarantee this table does not claim
is a guarantee the code does not make.

| What | macOS | Linux | How |
|---|---|---|---|
| No outbound TCP, no DNS | ✅ | ❌ *not in v1* | `sandbox-exec` Seatbelt profile, `(deny network*)` |
| No reads of user-owned files (`$HOME`, `/etc`, other temp dirs, **`~/.sbx`**) | ✅ | ❌ *not in v1* | same profile |
| No writes outside the private working directory | ✅ | ❌ *not in v1* | same profile |
| CPU seconds | ✅ | ✅ | `RLIMIT_CPU`, killed with `SIGXCPU` |
| Open file descriptors | ✅ | ✅ | `RLIMIT_NOFILE` |
| Process creation (no forking, no shelling out) | ✅ | ✅ | `RLIMIT_NPROC` |
| File size | ✅ | ✅ | `RLIMIT_FSIZE` |
| Resident memory | ✅ | ✅ | host watchdog polls RSS and kills |
| Wall clock, even against a `SIGTERM`-swallowing process | ✅ | ✅ | host watchdog, `killpg` + `SIGKILL` |
| Cannot import the host package | ✅ | ✅ | base interpreter run with `-S`, so no site-packages |

Three things in that table are deliberate and worth defending:

**Linux has no network or filesystem confinement in v1.** There is no
equivalent of Seatbelt that works unprivileged: Ubuntu 24.04 — what GitHub's
`ubuntu-latest` runs — ships
`kernel.apparmor_restrict_unprivileged_userns=1`, so `unshare --net` fails
without root. Rather than ship something that looks like a sandbox and is not,
the cell omits `network` and `filesystem` from its reported containment there,
and the tests that depend on those guarantees **skip with a stated reason**
instead of passing quietly.

**Memory is enforced by the host, not by `setrlimit`.** macOS refuses
`RLIMIT_AS` and `RLIMIT_DATA` outright — `setrlimit` returns an error rather
than a smaller limit. Capping address space would therefore mean one mechanism
and one reported outcome on Linux and different ones on macOS. One watchdog for
everyone costs a poll interval of latency, and the cap can be overshot in that
window; what it buys is a single behaviour to reason about.

**`RLIMIT_NPROC` is per-user, so the cap is 1.** The account running sbx
already has hundreds of processes, so any small cap means the cell cannot fork
at all — which is exactly what a strategy sandbox wants. At a cap of one,
`fork` fails on every machine rather than only on busy ones.

`sandbox-exec` has carried a deprecation warning since 2017 and Apple has named
no removal date and no replacement. That is a real dependency risk, recorded
here rather than discovered later.

## Testing stance

- **No mocks for the OS.** Isolation, resource limits and subprocess behaviour
  are tested against the real operating system. A test that mocks `setrlimit`
  tests nothing about containment.
- **Hostile inputs are fixtures.** `tests/hostile/` holds strategies that spin
  the CPU forever, allocate 10 GB, fork-bomb, open sockets, read outside their
  working directory, and swallow `SIGTERM`. Each must be contained and
  reported, never crash the host.
- **Determinism is asserted, not assumed.** Canonical encoding is tested across
  processes with different `PYTHONHASHSEED` values, because that is the bug
  that hides.

## Non-goals, permanently

Dashboards, strategy libraries, ML, broker or exchange connectivity, live
trading, multi-user services, web anything. Distributed execution is a
*possible* later phase, not this product.

## License

[MIT](LICENSE).
