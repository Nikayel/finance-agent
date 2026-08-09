"""The honest version of the idea: buy after two consecutive rising trades.

Every decision is made from what has already arrived. Note what it costs the
author: `tick.events` holds this tick's events and nothing else, so the running
state is kept here, in the strategy's own memory. State the host does not hold
is state the host does not have to be trusted about.
"""

from decimal import Decimal

SIZE = Decimal("0.001")
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
                market.order("BUY", SIZE)
                rising = 0
