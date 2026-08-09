"""strategy_sleep_forever -- idle-blocking fixture.

Attempts: Block essentially forever while consuming no CPU, so RLIMIT_CPU can never fire.
Cell must: Kill the process via the host-side wall-clock watchdog.
"""
import time

# Consumes no CPU time at all: only wall-clock elapses, which RLIMIT_CPU cannot
# see. The host watchdog is the only thing that can end this.
while True:
    time.sleep(3600)
