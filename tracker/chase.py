"""Chase-card lookup: the cards actually worth pulling from each set.

Source is Limitless (onepiece.limitlesstcg.com), which is server-rendered and
does not block automated reads -- unlike TCGplayer, PriceCharting and Cardmarket,
all of which refuse.

Two passes, because the obvious one-pass version is wrong:

  1. The set list page carries every card with rarity and price, but only the
     BASE printing. Ranking on that alone surfaces $20 secret rares and misses
     the $600 alternate arts entirely -- the base row for a card whose alt art
     is the real chase often reads a few cents.
  2. So for the plausible candidates, fetch the card page and read its prints
     table, which lists every printing (base, `aa` alternate art, `sp` special,
     manga) with its own price. Rank across all printings.

Candidates are limited by rarity and price so this stays ~25 fetches per set
rather than ~155. Meant to run nightly, not every two hours.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

BASE = "https://onepiece.limitlesstcg.com"
UA = "Mozilla/5.0 (compatible; op-price-tracker/1.0; personal collection tracker)"

# Rarities whose alternate printings are worth checking. Commons and uncommons
# occasionally get an alt art, so a price floor catches those too.
CANDIDATE_RARITIES = {"Secret Rare", "Super Rare", "Leader", "Rare"}
CANDIDATE_PRICE_FLOOR = 0.40
MAX_CANDIDATES = 48

VARIANT_NAMES = {
    "": "base",
    "aa": "alt art",
    "sp": "special art",
    "manga": "manga rare",
    "p1": "parallel",
    "p2": "parallel 2",
}


@dataclass
class Printing:
    card_id: str
    name: str
    rarity: str
    variant: str
    price: float
    url: str

    def as_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "rarity": self.rarity,
            "variant": self.variant,
            "price": round(self.price, 2),
            "url": self.url,
        }


def _get(session: requests.Session, path: str) -> str | None:
    try:
        r = session.get(f"{BASE}{path}", headers={"User-Agent": UA}, timeout=30)
        return r.text if r.status_code == 200 else None
    except requests.RequestException:
        return None


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _unescape(text: str) -> str:
    import html as _h

    return _h.unescape(text)


def set_cards(session: requests.Session, set_code: str) -> list[tuple[str, str, str, float]]:
    """(card_id, name, rarity, base_price) for every card in the set."""
    html = _get(session, f"/cards/{set_code}?display=list")
    if not html or "card-list" not in html:
        return []

    table = html[html.find("card-list"):]
    out: list[tuple[str, str, str, float]] = []
    for row in re.split(r"<tr\b", table)[1:]:
        cid = re.search(r'/cards/([A-Z0-9-]+)"', row)
        cells = [_unescape(_strip(c)) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if not cid or len(cells) < 5:
            continue
        price = next(
            (float(m.group(1).replace(",", ""))
             for m in (re.match(r"^\$([0-9,]+\.[0-9]{2})$", c) for c in cells) if m),
            None,
        )
        if price is None:
            continue
        out.append((cid.group(1), cells[1], cells[3], price))
    return out


def card_printings(session: requests.Session, card_id: str, name: str, rarity: str) -> list[Printing]:
    """Every printing of one card, from its prints table."""
    html = _get(session, f"/cards/{card_id}")
    if not html:
        return []

    printings: list[Printing] = []
    for row in re.split(r"<tr\b", html)[1:]:
        if "card-price usd" not in row:
            continue
        label_m = re.search(r'prints-table-card-number">([^<]*)</span>', row)
        price_m = re.search(r'card-price usd[^>]*>\$([0-9,]+\.[0-9]{2})</a>', row)
        if not price_m:
            continue
        label = (label_m.group(1).strip().lower() if label_m else "")
        printings.append(
            Printing(
                card_id=card_id,
                name=name,
                rarity=rarity,
                variant=VARIANT_NAMES.get(label, label or "base"),
                price=float(price_m.group(1).replace(",", "")),
                url=f"{BASE}/cards/{card_id}",
            )
        )
    return printings


def top_chase(set_code: str, *, limit: int = 8, delay: float = 0.35) -> list[dict]:
    """The `limit` most valuable printings in a set, across all variants."""
    session = requests.Session()
    cards = set_cards(session, set_code)
    if not cards:
        return []

    # Leaders and Secret Rares are ALWAYS checked, whatever their base price.
    # Selecting purely on base price missed OP16-022 (Luffy Leader): base
    # $0.10, alternate art $63 -- it should have ranked third and did not
    # appear at all. A cheap base says nothing about the alt art's value.
    # Super Rares join the always-list too: manga rares hang off Rares and
    # Super Rares, and capping candidates at 25 by base price dropped Kuzan's
    # $1,161 manga printing -- the single most valuable card in OP-16.
    always = [c for c in cards if c[2] in {"Leader", "Secret Rare", "Super Rare"}]
    rest = [
        c for c in cards
        if c not in always and (c[2] in CANDIDATE_RARITIES or c[3] >= CANDIDATE_PRICE_FLOOR)
    ]
    rest.sort(key=lambda c: -c[3])
    candidates = always + rest[: max(0, MAX_CANDIDATES - len(always))]

    everything: list[Printing] = []
    for cid, name, rarity, base_price in candidates:
        got = card_printings(session, cid, name, rarity)
        # If the card page will not load, keep the base row rather than
        # silently dropping a card that might be the set's chase.
        everything.extend(got or [Printing(cid, name, rarity, "base", base_price,
                                           f"{BASE}/cards/{cid}")])
        time.sleep(delay)

    everything.sort(key=lambda p: -p.price)

    # One entry per card: the chase IS the expensive printing, and listing a
    # card's base and alt art as two separate "chases" wastes the slots.
    seen: set[str] = set()
    top: list[Printing] = []
    for p in everything:
        if p.card_id in seen:
            continue
        seen.add(p.card_id)
        top.append(p)
        if len(top) >= limit:
            break
    return [p.as_dict() for p in top]
