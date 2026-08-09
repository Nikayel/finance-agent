"""A strategy that goes looking for the future, and reports what it finds.

This is the attack the architecture is built against. It tries the obvious
routes to the data it has not been shown yet: the sealed dataset in the sbx
store, the home directory, the parent of its own working directory, and the
network. Then it prints what happened.

The point is not that these are blocked by a rule. The point is that even if a
route opened, the next record has not been written to the pipe yet — it is not
in this process's memory, so there is nothing here to find. The filesystem
checks are belt and braces on top of that.
"""

import os
import socket
import sys
from decimal import Decimal


def _probe(label, attempt):
    try:
        attempt()
    except Exception as error:
        print(f"  refused  {label}: {type(error).__name__}", file=sys.stderr)
        return False
    print(f"  GOT IN   {label}", file=sys.stderr)
    return True


def _hunt_for_the_future():
    print("looking for data I have not been shown:", file=sys.stderr)
    home = os.path.expanduser("~")
    breaches = [
        _probe("the sealed dataset store", lambda: os.listdir(f"{home}/.sbx/datasets")),
        _probe("my home directory", lambda: os.listdir(home)),
        _probe("the parent of my working directory", lambda: os.listdir("..")),
        _probe("/etc/passwd", lambda: open("/etc/passwd", "rb").read(1)),
        _probe("the network", lambda: socket.create_connection(("1.1.1.1", 53), 2)),
    ]
    if any(breaches):
        print("  -- something opened; see above", file=sys.stderr)
    else:
        print("  -- every route refused", file=sys.stderr)


def strategy(market):
    _hunt_for_the_future()

    bought = False
    for tick in market.ticks():
        # Whatever the probes found, this is still all there is: one instant,
        # and the events that belong to it.
        for event in tick.events:
            if event.get("type") == "trade" and not bought:
                market.order("BUY", Decimal("0.001"))
                bought = True
