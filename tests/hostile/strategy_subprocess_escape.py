"""strategy_subprocess_escape -- shell-escape fixture.

Attempts: Shell out via subprocess to run an external command (/bin/sh -c "id") the cell should forbid.
Cell must: Prevent spawning external processes / shell escapes.
"""
import subprocess
import sys

try:
    result = subprocess.run(
        ["/bin/sh", "-c", "id"],
        capture_output=True,
        timeout=5,
    )
except (OSError, subprocess.SubprocessError):
    sys.exit(0)  # could not spawn a shell: contained

if result.returncode == 0 and result.stdout.strip():
    print("SHELL RAN: %s" % result.stdout.decode(errors="replace").strip(), flush=True)
    sys.exit(97)  # external command executed -> containment broken

sys.exit(0)  # shell spawned but produced nothing usable
