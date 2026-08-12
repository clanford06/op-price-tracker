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


STACKED = "https://www.tcgstacked.com/onepiece/card/{card_id}"

# How far two sources may disagree before the price is treated as unconfirmed.
# Sources sample different marketplaces, so some spread is normal; a doubling
# is not, and that is roughly the size of the error that put a $1,866 prize
# card into OP-12's chase list.
DISAGREE_RATIO = 0.5


# Which TCG Stacked printing label corresponds to which Limitless variant.
# A bare card-id URL on Stacked lands on whichever printing it defaults to --
# sometimes the manga rare, sometimes a $2 reprint -- so comparing without
# checking this flagged 70 of 161 cards as "disputed" when the sources were
# simply describing different cards.
PRINT_EQUIV = {
    "manga rare": {"manga"},
    "alt art": {"alternate art", "alt art", "aa"},
    "special art": {"special art", "sp"},
    "base": {"regular", "reprint", "base", "normal"},
    "parallel": {"parallel"},
}


def stacked_quote(session: requests.Session, card_id: str) -> tuple[float, str] | None:
    """(price, printing label) from TCG Stacked. None if unavailable."""
    try:
        r = session.get(STACKED.format(card_id=card_id),
                        headers={"User-Agent": UA}, timeout=25)
        if r.status_code != 200:
            return None
        price_m = re.search(r'"market_price"\s*:\s*([0-9.]+)', r.text)
        if not price_m:
            return None
        # The page names its printing in the FAQ heading, e.g.
        # "How much is Jewelry Bonney 118 Manga worth?"
        label_m = re.search(r"How much is ([^?]+?) worth\?", r.text)
        label = _unescape(label_m.group(1)).strip().lower() if label_m else ""
        return float(price_m.group(1)), label
    except (requests.RequestException, ValueError):
        return None


def _same_printing(variant: str, stacked_label: str) -> bool:
    """Do the two sources describe the same printing?

    Conservative: an unrecognised label counts as NOT comparable, so an
    ambiguous match never produces a confident agree/disagree verdict.
    """
    if not stacked_label:
        return False
    words = set(re.findall(r"[a-z]+", stacked_label))
    wanted = PRINT_EQUIV.get(variant, set())
    if any(all(t in words for t in phrase.split()) for phrase in wanted):
        return True
    # A label naming a DIFFERENT printing is a definite mismatch.
    for other, phrases in PRINT_EQUIV.items():
        if other == variant and True:
            continue
        if any(all(t in words for t in phrase.split()) for phrase in phrases):
            return False
    return False


@dataclass
class Printing:
    card_id: str
    name: str
    rarity: str
    variant: str
    price: float
    url: str
    price2: float | None = None      # second source
    agree: bool | None = None        # None = not comparable, NOT "agrees"
    compared: str = ""               # what the second source was actually quoting

    def as_dict(self) -> dict:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "rarity": self.rarity,
            "variant": self.variant,
            "price": round(self.price, 2),
            "price2": round(self.price2, 2) if self.price2 is not None else None,
            "agree": self.agree,
            "compared": self.compared,
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


def set_title(session: requests.Session, set_code: str) -> str:
    """The set's display name, e.g. 'Legacy of the Master'.

    Needed because a card page's prints table lists every printing of that card
    across ALL sets -- Prize Cards, promos, cross-set reprints -- and those
    prices must not be attributed to a box you can buy.
    """
    html = _get(session, f"/cards/{set_code}")
    if not html:
        return ""
    m = re.search(r"<title>\s*(.*?)\s*\(" + re.escape(set_code) + r"\)", html)
    return _unescape(m.group(1)).strip() if m else ""


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


def card_printings(
    session: requests.Session, card_id: str, name: str, rarity: str, set_name: str
) -> list[Printing]:
    """Printings of this card FROM THIS SET only.

    The prints table also carries Prize Cards, promos and other sets' versions.
    Counting those produced a $1,866 'OP-12 chase' that was actually a prize
    card -- the real OP-12 alternate art was $24.55. Anything not from
    `set_name` is discarded.
    """
    html = _get(session, f"/cards/{card_id}")
    if not html:
        return []

    printings: list[Printing] = []
    for row in re.split(r"<tr\b", html)[1:]:
        if "card-price usd" not in row:
            continue
        label_m = re.search(r'prints-table-card-number">([^<]*)</span>', row)
        price_m = re.search(r'card-price usd[^>]*>\$([0-9,]+\.[0-9]{2})</a>', row)
        set_m = re.search(r"<td>\s*<a[^>]*>\s*([^<]+?)\s*<span", row)
        if not price_m:
            continue
        row_set = _unescape(set_m.group(1)).strip() if set_m else ""
        if set_name and row_set and row_set.lower() != set_name.lower():
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
    set_name = set_title(session, set_code)

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
        got = card_printings(session, cid, name, rarity, set_name)
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
    # Cross-check each chase against a second source. Limitless alone reported
    # a $1,866 OP-12 "chase" that was a prize card; a second opinion catches
    # that class of error even when the parse looks clean.
    for pr in top:
        quote = stacked_quote(session, pr.card_id)
        time.sleep(delay)
        if quote is None or quote[0] <= 0:
            continue
        other, label = quote
        if not _same_printing(pr.variant, label):
            # Same card number, different printing. Recording this as a
            # disagreement would be a lie about what was compared.
            pr.price2 = other
            pr.compared = f"second source shows the {label or 'unknown'} printing"
            continue
        pr.price2 = other
        pr.compared = label
        hi, lo = max(pr.price, other), min(pr.price, other)
        pr.agree = (lo / hi) >= DISAGREE_RATIO

    return [p.as_dict() for p in top]
