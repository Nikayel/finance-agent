"""strategy_memory_bomb -- memory exhaustion fixture.

Attempts: Allocate roughly 10 GB and touch every page so the memory is actually committed, not lazily mapped.
Cell must: Kill the process once its resident size passes the run's memory limit.

Not by RLIMIT_AS. macOS refuses to set that limit at all -- setrlimit returns
an error rather than a smaller cap -- so memory is policed by the host, which
polls resident size and kills. One mechanism on every platform beats two
behaviours to explain, at the cost of a poll interval of overshoot.
"""
import sys

TEN_GB = 10 * 1024 * 1024 * 1024
CHUNK = 64 * 1024 * 1024  # 64 MB
PAGE = 4096

blocks = []
committed = 0
while committed < TEN_GB:
    chunk = bytearray(CHUNK)
    for i in range(0, CHUNK, PAGE):  # dirty every page so it is truly resident
        chunk[i] = 1
    blocks.append(chunk)
    committed += CHUNK

# Reaching here means ~10 GB was committed without being stopped: containment
# is broken. (RLIMIT_AS working instead raises MemoryError above -> not 97.)
print("ALLOCATED %d bytes" % committed, flush=True)
sys.exit(97)
