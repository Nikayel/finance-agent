"""Write a deterministic journal in the upstream dialect.

The demo has to work on a fresh clone with no market data anywhere, so it
builds its own. Every number here comes from a seeded generator: run this
twice and you get byte-identical files, which is the whole premise the demo
goes on to test.

The dialect is the one `finnce` Phase 1 emits — JSONL, a `type` discriminator,
decimal values as strings whose trailing zeros are significant, and a
timestamp field whose name depends on the record type.

    python3 demo/make_journal.py <path>
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SEED = 20260809
RECORDS = 400
START = datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc)
OPENING_PRICE = Decimal("118200.00")
TICK = Decimal("0.25")


def records() -> list[dict[str, object]]:
    rng = random.Random(SEED)
    price = OPENING_PRICE
    moment = START
    out: list[dict[str, object]] = []

    for sequence in range(1, RECORDS + 1):
        # A drifting random walk on an exact decimal grid: no floats anywhere,
        # so the file says exactly what it means.
        price += TICK * rng.randint(-6, 7)
        moment += timedelta(milliseconds=rng.randint(120, 900))

        out.append(
            {
                "type": "trade",
                "trade_id": f"t{sequence}",
                "product_id": "BTC-USD",
                "price": f"{price:.2f}",
                "size": f"{Decimal(rng.randint(1, 400)) / 100000:.8f}",
                "side": "BUY" if rng.random() < 0.52 else "SELL",
                "executed_at": moment.isoformat(),
                "sequence_num": sequence,
            }
        )

        # A real feed drops out sometimes, and a journal records it.
        if sequence == RECORDS // 2:
            out.append({"type": "disconnect", "reason": "reset by peer"})

    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records():
            handle.write(json.dumps(record) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
