"""strategy_ignore_sigterm -- termination-refusal fixture.

Attempts: Install handlers that swallow SIGTERM and SIGINT, then loop forever ignoring graceful termination.
Cell must: Escalate to SIGKILL when polite termination is ignored.
"""
import signal
import sys
import time


def swallow(signum, frame):
    return  # deliberately do nothing: refuse to die


signal.signal(signal.SIGTERM, swallow)
signal.signal(signal.SIGINT, swallow)

# Confirm the handlers are armed BEFORE we start ignoring signals.
sys.stdout.write("HANDLERS_INSTALLED sigterm+sigint swallowed\n")
sys.stdout.flush()

while True:
    time.sleep(1)
