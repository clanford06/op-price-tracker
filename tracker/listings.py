"""Turn holdings into priced eBay listing drafts.

The point is not to guess what a card might fetch. It is to answer one
question per card: what do I have to ask so that, after eBay takes its cut, I
keep what the card is actually worth? Everything here is arithmetic on
TCGplayer market prices, which are what cards really sell for, rather than on
eBay asks, which on this collection run 1.5x to 2.5x market and largely sit
unsold.

    ask   = the price that nets you TCGplayer market
    floor = the lowest offer still worth accepting (nets 85% of market)

Two rules keep it honest.

Cheap cards are not listed. A $2 card nets $1.33 after the 13.25% fee and the
$0.40 per-order charge, before an envelope, a sleeve, a toploader and a trip
to the post office. Below `single_floor` a card is worth more as part of a
bulk lot than as its own listing, and this says so rather than generating
twenty listings that each lose money.

Copies already committed to a live listing are subtracted. Carter has two
Shanks OP17-020 and one is in the leader lot, so only one is sellable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# eBay's per-order charge applies once per sale, so it is folded into the ask
# rather than ignored. Shipping is assumed buyer-paid, which is how singles in
# this category are normally listed.
DEFAULT_SINGLE_FLOOR = 10.00
OFFER_RETENTION = 0.85     # accept down to 85% of market before it is a bad trade


@dataclass
class Draft:
    holding_id: str
    name: str
    number: str
    variant: str
    rarity: str
    qty: int                # copies actually available to sell
    market: float           # TCGplayer market, per copy
    ask: float
    floor: float
    net_at_ask: float
    net_at_floor: float
    title: str

    @property
    def line_value(self) -> float:
        return round(self.market * self.qty, 2)


# Parentheticals that only repeat the card number, e.g. "Kaido (062)".
_NOISE = re.compile(r"^\d+$|^english$", re.I)


def _parse(name: str) -> tuple[str, str, str]:
    """Split 'OP17-062 Kaido (062) (Super Alternate Art)' into its parts."""
    name = name.replace('"', "").replace("\u2014", "-")
    num = ""
    m = re.match(r"^((?:OP|EB|ST|P)[-0-9]*\d)\s+(.*)$", name)
    rest = name
    if m:
        num, rest = m.group(1), m.group(2)
    else:
        # Slabs read "PSA 10 Eustass Captain Kidd SP EB04-039", with the number
        # at the end rather than the front.
        tail = re.search(r"\b((?:OP|EB|ST)\d\d-\d{3})\b", rest)
        if tail:
            num = tail.group(1)
            rest = (rest[:tail.start()] + rest[tail.end():]).strip()
    parens = [v.strip() for v in re.findall(r"\(([^)]*)\)", rest)]
    # Keep every meaningful parenthetical, not just the first that looks like a
    # variant. "DON!! Card (World United) (Luffy)" is two different cards from
    # "DON!! Card (World United) (Zoro)", and dropping the second made both
    # titles identical.
    # Also drop a parenthetical that just repeats the card number, which is how
    # the app names SP reprints: "Marshall.D.Teach (ST27-005) (SP)".
    keep = [v for v in parens
            if not _NOISE.match(v) and v.upper() != num.upper()]
    variant = " ".join(keep)
    base = re.sub(r"\s*\([^)]*\)", "", rest).strip()
    return num, base, variant


def _title(number: str, base: str, variant: str, rarity: str) -> str:
    """eBay allows 80 characters. Lead with what people search."""
    # A slab is searched for by its grade first, so that leads.
    grade = ""
    gm = re.match(r"^(PSA|BGS|CGC)\s*([\d.]+)\s+(.*)$", base, re.I)
    if gm:
        grade, base = f"{gm.group(1).upper()} {gm.group(2)}", gm.group(3).strip()
    bits = [grade, "One Piece", base, number] if grade else ["One Piece", base, number]
    if variant and variant.lower() not in base.lower():
        bits.append(variant)
    if rarity and rarity not in ("None", ""):
        bits.append({"Secret Rare": "SEC", "Super Rare": "SR",
                     "Leader": "Leader", "Rare": "R"}.get(rarity, rarity))
    bits += ["English", "Foil"] + ([] if grade else ["NM"])
    title = " ".join(b for b in bits if b)
    while len(title) > 80 and len(bits) > 4:
        bits.pop(-4)                       # drop rarity, then variant
        title = " ".join(b for b in bits if b)
    return title[:80]


def price_for(market: float, fee_pct: float, fee_flat: float) -> tuple[float, float]:
    """(ask, floor). The ask nets `market`; the floor nets 85% of it."""
    ask = (market + fee_flat) / (1 - fee_pct / 100)
    floor = (market * OFFER_RETENTION + fee_flat) / (1 - fee_pct / 100)
    return round(ask + 0.004, 2), round(floor + 0.004, 2)


def build(ledger, *, tags: set[str], single_floor: float = DEFAULT_SINGLE_FLOOR,
          committed: dict[int, int] | None = None) -> tuple[list[Draft], list, list]:
    """Returns (individual drafts, bulk holdings, not-sellable holdings)."""
    committed = committed or {}
    drafts: list[Draft] = []
    bulk: list = []
    blocked: list = []

    for h in ledger.holdings:
        if h.estimate is None or h.liquid or h.tag not in tags:
            continue
        # A card sitting at PSA cannot be listed, whatever it is worth.
        if h.status == "grading":
            blocked.append((h, "at PSA, not in hand"))
            continue
        avail = h.qty - committed.get(h.tcgplayer_id or -1, 0)
        if avail <= 0:
            blocked.append((h, "every copy is already in a live listing"))
            continue

        unit = round(h.estimate / h.qty, 2)
        if unit < single_floor:
            bulk.append((h, avail, unit))
            continue

        num, base, variant = _parse(h.name)
        ask, floor = price_for(unit, ledger.fee_pct, ledger.fee_flat)
        net = lambda p: round(p - (p * ledger.fee_pct / 100 + ledger.fee_flat), 2)
        drafts.append(Draft(
            holding_id=h.id, name=h.name, number=num, variant=variant,
            rarity=(h.source.split("·")[1].strip() if "·" in h.source else ""),
            qty=avail, market=unit, ask=ask, floor=floor,
            net_at_ask=net(ask), net_at_floor=net(floor),
            title=_title(num, base, variant,
                         h.source.split("·")[1].strip() if "·" in h.source else ""),
        ))

    drafts.sort(key=lambda d: -d.market)
    bulk.sort(key=lambda b: -b[2])
    return drafts, bulk, blocked


def render(drafts, bulk, blocked, ledger, single_floor: float) -> str:
    m = lambda v: f"${v:,.2f}"
    out: list[str] = []
    out.append("# One Piece listing drafts\n")
    out.append(f"Generated from TCGplayer market prices. Fees modelled at "
               f"{ledger.fee_pct}% + {m(ledger.fee_flat)} per order, shipping buyer-paid.\n")
    out.append("`ask` nets you TCGplayer market. `floor` is the lowest offer still worth "
               "taking, at 85% of market. Below the floor you are better off keeping the "
               "card.\n")

    tot_ask = sum(d.ask * d.qty for d in drafts)
    tot_net = sum(d.net_at_ask * d.qty for d in drafts)
    out.append(f"\n**{len(drafts)} individual listings · {sum(d.qty for d in drafts)} copies "
               f"· {m(tot_ask)} asking · {m(tot_net)} net if they all sell at ask**\n")

    for d in drafts:
        out.append(f"\n## {d.title}")
        out.append(f"```\n{d.title}\n```")
        out.append(f"    card number   {d.number}")
        out.append(f"    quantity      {d.qty}")
        out.append(f"    market        {m(d.market)} each")
        out.append(f"    ASK           {m(d.ask)}   -> you net {m(d.net_at_ask)}")
        out.append(f"    min offer     {m(d.floor)}   -> you net {m(d.net_at_floor)}")

    if bulk:
        bv = sum(u * a for _, a, u in bulk)
        out.append(f"\n\n## Bulk lot — everything under {m(single_floor)} a card\n")
        out.append(f"{len(bulk)} lines, {sum(a for _, a, _ in bulk)} copies, "
                   f"{m(bv)} at market.\n")
        out.append("Listed individually these lose money: the flat fee alone is "
                   f"{m(ledger.fee_flat)} per sale before an envelope and a toploader. "
                   "Sell them as one lot or keep them.\n")
        ask, _ = price_for(bv, ledger.fee_pct, ledger.fee_flat)
        out.append(f"    ASK as one lot   {m(ask)}\n")
        for h, a, u in bulk:
            out.append(f"      {a} x {m(u):>7}  {h.name[:56]}")

    if blocked:
        out.append("\n\n## Not listable\n")
        for h, why in blocked:
            out.append(f"    {m(h.estimate or 0):>9}  {h.name[:50]}  ({why})")
    return "\n".join(out) + "\n"
