"""strategy_fd_exhaustion -- file-descriptor exhaustion fixture.

Attempts: Open file descriptors in an unbounded loop until the per-process table is exhausted.
Cell must: Contain descriptor exhaustion via RLIMIT_NOFILE.
"""
import os
import sys

# Anchor fd that needs no filesystem access; dup it to consume the fd table.
anchor, _other = os.pipe()
held = [anchor, _other]
while True:
    try:
        held.append(os.dup(anchor))
    except OSError:
        sys.exit(0)  # RLIMIT_NOFILE reached: contained
    if len(held) > 100000:
        print("OPENED %d fds" % len(held), flush=True)
        sys.exit(97)  # implausible fd count -> containment broken
