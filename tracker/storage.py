"""Price history persistence.

History lives in docs/data.json so GitHub Pages serves it directly to the
dashboard -- one file is both the database and the API. Committing it each run
also means git gives you a full audit trail of price movement for free.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# ~1 year of 2-hourly samples. Keeps the file small enough for phones to load.
MAX_HISTORY_POINTS = 4000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "updated_at": None, "products": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt file must not wedge the scheduled job forever.
        return {"schema": SCHEMA_VERSION, "updated_at": None, "products": {}}
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("products", {})
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def previous_low(data: dict[str, Any], product_id: str) -> float | None:
    """Lowest price ever recorded for this product, for new-low detection."""
    entry = data["products"].get(product_id) or {}
    history = entry.get("history") or []
    prices = [p["price"] for p in history if isinstance(p.get("price"), (int, float))]
    return min(prices) if prices else None


def record(
    data: dict[str, Any],
    *,
    product_id: str,
    name: str,
    price: float | None,
    listing: dict[str, Any] | None,
    considered: int,
    relevant: int,
    verified: int,
    kept: int,
    rejected: list[dict[str, Any]],
    min_trust_score: int,
    error: str | None = None,
) -> None:
    entry = data["products"].setdefault(product_id, {"history": []})
    entry["name"] = name
    entry["current"] = listing
    entry["considered"] = considered      # returned by eBay
    entry["relevant"] = relevant          # passed title screening
    entry["verified"] = verified          # had item detail fetched and scored
    entry["kept"] = kept                  # passed every check (0 or 1)
    entry["rejected"] = rejected[:10]     # why the cheaper ones were turned down
    entry["min_trust_score"] = min_trust_score
    entry["error"] = error
    entry["checked_at"] = utc_now_iso()

    # Only real prices enter history; a failed run must not look like a $0 low.
    if price is not None:
        history = entry.setdefault("history", [])
        history.append({"t": utc_now_iso(), "price": price})
        if len(history) > MAX_HISTORY_POINTS:
            del history[: len(history) - MAX_HISTORY_POINTS]
        prices = [p["price"] for p in history]
        entry["all_time_low"] = min(prices)
        entry["all_time_high"] = max(prices)
