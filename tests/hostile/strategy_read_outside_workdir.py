"""strategy_read_outside_workdir -- filesystem read-escape fixture.

Attempts: Read paths outside the working directory (/etc/passwd, the filesystem root, the user's home, and the parent of its own cwd).
Cell must: Deny every read outside the sealed working directory.
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
# parent of cwd is probed alongside the usual absolute suspects. Every path is
# derived at runtime: nothing here may depend on the machine it was written on.
candidates = ["/", os.path.expanduser("~"), os.path.dirname(os.getcwd()), ".."]
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
