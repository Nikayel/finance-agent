"""sbx — a cheat-proof research sandbox.

Strategies run against journaled market data behind a time gate that makes
look-ahead unrepresentable, inside a sealed subprocess, with every run
recorded as a re-runnable ``(data, code, seed) -> result`` tuple.
"""

__version__ = "0.1.0"
