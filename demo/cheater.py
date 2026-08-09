"""The same idea, with a number that came from nowhere.

This is the ordinary failure, not the exotic one. Nothing here is malicious and
nothing here is hidden: the position size is scaled by the wall clock, so the
run produces a result, reports a P&L, and looks exactly like work. Run it again
and the number is different.

That is the whole problem with an agent-written backtest. It is not usually a
lie; it is usually an unrepeatable accident presented as a measurement. sbx
does not prevent a strategy reaching for the clock — nothing running real code
can. What it does is make the claim checkable: `sbx verify` re-executes the
recorded tuple and the result does not come back.
"""

import time
from decimal import Decimal

BASE = Decimal("0.001")
RUN_LENGTH = 2


def strategy(market):
    previous = None
    rising = 0

    for tick in market.ticks():
        for event in tick.events:
            if event.get("type") != "trade":
                continue

            price = Decimal(event["price"])
            if previous is not None and price > previous:
                rising += 1
            else:
                rising = 0
            previous = price

            if rising >= RUN_LENGTH:
                # The sin, in one line: a size nobody can reproduce.
                #
                # The modulus is a microsecond window rather than a nanosecond
                # one because macOS's clock only has microsecond granularity —
                # `time_ns() % 1000` is always zero there, which would make
                # this accidentally reproducible and prove nothing.
                jitter = Decimal(time.time_ns() % 1_000_000) / Decimal(1_000_000)
                market.order("BUY", BASE + BASE * jitter)
                rising = 0
