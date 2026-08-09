"""strategy_cpu_spin -- CPU exhaustion fixture.

Attempts: Burn CPU in a tight, un-yielding arithmetic loop that never sleeps and never terminates.
Cell must: Kill the process once it exceeds RLIMIT_CPU.
"""

# No imports, no I/O, no sleeping: pure CPU time so RLIMIT_CPU is the only thing
# that can stop it.
x = 1
while True:
    x = x * x + 1
    if x > 10 ** 18:
        x = 1
