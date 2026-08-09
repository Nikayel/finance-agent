"""strategy_env_snoop -- environment-scrubbing probe.

Attempts: Enumerate and print every environment variable name visible to the process (a probe, not an attack).
Cell must: Hand the process a scrubbed environment so no host secrets are visible.
"""
import os
import sys

names = sorted(os.environ.keys())
for name in names:
    print(name)
print("ENV_VAR_COUNT=%d" % len(names), flush=True)
sys.exit(0)
