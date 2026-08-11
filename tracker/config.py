"""Watchlist and environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WATCHLIST = REPO_ROOT / "watchlist.yaml"
DEFAULT_DATA_FILE = REPO_ROOT / "docs" / "data.json"
DEFAULT_LEDGER = REPO_ROOT / "ledger.yaml"


@dataclass
class Product:
    id: str
    name: str
    query: str
    min_price: float
    max_price: float
    alert_below: float | None
    require_all: list[str] = field(default_factory=list)
    require_any: list[str] = field(default_factory=list)
    exclude_any: list[str] = field(default_factory=list)
    min_seller_score: int = 100
    min_seller_pct: float = 98.0
    implausible_below: float | None = None
    # Verification policy
    min_trust_score: int = 70
    require_returns: bool = True
    require_us_location: bool = True
    require_verified_detail: bool = True
    max_quantity: int = 12
    verify_top_n: int = 8
    # Purchase scoring
    unit_kind: str = "box"        # box | pack | case
    packs_in_unit: int = 24       # English booster box = 24 packs
    ev_per_pack: float | None = None   # configured estimate; None disables the EV term
    sp_per_box: float | None = None    # SPs per box; drives the dud-chance figure
    blocked_sellers: list[str] = field(default_factory=list)
    trusted_sellers: list[str] = field(default_factory=list)


@dataclass
class Settings:
    ebay_client_id: str
    ebay_client_secret: str
    ntfy_topic: str
    ntfy_server: str
    alert_on_new_low: bool


def load_settings() -> Settings:
    return Settings(
        ebay_client_id=os.environ.get("EBAY_CLIENT_ID", "").strip(),
        ebay_client_secret=os.environ.get("EBAY_CLIENT_SECRET", "").strip(),
        ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip(),
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/"),
        alert_on_new_low=os.environ.get("ALERT_ON_NEW_LOW", "true").lower() != "false",
    )


def load_products(path: Path | None = None) -> list[Product]:
    path = path or DEFAULT_WATCHLIST
    if not path.exists():
        raise FileNotFoundError(f"Watchlist not found: {path}")

    doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults: dict[str, Any] = doc.get("defaults") or {}
    entries: list[dict[str, Any]] = doc.get("products") or []
    if not entries:
        raise ValueError(f"No products defined in {path}")

    products: list[Product] = []
    for raw in entries:
        merged = {**defaults, **raw}
        missing = [k for k in ("id", "name", "query") if not merged.get(k)]
        if missing:
            raise ValueError(f"Product entry missing {missing}: {raw}")
        products.append(
            Product(
                id=str(merged["id"]),
                name=str(merged["name"]),
                query=str(merged["query"]),
                min_price=float(merged.get("min_price", 30)),
                max_price=float(merged.get("max_price", 500)),
                alert_below=(
                    float(merged["alert_below"]) if merged.get("alert_below") is not None else None
                ),
                require_all=list(merged.get("require_all") or []),
                require_any=list(merged.get("require_any") or []),
                exclude_any=list(merged.get("exclude_any") or []),
                min_seller_score=int(merged.get("min_seller_score", 100)),
                min_seller_pct=float(merged.get("min_seller_pct", 98.0)),
                implausible_below=(
                    float(merged["implausible_below"])
                    if merged.get("implausible_below") is not None
                    else None
                ),
                min_trust_score=int(merged.get("min_trust_score", 70)),
                require_returns=bool(merged.get("require_returns", True)),
                require_us_location=bool(merged.get("require_us_location", True)),
                require_verified_detail=bool(merged.get("require_verified_detail", True)),
                max_quantity=int(merged.get("max_quantity", 12)),
                verify_top_n=int(merged.get("verify_top_n", 8)),
                unit_kind=str(merged.get("unit_kind", "box")),
                packs_in_unit=int(merged.get("packs_in_unit", 24)),
                ev_per_pack=(
                    float(merged["ev_per_pack"]) if merged.get("ev_per_pack") is not None else None
                ),
                sp_per_box=(
                    float(merged["sp_per_box"]) if merged.get("sp_per_box") is not None else None
                ),
                blocked_sellers=[str(s) for s in (merged.get("blocked_sellers") or [])],
                trusted_sellers=[str(s) for s in (merged.get("trusted_sellers") or [])],
            )
        )

    ids = [p.id for p in products]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate product id(s) in watchlist: {sorted(dupes)}")
    return products
