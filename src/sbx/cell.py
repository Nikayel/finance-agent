"""Component 2 — the sealed execution cell.

One subprocess per run: an empty private working directory, `setrlimit` caps,
no network, a host-side wall-clock watchdog, and honest resource accounting
reported back to the host. Milestone 3.
"""
