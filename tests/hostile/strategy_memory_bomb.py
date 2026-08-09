"""strategy_memory_bomb -- address-space exhaustion fixture.

Attempts: Allocate roughly 10 GB and touch every page so the memory is actually committed, not lazily mapped.
Cell must: Kill the process (or fail its allocation) once it exceeds RLIMIT_AS.
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
