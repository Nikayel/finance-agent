"""The upstream journal dialect, as reusable fixtures.

sbx consumes journals produced by `finnce`, a separately hand-built market-data
ingestor. This module is the single place that dialect is written down, so a
test never hand-rolls a record and quietly drifts from the real format.

The dialect, in four facts:

* JSONL — one complete JSON object per line, UTF-8, `\\n`-terminated, appended
  in arrival order and never rewritten.
* Every record carries a `type` discriminator: ``trade``, ``book_update``,
  ``gap``, ``disconnect``, ``error``.
* Prices and sizes are **decimal strings**, not numbers, and their trailing
  zeros are significant — exchange precision is data.
* The timestamp field's *name* differs per record type, and two of the types
  have no timestamp at all. There is no single canonical time field.

Deliberately written with `json.dumps` defaults (spaces after separators), not
sbx's canonical encoding: the upstream file is not canonical, and pretending
otherwise would hide the very normalisation sbx has to perform.
"""

from __future__ import annotations

import json
from pathlib import Path

# One representative record of each type, exactly as finnce emits them.
TRADE = {
    "type": "trade",
    "trade_id": "823401",
    "product_id": "BTC-USD",
    "price": "118200.75",
    "size": "0.00420000",
    "side": "BUY",
    "executed_at": "2026-07-29T14:30:01.250000+00:00",
    "sequence_num": 2,
}

BOOK_UPDATE = {
    "type": "book_update",
    "product_id": "BTC-USD",
    "side": "BID",
    "price": "118200.50",
    "quantity": "0.75000000",
    "event_time": "2026-07-29T14:30:00.123456+00:00",
    "is_snapshot": True,
    "sequence_num": 1,
}

GAP = {
    "type": "gap",
    "expected_sequence": 2,
    "received_sequence": 4,
    "missed_count": 2,
}

DISCONNECT = {"type": "disconnect", "reason": "reset by peer"}

ERROR = {
    "type": "error",
    "received_at": "2026-07-29T14:30:05.000000+00:00",
    "reason": "not JSON",
    "frame": "garbage",
}

# Which field carries each type's timestamp; `gap` and `disconnect` have none.
TIMESTAMP_FIELDS = {
    "trade": "executed_at",
    "book_update": "event_time",
    "error": "received_at",
}


def trade(*, price: str, size: str = "0.00100000", at: str, sequence: int) -> dict:
    """A trade record with the fields a test is likely to vary."""
    return {
        **TRADE,
        "trade_id": f"t{sequence}",
        "price": price,
        "size": size,
        "executed_at": at,
        "sequence_num": sequence,
    }


def sample_records() -> list[dict]:
    """A short journal covering every record type, in a plausible order."""
    return [
        BOOK_UPDATE,
        trade(price="118200.75", at="2026-07-29T14:30:01.250000+00:00", sequence=2),
        trade(price="118201.00", at="2026-07-29T14:30:02.500000+00:00", sequence=3),
        GAP,
        trade(price="118199.25", at="2026-07-29T14:30:04.000000+00:00", sequence=6),
        ERROR,
        DISCONNECT,
    ]


def write_journal(path: Path, records: list[dict] | None = None) -> Path:
    """Write records to `path` in the upstream dialect and return the path."""
    if records is None:
        records = sample_records()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path
