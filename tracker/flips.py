"""Cards on a local shelf worth less than they resell for.

This is the buy side of the tracker, and it exists because the eBay side
answers a question Carter does not ask. He does not buy sealed boxes online;
he drives to a shop. So the useful question is not "what is the cheapest box
on eBay" but "is anything at the shop I'm going to mispriced."

THE ARITHMETIC, and why the bar is deliberately high.

A flip only pays if the resale clears the buy price AFTER costs, and the costs
are most of the story on a cheap card:

    proceeds = market x (1 - 0.1325) - $0.40 flat - shipping
    profit   = proceeds - what the shop charges

On a $5 card that leaves roughly $2.55 before shipping eats it. So a flip has
to clear BOTH an absolute dollar floor and a percentage floor. Percentage
alone promotes a $0.75 card that doubles; dollars alone promotes a $200 card
that moves 4%, which is inside the noise on a single comp. This is the same
two-sided guard the card-price refresh uses on suspicious moves, for the same
reason -- one ratio is never enough on its own.

WHAT THIS IS NOT. `market` here is TCGplayer's market price, which is what
cards clear at ON TCGPLAYER. Carter would be selling on eBay, where the same
card can run differently, and a market price on a thin set is an average over
very few sales. So the output is a SHORTLIST TO VERIFY against sold comps, not
a buy list. The first live scan found 93 of 97 cards priced ABOVE resale --
the honest use of this tool most days is telling him what not to buy.

Graded cards never appear here. A shop's raw single and a slab are different
assets, the same reason the PSA holdings stay off the auto-refresh.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from . import cardprices, stores
from .stores import Listing, Store

# eBay's cut, matching what portfolio.py already charges holdings.
FEE_PCT = 0.1325
FEE_FLAT = 0.40
# Plain envelope for cheap cards; tracked mailer once a card is worth claiming.
SHIP_CHEAP, SHIP_TRACKED, TRACKED_ABOVE = 1.10, 5.00, 20.00


def shipping_for(value: float) -> float:
    return SHIP_CHEAP if value < TRACKED_ABOVE else SHIP_TRACKED


def net_proceeds(market: float) -> float:
    """What actually lands in his account after eBay and postage."""
    return market * (1 - FEE_PCT) - FEE_FLAT - shipping_for(market)


@dataclass
class Flip:
    store_id: str
    store_name: str
    product_id: int
    name: str
    set_name: str
    rarity: str
    store_price: float
    market: float
    proceeds: float
    profit: float
    roi: float
    condition: str
    quantity: int
    is_foil: bool

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "store_id", "store_name", "product_id", "name", "set_name", "rarity",
            "store_price", "market", "proceeds", "profit", "roi", "condition",
            "quantity", "is_foil")}


def scan_store(store: Store, *, min_store_price: float = 2.0,
               min_profit: float = 3.0, min_roi: float = 0.30,
               near_mint_only: bool = True, pause: float = 0.35,
               verbose: bool = True) -> tuple[list[Flip], list[Flip]]:
    """Returns (flips, overpriced) for one shop.

    Screens on the store's own `lowestPrice` before spending a quote call per
    card -- at Prime Time that is 97 candidates out of 1,067 products, so the
    screen is what makes a daily run cheap rather than a thousand requests.
    """
    catalogue = stores.catalogue(store)
    if verbose:
        print(f"  {store.name}: {len(catalogue)} products listed")

    cands = [c for c in catalogue
             if (c.get("lowestPrice") or 0) >= min_store_price and not c.get("isPresale")]
    if verbose:
        print(f"  {len(cands)} at or above ${min_store_price:.2f} and not presale")

    # Real quantities and conditions for the shortlist only.
    live: dict[int, Listing] = {}
    for lst in stores.skus(store, [c["id"] for c in cands], pause=pause):
        if near_mint_only and not lst.is_near_mint:
            continue
        best = live.get(lst.product_id)
        if best is None or lst.price < best.price:
            live[lst.product_id] = lst

    flips: list[Flip] = []
    over: list[Flip] = []
    for c in cands:
        lst = live.get(c["id"])
        if lst is None:            # listed but not actually on the shelf
            continue
        try:
            q = cardprices.quote(c["id"])
        except Exception:
            continue
        market = q.price
        if not market:             # no recorded sales -- not evidence of cheap
            continue
        proceeds = net_proceeds(market)
        profit = proceeds - lst.price
        roi = profit / lst.price if lst.price else 0.0
        row = Flip(store_id=store.id, store_name=store.name, product_id=c["id"],
                   name=c.get("name") or "", set_name=c.get("setName") or "",
                   rarity=c.get("rarityName") or "", store_price=lst.price,
                   market=market, proceeds=round(proceeds, 2),
                   profit=round(profit, 2), roi=roi, condition=lst.condition,
                   quantity=lst.quantity, is_foil=lst.is_foil)
        if profit >= min_profit and roi >= min_roi:
            flips.append(row)
        elif profit < 0:
            over.append(row)
        time.sleep(pause)

    flips.sort(key=lambda f: -f.profit)
    over.sort(key=lambda f: f.profit)
    return flips, over


def scan(store_list: Iterable[Store], **kw) -> dict[str, Any]:
    verbose = kw.get("verbose", True)
    all_flips: list[Flip] = []
    all_over: list[Flip] = []
    errors: list[str] = []
    for st in store_list:
        try:
            f, o = scan_store(st, **kw)
        except stores.StoreError as exc:
            errors.append(str(exc))
            if verbose:
                print(f"  ! {exc}")
            continue
        all_flips += f
        all_over += o
    all_flips.sort(key=lambda f: -f.profit)
    return {"flips": [f.as_dict() for f in all_flips],
            "overpriced": [f.as_dict() for f in all_over],
            "errors": errors}


def report(result: dict[str, Any]) -> None:
    flips, over = result["flips"], result["overpriced"]
    print("\n" + "=" * 62)
    print("LOCAL SHELF vs RESALE")
    print("=" * 62)
    if not flips:
        print("\n  Nothing clears the bar today. That is the normal result.")
    else:
        print(f"\n  {'PROFIT':>8} {'ROI':>6} {'SHOP':>8} {'MARKET':>8}  CARD")
        print("  " + "-" * 74)
        for f in flips:
            print(f"  {f['profit']:>8.2f} {f['roi']*100:>5.0f}% {f['store_price']:>8.2f} "
                  f"{f['market']:>8.2f}  {f['name'][:30]:<30} x{f['quantity']} @ {f['store_name'][:14]}")
        print(f"\n  Profit is AFTER eBay's {FEE_PCT*100:.2f}% + ${FEE_FLAT:.2f} and postage.")
        print("  Verify against SOLD comps before buying -- market price is an")
        print("  average over few sales on thin sets, and you would sell on eBay.")
    print(f"\n  {len(over)} card(s) cost MORE at the shop than they resell for.")
    if over:
        w = over[0]
        print(f"  Worst: {w['name'][:34]} — shop ${w['store_price']:.2f}, "
              f"nets ${w['proceeds']:.2f}")
    for e in result.get("errors", []):
        print(f"  ! {e}")
