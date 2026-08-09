"""strategy_read_outside_workdir -- filesystem read-escape fixture.

Attempts: Read what belongs to the user -- /etc/passwd, the home directory, the sbx store, other temp directories, and the parent of its own cwd.
Cell must: Deny every read of user-owned data.

The contract is "an empty working directory plus the read-only stdlib", so the
system's own library trees (/usr, /System) are readable *by design* and are not
probed here. What must never be readable is anything the user owns -- above all
~/.sbx, where the sealed datasets live: a strategy that could open the journal
could read its own future, which is the one thing this whole project exists to
make impossible.
"""
import os
import sys

breached = None

try:
    with open("/etc/passwd", "rb") as fh:
        if fh.read(1):
            breached = "/etc/passwd"
except OSError:
    pass

# Climbing out of the private workdir is the escape that matters most, so the
# parent of cwd is probed alongside the usual suspects. Every path is derived
# at runtime: nothing here may depend on the machine it was written on.
candidates = [
    os.path.expanduser("~"),
    os.path.expanduser("~/.sbx"),
    os.path.dirname(os.getcwd()),
    "..",
    "/private/tmp",
    "/tmp",
]
for path in candidates:
    if breached:
        break
    try:
        if os.listdir(path):
            breached = path
    except OSError:
        pass

if breached:
    print("READ succeeded: %s" % breached, flush=True)
    sys.exit(97)  # read outside workdir succeeded -> containment broken

sys.exit(0)  # every read was denied
