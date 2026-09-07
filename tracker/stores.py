"""Live inventory from local card shops running TCGplayer Pro.

Carter does not buy sealed boxes off eBay -- the one time he did, loose packs
cost ~25-30% more per pack than the box would have. What he actually does is
drive to a shop. So this module reads what the shops near him have on the
shelf, which the eBay side of this tracker can never see.

WHY THIS WORKS AT ALL. A TCGplayer Pro storefront is a 563-byte HTML shell
that loads storefronts-app.tcgplayer.com/app.js, and that app's axios client
is configured `baseURL: window.location.origin`. So the API is served from
each store's OWN subdomain, and the identical paths work for every store on
the platform. Write the client once, point it at any shop.

    GET /api/site                      store name, menus
    GET /api/catalog/productLines      games carried; per-set availableQuantity
    GET /api/catalog/products          paged catalogue, with lowestPrice
    GET /api/inventory/skus?productIds=  per-SKU price, QUANTITY, condition

THE FIELD THAT MAKES THIS VALUABLE: `productId` here is the same TCGplayer
product id used by cardprices.py. A store's shelf joins to TCGplayer's market
price directly, with no name matching and therefore none of the variant risk
that makes name lookups unusable for this game.

TWO TRAPS, both of which fail quietly:

1. `productLineName` wants the URL-NAME ("one-piece-card-game"), not the
   display name. Passing the display name returns {"totalItems":0,"items":[]}
   -- a valid, empty, entirely believable "this shop has no One Piece."

2. A plain curl/requests call gets HTTP 403 from the AWS load balancer. It
   needs a real browser User-Agent plus Accept headers. The 403 has no body,
   so it reads like the store is gone rather than like a blocked client.

Not every shop is on this platform, and there is no index of the ones that
are -- stores.yaml is maintained by hand. That is fine at Carter's scale: the
question is which of a handful of shops within driving range has a card, not
which of ten thousand.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import requests
import yaml

from .config import REPO_ROOT

DEFAULT_STORES = REPO_ROOT / "stores.yaml"
ONE_PIECE = "one-piece-card-game"

# The load balancer 403s anything that does not look like a browser.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class StoreError(RuntimeError):
    """A store could not be read. Never fatal -- other stores still run."""


@dataclass
class Store:
    id: str
    name: str
    url: str
    city: str = ""
    note: str = ""

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


@dataclass
class Listing:
    """One SKU sitting on one shop's shelf."""

    store_id: str
    store_name: str
    product_id: int
    name: str
    set_name: str
    rarity: str
    condition: str
    language: str
    is_foil: bool
    price: float
    quantity: int

    @property
    def is_near_mint(self) -> bool:
        return self.condition.lower().startswith("near mint")

    def as_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id, "store_name": self.store_name,
            "product_id": self.product_id, "name": self.name,
            "set_name": self.set_name, "rarity": self.rarity,
            "condition": self.condition, "language": self.language,
            "is_foil": self.is_foil, "price": self.price,
            "quantity": self.quantity,
        }


def load_stores(path: Path | None = None) -> list[Store]:
    path = path or DEFAULT_STORES
    if not path.exists():
        raise FileNotFoundError(f"no store list at {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    out = []
    for s in raw.get("stores") or []:
        if not s.get("enabled", True):
            continue
        out.append(Store(id=s["id"], name=s.get("name", s["id"]), url=s["url"],
                         city=s.get("city", ""), note=s.get("note", "")))
    return out


def _session(session: requests.Session | None = None) -> requests.Session:
    s = session or requests.Session()
    s.headers.update(_HEADERS)
    return s


def _get(store: Store, path: str, session: requests.Session, **params) -> Any:
    r = session.get(f"{store.base}{path}", params=params or None, timeout=30,
                    headers={"Referer": store.base + "/"})
    if r.status_code == 403:
        raise StoreError(
            f"{store.id}: HTTP 403 -- the load balancer rejected the client. "
            "This is a header problem, not a missing store."
        )
    if not r.ok:
        raise StoreError(f"{store.id}: HTTP {r.status_code} on {path}")
    return r.json()


def set_availability(store: Store, product_line: str = ONE_PIECE,
                     session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Per-set stock counts. One call, cheap -- good for a health check."""
    s = _session(session)
    lines = _get(store, "/api/catalog/productLines", s)
    for pl in lines:
        if pl.get("urlName") == product_line:
            return [
                {"set": st.get("name"), "available": st.get("availableQuantity") or 0,
                 "total": st.get("totalQuantity") or 0}
                for st in pl.get("sets") or []
            ]
    return []


def catalogue(store: Store, product_line: str = ONE_PIECE, *, page_size: int = 200,
              max_pages: int = 40, pause: float = 0.3,
              session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Every product the store lists for a game, with its lowest price.

    `lowestPrice` is the product-level floor across conditions. It is the
    cheap screen; call `skus()` on the shortlist for real quantities.
    """
    s = _session(session)
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        d = _get(store, "/api/catalog/products", s,
                 productLineName=product_line, page=page, pageSize=page_size)
        batch = d.get("items") or []
        items += batch
        if not batch or len(items) >= (d.get("totalItems") or 0):
            break
        time.sleep(pause)
    return items


def skus(store: Store, product_ids: list[int], *, batch: int = 40, pause: float = 0.3,
         session: requests.Session | None = None) -> Iterator[Listing]:
    """Live per-SKU price, condition and QUANTITY. Batched by product id.

    A product with an empty `skus` list is not actually on the shelf, even
    though it appeared in the catalogue -- so absence here is the real
    in-stock test, not presence in `catalogue()`.
    """
    s = _session(session)
    by_id = {}
    for i in range(0, len(product_ids), batch):
        chunk = product_ids[i:i + batch]
        try:
            d = _get(store, "/api/inventory/skus", s,
                     productIds=",".join(str(p) for p in chunk))
        except StoreError:
            continue
        for entry in d:
            by_id[entry.get("productId")] = entry.get("skus") or []
        time.sleep(pause)

    for pid, rows in by_id.items():
        for sk in rows:
            qty = sk.get("quantity") or 0
            if qty <= 0:
                continue
            yield Listing(
                store_id=store.id, store_name=store.name, product_id=int(pid),
                name="", set_name="", rarity="",
                condition=sk.get("conditionName") or "",
                language=sk.get("languageName") or "",
                is_foil=bool(sk.get("isFoil")),
                price=float(sk.get("price") or 0.0), quantity=int(qty),
            )
