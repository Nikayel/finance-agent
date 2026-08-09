"""Fills and profit, in `Decimal` from end to end.

**An order fills at the price of the next trade after the tick it was placed
in.** Not at a price from that tick — the strategy has already seen that one,
and letting it trade there is look-ahead wearing a different hat. Not at some
averaged or idealised price either: the next trade actually happened, at a
price the strategy could not have known.

An order with no subsequent trade simply never fills. That is the honest
outcome for a decision made at the end of the data, and it is one of the
places a backtest usually lies.

Orders arriving from the cell are untrusted. Everything here re-validates
rather than believing the wire.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import SbxError

SIDES = ("BUY", "SELL")

# A size must be a plausible quantity, not merely a finite Decimal.
# `Decimal("1E-20000000")` is finite and positive and arrives in 22 bytes; its
# canonical text is twenty million characters, materialised inside the *host*,
# which unlike the cell has no resource limits. Measured: 1 GB of ASCII from
# one order. The bound is what stands between a 22-byte frame and the machine.
MAX_DECIMAL_PLACES = 18
MAX_MAGNITUDE_DIGITS = 18


@dataclass(frozen=True)
class Order:
    side: str
    size: Decimal


@dataclass(frozen=True)
class Fill:
    at: str
    side: str
    size: Decimal
    price: Decimal

    def as_record(self) -> dict[str, Any]:
        """The canonical form written to the ledger."""
        return {
            "at": self.at,
            "side": self.side,
            "size": self.size,
            "price": self.price,
        }


def plausible_decimal(value: Any, what: str) -> Decimal:
    """Parse an untrusted decimal, or say precisely why it is not one.

    Used for order sizes *and* for prices read out of a sealed journal. Both
    are untrusted: a journal is content-addressed, which proves nobody changed
    it, not that it was sensible to begin with. A price of `"NaN"` used to
    parse cleanly here and only surface much later as an encoding failure at
    hash time, blaming the wrong thing entirely.
    """
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SbxError(f"{what} {value!r} is not a decimal quantity") from None
    if not quantity.is_finite() or quantity <= 0:
        raise SbxError(f"{what} must be positive and finite, not {value!r}")
    if quantity.as_tuple().exponent < -MAX_DECIMAL_PLACES:
        raise SbxError(f"{what} has more than {MAX_DECIMAL_PLACES} decimal places")
    if quantity.adjusted() > MAX_MAGNITUDE_DIGITS:
        raise SbxError(f"{what} is larger than {MAX_MAGNITUDE_DIGITS} digits")
    return quantity


def parse_order(raw: Any, index: int) -> Order:
    """Turn one wire order into an Order, or say precisely why it is not one."""
    where = f"order {index}"
    if not isinstance(raw, Mapping):
        raise SbxError(f"{where}: expected an object, got {type(raw).__name__}")

    side = raw.get("side")
    if side not in SIDES:
        raise SbxError(f"{where}: side must be BUY or SELL, not {side!r}")

    return Order(side=side, size=plausible_decimal(raw.get("size"), f"{where}: size"))


@dataclass
class Portfolio:
    """Position, cash and fills for one run."""

    position: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    last_price: Decimal | None = None
    fills: list[Fill] = field(default_factory=list)
    _pending: list[Order] = field(default_factory=list)

    def submit(self, order: Order) -> None:
        """Queue an order against the next trade."""
        self._pending.append(order)

    def observe(self, event: Mapping[str, Any], now: str) -> None:
        """Show the portfolio one revealed event, filling anything waiting.

        A fill is stamped with the *simulated* time — `now` — and not with the
        exchange's own `executed_at`. Those differ: exchange clocks are not
        monotonic, so a late-arriving trade carries a stamp earlier than the
        tick that revealed it. Writing that into the ledger would produce fills
        dated before the decisions that caused them, which is indistinguishable
        from real look-ahead in the one artefact meant to disprove it.
        """
        if event.get("type") != "trade":
            return
        price = plausible_decimal(event.get("price"), "trade price")
        at = now

        self.last_price = price
        if not self._pending:
            return
        for order in self._pending:
            self._fill(order, at, price)
        self._pending.clear()

    def _fill(self, order: Order, at: str, price: Decimal) -> None:
        signed = order.size if order.side == "BUY" else -order.size
        self.position += signed
        self.cash -= signed * price
        self.fills.append(
            Fill(at=at, side=order.side, size=order.size, price=price)
        )

    @property
    def pnl(self) -> Decimal:
        """Cash plus the position marked at the last trade anyone saw."""
        if self.last_price is None:
            return self.cash
        return self.cash + self.position * self.last_price
