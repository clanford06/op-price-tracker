"""Live card market values from TCGplayer.

The sealed-box side of this tracker prices things by searching eBay and
verifying listings. Singles cannot work that way: an eBay search for a card
number returns every printing of that card in every language, and the spread
between them is enormous -- the Kaido 062 English Super Alternate Art asks
3.25x its Japanese printing, and its Manga printing is a different card again
at $1,234. Any name-based lookup will eventually price the wrong card.

So singles are pinned by TCGplayer PRODUCT ID, recorded per holding in
ledger.yaml. A product id names exactly one printing in one language, and it
never drifts. Nothing here searches by name; if a holding has no id it keeps
its hand-entered estimate and is reported as manual.

Two undocumented endpoints, both unauthenticated:

    mp-search-api.tcgplayer.com/v1/search/request   POST, ?q= in the URL
    mpapi.tcgplayer.com/v2/product/{id}/pricepoints GET

The search is here only to FIND ids when adding a card (`--find-card`), and it
searches by card number alone -- adding the character name makes TCGplayer's
fuzzy matcher confidently return a different card, which is how "OP17-020
Shanks" once resolved to the OP13-028 Shanks SP.

Price points split Normal from Foil. Foil is what almost every hit in this
collection actually is, so foil market price wins; a card with no foil
printing (the standard P-110 promo) falls back to Normal. Market price, not
listed median: the median is what sellers are ASKING and on a fresh set runs
well above what clears. On 2026-08-30 the Oden's foil market was $213.39 while
its listed median was $325.00, a 52% gap.

Graded cards are deliberately not priceable here. TCGplayer sells raw singles;
a PSA 10 is a different asset with its own market, so the PSA holdings keep
their manual estimates rather than silently inheriting a raw price.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

SEARCH = "https://mp-search-api.tcgplayer.com/v1/search/request?q={q}&isList=false"
PRICEPOINTS = "https://mpapi.tcgplayer.com/v2/product/{pid}/pricepoints"
PRODUCT_URL = "https://www.tcgplayer.com/product/{pid}"
UA = "Mozilla/5.0 (compatible; op-price-tracker/1.0; personal collection tracker)"

# A quote this far from the estimate it replaces is reported but NOT applied.
# Fresh-set prices genuinely move 20-40% in a week, so the bar is set where a
# move stops looking like the market and starts looking like a wrong product
# id or a decimal error.
SANITY_RATIO = 4.0

# ...but only once real money is involved. A ratio test alone rejected the
# Edward Newgate base SR falling $6.72 -> $1.65, which is a 4.07x move and also
# exactly what a $7 card does when a set leaves presale. Guarding on the ratio
# AND the dollar swing keeps the check where it earns its keep -- catching a
# mis-pinned id on a $340 card -- without vetoing normal churn on cheap ones.
SANITY_ABS = 25.00


@dataclass
class Quote:
    tcgplayer_id: int
    foil_market: float | None = None
    normal_market: float | None = None
    listed_median: float | None = None
    error: str = ""

    @property
    def price(self) -> float | None:
        """Foil market, or Normal for the few cards with no foil printing."""
        return self.foil_market if self.foil_market is not None else self.normal_market

    @property
    def printing(self) -> str:
        if self.foil_market is not None:
            return "foil"
        return "normal" if self.normal_market is not None else ""

    def as_dict(self) -> dict:
        return {
            "tcgplayer_id": self.tcgplayer_id,
            "price": round(self.price, 2) if self.price is not None else None,
            "printing": self.printing,
            "foil_market": self.foil_market,
            "normal_market": self.normal_market,
            "listed_median": self.listed_median,
            "url": PRODUCT_URL.format(pid=self.tcgplayer_id),
            "error": self.error,
        }


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


def quote(pid: int, session: requests.Session | None = None) -> Quote:
    s = session or _session()
    try:
        r = s.get(PRICEPOINTS.format(pid=pid), timeout=25)
        if r.status_code != 200:
            return Quote(pid, error=f"HTTP {r.status_code}")
        rows = {row.get("printingType"): row for row in r.json()}
    except (requests.RequestException, ValueError) as exc:
        return Quote(pid, error=str(exc)[:120])

    foil, normal = rows.get("Foil") or {}, rows.get("Normal") or {}
    q = Quote(
        pid,
        foil_market=foil.get("marketPrice"),
        normal_market=normal.get("marketPrice"),
        listed_median=foil.get("listedMedianPrice") or normal.get("listedMedianPrice"),
    )
    if q.price is None:
        # A live product with no market price has not sold yet -- normal for a
        # card still on presale. Say so rather than reporting a silent zero.
        q.error = "no market price yet (unsold / presale)"
    return q


def find_card(number: str, limit: int = 12) -> list[dict]:
    """Candidate products for a card NUMBER, e.g. 'OP17-062'. Ids for humans.

    Pass the number alone. Adding the character name makes the fuzzy matcher
    return a confident wrong answer from an unrelated set.
    """
    body = {
        "algorithm": "sales_synonym_v2", "from": 0, "size": limit,
        "filters": {"term": {"productLineName": ["one-piece-card-game"]},
                    "range": {}, "match": {}},
        "listingSearch": {"context": {"cart": {}},
                          "filters": {"term": {"sellerStatus": "Live", "channelId": 0},
                                      "range": {"quantity": {"gte": 1}},
                                      "exclude": {"channelExclusion": 0}}},
        "context": {"cart": {}, "shippingCountry": "US", "userProfile": {}},
        "settings": {"useFuzzySearch": True, "didYouMean": {}}, "sort": {},
    }
    r = _session().post(SEARCH.format(q=urllib.parse.quote(number)), json=body, timeout=30)
    r.raise_for_status()
    return [
        {"tcgplayer_id": int(p["productId"]), "name": p["productName"],
         "rarity": p.get("rarityName", ""), "set": p.get("setName", ""),
         "market": p.get("marketPrice")}
        for p in r.json()["results"][0]["results"]
    ]


@dataclass
class RefreshResult:
    cards: dict[str, dict] = field(default_factory=dict)
    applied: int = 0
    failed: int = 0
    rejected: list[str] = field(default_factory=list)


def refresh(ledger, *, verbose: bool = True) -> RefreshResult:
    """Quote every holding that carries a tcgplayer_id."""
    out = RefreshResult()
    session = _session()

    for h in ledger.holdings:
        if not h.tcgplayer_id:
            continue
        q = quote(h.tcgplayer_id, session)
        unit = q.price
        was = h.estimate
        row = {**q.as_dict(), "name": h.name, "qty": h.qty,
               "previous_estimate": was, "value": None, "applied": False}

        if unit is None:
            out.failed += 1
            if verbose:
                print(f"  !  {h.name[:44]:<44} {q.error}")
            out.cards[h.id] = row
            continue

        value = round(unit * h.qty, 2)
        row["value"] = value

        # A quote that disagrees with the standing estimate by more than the
        # sanity ratio is far more likely to be a mis-pinned product id than a
        # real move, and applying it silently would corrupt the position.
        wild = was and was > 0 and not (1 / SANITY_RATIO <= value / was <= SANITY_RATIO)
        if wild and abs(value - was) >= SANITY_ABS:
            out.rejected.append(f"{h.name}: ${was:,.2f} -> ${value:,.2f}")
            out.cards[h.id] = row
            if verbose:
                print(f"  ?  {h.name[:44]:<44} ${was:,.2f} -> ${value:,.2f}  REJECTED, "
                      f"check tcgplayer_id {h.tcgplayer_id}")
            continue

        row["applied"] = True
        out.applied += 1
        out.cards[h.id] = row
        if verbose:
            delta = f"{value - was:+,.2f}" if was is not None else "new"
            qty = f" x{h.qty}" if h.qty > 1 else ""
            print(f"  ok {h.name[:44]:<44} ${unit:>8,.2f}{qty:<4} {q.printing:<6} "
                  f"= ${value:>9,.2f}  ({delta})")
    return out


def write(path: Path, result: RefreshResult) -> None:
    from .storage import utc_now_iso

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"schema": 1, "updated_at": utc_now_iso(), "source": "tcgplayer price points",
         "basis": "foil market price, or normal where the card has no foil printing",
         "applied": result.applied, "failed": result.failed,
         "rejected": result.rejected, "cards": result.cards},
        indent=2) + "\n", encoding="utf-8")


def apply_to(ledger, path: Path) -> int:
    """Overlay the last refresh onto a freshly loaded ledger. Returns count.

    Called on every load so the dashboard, the terminal report and the issue
    workflow all agree. Missing or stale file is not an error -- the ledger's
    hand-entered estimates are the fallback, and a failed quote must never
    blank a holding to zero.
    """
    if not path.exists():
        return 0
    try:
        cards = json.loads(path.read_text(encoding="utf-8")).get("cards") or {}
    except (OSError, ValueError):
        return 0

    n = 0
    for h in ledger.holdings:
        row = cards.get(h.id)
        if not row or not row.get("applied") or row.get("value") is None:
            continue
        h.estimate_manual = h.estimate
        h.estimate = float(row["value"])
        h.estimate_source = "tcgplayer"
        h.tcgplayer_url = row.get("url", "")
        n += 1
    return n
