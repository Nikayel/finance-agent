"""The only object a strategy ever holds.

This module runs *inside* the cell. It is copied into the cell's working
directory along with the encoder and the protocol, so both ends of the wire
share one definition of the format instead of two that drift apart.

The gate is the shape of the API, not a rule the runtime enforces:

    def strategy(market):
        for tick in market.ticks():
            ...
            market.order("BUY", Decimal("0.001"))

Advancing the iterator is what sends the queued decision and asks for the next
tick. There is no way to obtain the next tick without giving up control, and
no method that returns anything the host has not already sent — so a strategy
that walks ``dir()``, ``gc.get_objects()`` or ``__subclasses__()`` finds
nothing, because the future is not in this process yet.

`tick.events` holds this tick's events and no others. A strategy that wants
history keeps its own, in its own memory, which the host never has to trust.
"""

from __future__ import annotations

import os
import random
import sys
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO

from . import protocol

SIDES = ("BUY", "SELL")

STRATEGY_MODULE = "strategy"
STRATEGY_ENTRY = "strategy"


class StrategyError(Exception):
    """The strategy asked for something that is not on offer."""


class Market:
    """A strategy's whole view of the world."""

    def __init__(
        self, to_host: BinaryIO, from_host: BinaryIO, randomness: random.Random
    ) -> None:
        self._to_host = to_host
        self._from_host = from_host
        self._orders: list[dict[str, Any]] = []
        self.now: str = ""
        self.events: tuple[dict[str, Any], ...] = ()
        #: Seeded from the run's seed. The only randomness a strategy should
        #: use, and the only one whose numbers come back the same next year.
        self.random = randomness

    def order(self, side: str, size: Decimal) -> None:
        """Queue a market order against the current tick."""
        if side not in SIDES:
            raise StrategyError(f"side must be BUY or SELL, not {side!r}")
        if isinstance(size, float):
            raise StrategyError(
                "size must be a Decimal: a float has no canonical form and would "
                "make this run unreproducible"
            )
        try:
            quantity = Decimal(size)
        except (InvalidOperation, TypeError, ValueError):
            raise StrategyError(f"size {size!r} is not a decimal quantity") from None
        if not quantity.is_finite() or quantity <= 0:
            raise StrategyError(f"size must be positive and finite, not {size!r}")
        self._orders.append({"side": side, "size": quantity})

    def ticks(self) -> Iterator["Market"]:
        """Yield each simulated instant, one answer per tick."""
        while True:
            message = protocol.read_frame(self._from_host)
            if message is None or message.get("m") == "stop":
                return
            if message.get("m") != "tick":
                raise StrategyError(
                    f"host sent {message.get('m')!r} where a tick was due"
                )

            self.now = message["now"]
            self.events = tuple(message["events"])
            try:
                yield self
            finally:
                # The host is blocked waiting for exactly one answer to this
                # tick. Whatever the strategy did -- returned, broke out of the
                # loop, raised -- it gets one, or both sides wait forever.
                pending, self._orders = self._orders, []
                # The tick being answered is named in the answer, so the host
                # can check the conversation is still in step rather than
                # trusting that it must be.
                protocol.write_frame(
                    self._to_host,
                    {"m": "decision", "now": self.now, "orders": pending},
                )


def _tell_host(to_host: BinaryIO, reason: str) -> None:
    """Best-effort: the host may already be gone, and that is not our problem."""
    try:
        protocol.write_frame(to_host, {"m": "error", "reason": reason})
    except Exception:  # pragma: no cover - the host went away first
        pass


def main() -> None:
    """Bootstrap the cell side: claim the wire, then run the strategy."""
    # fd 1 becomes the private channel to the host, and the strategy's own
    # stdout is pointed at stderr. A stray print can then never desynchronise
    # the frame stream -- it just shows up in the run's diagnostics.
    to_host = os.fdopen(os.dup(1), "wb")
    sys.stdout.flush()
    os.dup2(2, 1)
    from_host = sys.stdin.buffer

    protocol.write_frame(to_host, {"m": "ready"})

    opening = protocol.read_frame(from_host)
    if opening is None or opening.get("m") != "start":
        _tell_host(to_host, f"host opened with {opening!r} where a start was due")
        raise SystemExit(1)
    seed = opening.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        _tell_host(to_host, f"start carried {seed!r}, which is not a seed")
        raise SystemExit(1)

    # Seed the module-level generator too, not just the one handed to the
    # strategy: a careless `import random` in someone's code should still
    # produce the same numbers next year, rather than quietly ruining a run's
    # reproducibility in a way only `sbx verify` would ever notice.
    random.seed(seed)

    try:
        module = __import__(STRATEGY_MODULE)
    except BaseException as error:
        _tell_host(to_host, f"strategy did not import: {type(error).__name__}: {error}")
        raise

    entry = getattr(module, STRATEGY_ENTRY, None)
    if not callable(entry):
        _tell_host(
            to_host,
            f"{STRATEGY_MODULE}.py defines no callable named {STRATEGY_ENTRY!r}",
        )
        raise SystemExit(1)

    market = Market(to_host, from_host, random.Random(seed))
    try:
        entry(market)
    except BaseException as error:
        _tell_host(to_host, f"{type(error).__name__}: {error}")
        raise

    protocol.write_frame(to_host, {"m": "done"})
