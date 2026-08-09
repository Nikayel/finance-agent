"""strategy_write_outside_workdir -- filesystem write-escape fixture.

Attempts: Create files outside the working directory (in /tmp and in the parent of cwd).
Cell must: Deny every write outside the sealed working directory.
"""
import os
import sys

TARGETS = ["/tmp/sbx_escape_probe.txt",
           os.path.join(os.path.dirname(os.getcwd()), "sbx_escape_probe.txt")]
breached = None
for path in TARGETS:
    try:
        with open(path, "w") as fh:
            fh.write("escape")
    except OSError:
        continue  # blocked as expected
    breached = path
    try:  # best-effort cleanup; the breach is already proven
        os.remove(path)
    except OSError:
        pass
    break

if breached:
    print("WROTE outside workdir: %s" % breached, flush=True)
    sys.exit(97)  # write outside workdir succeeded -> containment broken

sys.exit(0)  # every write was denied
