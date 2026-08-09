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


def parse_order(raw: Any, index: int) -> Order:
    """Turn one wire order into an Order, or say precisely why it is not one."""
    where = f"order {index}"
    if not isinstance(raw, Mapping):
        raise SbxError(f"{where}: expected an object, got {type(raw).__name__}")

    side = raw.get("side")
    if side not in SIDES:
        raise SbxError(f"{where}: side must be BUY or SELL, not {side!r}")

    size = raw.get("size")
    try:
        quantity = Decimal(str(size))
    except (InvalidOperation, ValueError):
        raise SbxError(f"{where}: size {size!r} is not a decimal quantity") from None
    if not quantity.is_finite() or quantity <= 0:
        raise SbxError(f"{where}: size must be positive and finite, not {size!r}")

    return Order(side=side, size=quantity)


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

    def observe(self, event: Mapping[str, Any]) -> None:
        """Show the portfolio one revealed event, filling anything waiting."""
        if event.get("type") != "trade":
            return
        try:
            price = Decimal(str(event["price"]))
        except (InvalidOperation, KeyError, ValueError) as error:
            raise SbxError(f"trade has no usable price: {error}") from error
        at = str(event.get("executed_at", ""))

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
