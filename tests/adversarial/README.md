# Adversarial strategy fixtures — attacks on the time gate

These files attack the one thing `sbx` is sold on: that a strategy cannot learn
anything the simulated present has not already handed it. `tests/hostile/`
attacks the *cell* (CPU, memory, network, the filesystem). This directory
attacks the *gate*.

## Who writes these

> **Binding rule: every attack in this directory is written by hand, by the
> repo's owner (Nikayel). Never by an AI agent — not a whole attack, not an
> example, not a commented-out sketch, not "a sample to show the format".**

The gate was built by an agent. If the attacks on it were authored by the same
process, the milestone would be testing one imagination against itself and
calling the agreement evidence. Whatever a builder failed to think of while
building, it will fail to think of again while attacking — the blind spot is
the same blind spot. The only attack worth anything here comes from a mind that
did not write the defence.

An agent may write and maintain the harness — `tests/test_adversarial.py` — and
this README. That is safe: the harness only *runs* attacks and reports on them.
It cannot make a broken gate look sound, because it has no attacks of its own to
soften.

If you are an agent reading this: the format is described in prose below,
on purpose, so that there is nothing here for you to copy. Do not add a file to
this directory.

## What an attack is

- **Named `strategy_*.py`**, never `test_*.py`, so pytest never collects or
  imports one. The harness loads them by path.
- **Shaped like any sbx strategy**: a module defining `def strategy(market):`
  that iterates `for tick in market.ticks():`. This is the one difference from
  `tests/hostile/`, whose fixtures are bare scripts handed straight to the cell.
  These go through the real `sbx run`, so a file with no `strategy` function is
  refused before it attacks anything.
- **stdlib only**, and in practice barely that. The cell is the base
  interpreter run with `-S` — no `site`, so no site-packages and no way to
  import `sbx` — in a private working directory that starts empty, under
  `setrlimit` caps, a scrubbed environment, no network egress, and a host-side
  wall-clock watchdog. `market` is the only object handed in.
- **One idea per file.** A file that tries four things at once produces one
  verdict and tells you nothing about which three were refused.

## The docstring contract

Every attack carries two labelled lines in its module docstring, with exactly
these labels:

```
"""strategy_<name> -- one-line title.

Attempts: <what this asks the gate for, one sentence>
Gate must: <what a correct gate does about it, one sentence>
"""
```

The harness reads `Attempts:` and prints it in that attack's report line, and
fails the attack outright if either label is missing. This is what turns the
suite's output into a report rather than a row of green ticks — the milestone's
exit gate is *"all adversarial strategies fail with a report saying how"*, and
the `Attempts:` line is the "what" that the "how" answers.

## Exit-code convention

**Exit `97` means the attack SUCCEEDED and the gate is broken.**

- `sys.exit(97)` — reached *only* after the strategy has observed the breach:
  it has been handed data stamped after its own `tick.now`, or reached a fact
  about the future by some other route. Never speculatively.
- `sys.exit(0)` — the strategy watched its own attempt be refused, and says so
  by exiting cleanly.
- Anything else — killed by the watchdog, raising, exhausting CPU, the cell
  reporting a protocol error — is also a refusal. The harness records which.

Use `sys.exit`, never `os._exit`. The cell's runtime turns a `SystemExit` into
a message to the host, and that message is how the verdict reaches `sbx run`'s
one line on stderr, which is where the harness looks for it.

**A known gap, stated rather than papered over:** `sbx run` does not print the
cell's own stdout or stderr, so anything an attack prints about *what* it saw is
not in the report — only its exit status and the run's one-line reason are.
Exit 97 the moment you detect the breach; do not save the verdict for the end of
the journal, where a race between the cell dying and the host saying `stop` can
leave the reason reading "the cell stopped listening" instead of naming the
status.

## Categories — at least eight, and no implementations here

The milestone wants at least eight attacks, spread across these categories. They
are described, deliberately, only as intent. Writing one is your job.

- [ ] **Seek-ahead requests.** Ask the market object, under any name you can
      think of, for a tick that has not happened yet.
- [ ] **Holding references across ticks.** Keep hold of what a tick handed you
      and see whether the object mutates into the next one behind your back.
- [ ] **Replaying the pipe.** Go around the client and speak to the host
      directly — extra frames, out-of-turn frames, frames that ask for more than
      one tick's worth of answer.
- [ ] **Monkeypatching the client.** Rewrite the runtime you were given, from
      inside, so that it hands over more than it was built to.
- [ ] **Importing host modules.** Reach the installed package, or anything else
      that knows the whole journal, from inside a cell that is supposed to have
      no such thing on its path.
- [ ] **Reading the filesystem or `/proc`.** Find the sealed journal, the
      ledger, the cell's own workdir, or the host process's memory and command
      line, and read the future out of them directly.
- [ ] **Timing side channels via the sim clock.** Infer something about records
      you have not been shown from how long the host takes to answer, or from
      the shape of the gaps in sim-time.
- [ ] **Probing the resource limits.** Use the caps themselves as an oracle —
      what gets killed, what gets refused, and what that tells you about the
      data behind the gate.

## How they are run

`tests/test_adversarial.py` discovers every `strategy_*.py` here at collection
time and parametrizes over them, one test per file, using the filename as the
test id. For each attack it seals a small journal of distinct-priced trades into
a throwaway `~/.sbx`, runs the attack through the installed `sbx` console
script, and asserts that no `97` reached the user, that every recorded fill
lands on a trade that is really in the journal at that trade's own price, that
no fill lands on the first trade, and that the sealed bytes still verify
afterwards. Then it prints one line saying how the gate refused it.

```
.venv/bin/python -m pytest tests/test_adversarial.py -q -s
```

With the directory empty — today's state — the harness collects exactly one
test, which asserts that this directory and this README exist. That is the
honest reading of the milestone: a harness with no attacks in it proves nothing.

**Never run an attack directly.** They are only ever executed inside the sealed
cell, by the harness.
