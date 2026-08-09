"""strategy_fork_bomb -- process-table exhaustion fixture.

Attempts: Spawn child processes in an unbounded os.fork() loop where every child forks again.
Cell must: Contain the process explosion via RLIMIT_NPROC.
"""
import os
import time

root = os.getpid()
while True:
    # Self-limiting: if we were orphaned (parent gone) we exit, so this tree can
    # never outlive the original process.
    if os.getpid() != root and os.getppid() == 1:
        os._exit(0)
    try:
        os.fork()
    except OSError:
        # RLIMIT_NPROC reached: exit at once rather than spin retrying on EAGAIN.
        os._exit(0)
    time.sleep(0.02)  # let the tree persist and grow instead of churning
