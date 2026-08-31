"""Expense and profit tracking.

Deliberately uses a *pooled cost basis* rather than trying to assign a cost to
each card. Cards pulled from a box have no individual purchase price -- the box
does. Any attempt to allocate box cost across pulls is arbitrary (by count? by
value? by rarity?) and produces per-card "profit" figures that are fiction.

So three separate ledgers:

    expenses   money that left your pocket    (boxes, entry fees, grading, shipping)
    holdings   what you own, with an estimate (unrealised)
    sales      money that came back, net of fees (realised)

    net position = realised + unrealised - spent

That answers the only question that matters -- "am I up or down overall?" --
without pretending to know what an individual pulled card cost you.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# eBay takes a final value fee on the item price AND the shipping you charge,
# plus a fixed per-order fee. Collectibles sat around 13.25% + $0.40.
# Override in ledger.yaml if your category or store subscription differs.
DEFAULT_FEE_PCT = 13.25
DEFAULT_FEE_FLAT = 0.40


@dataclass
class Expense:
    date: str
    item: str
    category: str
    amount: float
    tag: str = ""      # free-form grouping label, e.g. "op16", "op17", "grading"
    planned: bool = False
    """Committed but not yet paid.

    Kept out of `spent` deliberately. Mixing money you intend to spend with
    money that has left your account makes today's position wrong, and today's
    position is the one you make decisions from.
    """


@dataclass
class Holding:
    id: str
    name: str
    status: str               # owned | grading | listed
    estimate: float | None
    acquired: str = ""
    source: str = ""
    note: str = ""
    ebay_query: str = ""      # if set, live pricing can refresh `estimate`
    tag: str = ""
    tcgplayer_id: int | None = None
    """TCGplayer product id. Pins ONE printing in ONE language.

    Set it and `estimate` is refreshed from TCGplayer's foil market price
    automatically. Leave it unset for anything TCGplayer does not sell as a
    raw single -- graded slabs above all, whose market is separate from the
    raw card's and would be badly understated by it.
    """
    qty: int = 1              # `estimate` is the total for all copies
    estimate_manual: float | None = None   # what the ledger said, pre-overlay
    estimate_source: str = "manual"        # manual | tcgplayer
    tcgplayer_url: str = ""
    scenarios: list[dict] = field(default_factory=list)
    """Outcomes this holding could resolve to, when the value is not yet known.

    A card at PSA has no single correct estimate -- it has a grade that already
    exists and that nobody has seen. `estimate` stays at the conservative floor
    so the position is never inflated by a guess, and the branches live here so
    the upside is visible without being counted.
    """


@dataclass
class Sale:
    date: str
    item: str
    gross: float
    fees: float
    shipping_cost: float
    tag: str = ""

    @property
    def net(self) -> float:
        return round(self.gross - self.fees - self.shipping_cost, 2)


@dataclass
class Ledger:
    expenses: list[Expense] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    sales: list[Sale] = field(default_factory=list)
    fee_pct: float = DEFAULT_FEE_PCT
    fee_flat: float = DEFAULT_FEE_FLAT
    card_prices_applied: int = 0    # holdings priced from the last TCGplayer run
    card_prices_at: str = ""        # when that run happened

    # -- totals ------------------------------------------------------------

    @property
    def spent(self) -> float:
        """Money actually paid."""
        return round(sum(e.amount for e in self.expenses if not e.planned), 2)

    @property
    def planned(self) -> float:
        return round(sum(e.amount for e in self.expenses if e.planned), 2)

    @property
    def committed(self) -> float:
        return round(self.spent + self.planned, 2)

    @property
    def realised(self) -> float:
        return round(sum(s.net for s in self.sales), 2)

    @property
    def unrealised_gross(self) -> float:
        return round(sum(h.estimate or 0.0 for h in self.holdings), 2)

    def net_if_sold(self, gross: float) -> float:
        """What `gross` actually becomes after eBay's cut. Not what you list at."""
        if gross <= 0:
            return 0.0
        return round(gross * (1 - self.fee_pct / 100) - self.fee_flat, 2)

    @property
    def unrealised_net(self) -> float:
        return round(sum(self.net_if_sold(h.estimate or 0.0) for h in self.holdings), 2)

    @property
    def position_gross(self) -> float:
        return round(self.realised + self.unrealised_gross - self.spent, 2)

    @property
    def position_net(self) -> float:
        """The honest number: assumes you'd pay selling fees to realise it."""
        return round(self.realised + self.unrealised_net - self.spent, 2)

    @property
    def roi_pct(self) -> float | None:
        return round(100 * self.position_net / self.spent, 1) if self.spent else None

    def by_category(self, *, planned: bool = False) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.expenses:
            if e.planned is not planned:
                continue
            out[e.category] = round(out.get(e.category, 0.0) + e.amount, 2)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def by_tag(self) -> dict[str, dict[str, float]]:
        """Roll everything up per tag, so each thing you track has its own P/L.

        Untagged entries land under "untagged" rather than being dropped -- a
        silently missing row is worse than an ugly one.
        """
        tags: dict[str, dict[str, float]] = {}

        def slot(t: str) -> dict[str, float]:
            return tags.setdefault(t or "untagged",
                                   {"spent": 0.0, "planned": 0.0, "realised": 0.0, "held": 0.0})

        for e in self.expenses:
            slot(e.tag)["planned" if e.planned else "spent"] += e.amount
        for s_ in self.sales:
            slot(s_.tag)["realised"] += s_.net
        for h in self.holdings:
            slot(h.tag)["held"] += self.net_if_sold(h.estimate or 0.0)

        for row in tags.values():
            row["net"] = round(row["realised"] + row["held"] - row["spent"], 2)
            for k in row:
                row[k] = round(row[k], 2)
        return dict(sorted(tags.items(), key=lambda kv: kv[1]["net"]))

    def unpriced(self) -> list[Holding]:
        return [h for h in self.holdings if h.estimate is None]

    def uncosted(self) -> list[Expense]:
        """Expenses still sitting at 0.00 — unfilled TODOs, not free purchases.

        These matter more than missing estimates: a missing cost inflates the
        position, so the report looks best exactly when it is least complete.
        """
        return [e for e in self.expenses if e.amount == 0 and not e.planned]

    def as_dict(self) -> dict[str, Any]:
        return {
            "spent": self.spent,
            "planned": self.planned,
            "committed": self.committed,
            "realised": self.realised,
            "unrealised_gross": self.unrealised_gross,
            "unrealised_net": self.unrealised_net,
            "position_gross": self.position_gross,
            "position_net": self.position_net,
            "roi_pct": self.roi_pct,
            "fee_pct": self.fee_pct,
            "by_category": self.by_category(),
            "by_tag": self.by_tag(),
            "card_prices_applied": self.card_prices_applied,
            "card_prices_at": self.card_prices_at,
            "holdings": [
                {
                    "id": h.id,
                    "name": h.name,
                    "status": h.status,
                    "estimate": h.estimate,
                    "net_if_sold": self.net_if_sold(h.estimate or 0.0) if h.estimate else None,
                    "source": h.source,
                    "note": h.note,
                    "qty": h.qty,
                    "estimate_source": h.estimate_source,
                    "estimate_manual": h.estimate_manual,
                    "tcgplayer_url": h.tcgplayer_url,
                    "scenarios": [
                        {
                            "label": str(s.get("label", "")),
                            "estimate": float(s["estimate"]),
                            "share": s.get("share"),
                            "note": str(s.get("note", "")),
                            "net_if_sold": self.net_if_sold(float(s["estimate"])),
                            "position_net": round(
                                self.position_net
                                - self.net_if_sold(h.estimate or 0.0)
                                + self.net_if_sold(float(s["estimate"])), 2),
                        }
                        for s in h.scenarios if s.get("estimate") is not None
                    ],
                }
                for h in sorted(self.holdings, key=lambda x: -(x.estimate or 0))
            ],
            "expenses": [vars(e) for e in self.expenses],
            "sales": [{**vars(s), "net": s.net} for s in self.sales],
        }


def load_ledger(path: Path, *, live: bool = True) -> Ledger:
    """Load the ledger, overlaying the last TCGplayer refresh by default.

    The overlay is applied here rather than by each caller so the terminal
    report, the dashboard and the issue workflow can never disagree about what
    a card is worth.
    """
    if not path.exists():
        raise FileNotFoundError(f"Ledger not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = doc.get("settings") or {}
    ledger = Ledger(
        expenses=[Expense(**_req(e, "expense", "date", "item", "amount"))
                  for e in (doc.get("expenses") or [])],
        holdings=[Holding(**_holding(h)) for h in (doc.get("holdings") or [])],
        sales=[Sale(**_sale(s)) for s in (doc.get("sales") or [])],
        fee_pct=float(settings.get("fee_pct", DEFAULT_FEE_PCT)),
        fee_flat=float(settings.get("fee_flat", DEFAULT_FEE_FLAT)),
    )
    if live:
        from .cardprices import apply_to
        from .config import DEFAULT_CARD_PRICES

        ledger.card_prices_applied = apply_to(ledger, DEFAULT_CARD_PRICES)
        ledger.card_prices_at = _card_prices_stamp(DEFAULT_CARD_PRICES)
    return ledger


def _card_prices_stamp(path: Path) -> str:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8")).get("updated_at", "")
    except (OSError, ValueError):
        return ""


def _req(raw: dict, kind: str, *required: str) -> dict:
    missing = [k for k in required if raw.get(k) is None]
    if missing:
        raise ValueError(f"{kind} entry missing {missing}: {raw}")
    return {
        "date": str(raw["date"]),
        "item": str(raw["item"]),
        "category": str(raw.get("category", "other")),
        "amount": float(raw["amount"]),
        "planned": bool(raw.get("planned", False)),
        "tag": str(raw.get("tag", "")),
    }


def _holding(raw: dict) -> dict:
    if not raw.get("name"):
        raise ValueError(f"holding missing name: {raw}")
    est = raw.get("estimate")
    return {
        "id": str(raw.get("id") or raw["name"]),
        "name": str(raw["name"]),
        "status": str(raw.get("status", "owned")),
        "estimate": float(est) if est is not None else None,
        "acquired": str(raw.get("acquired", "")),
        "source": str(raw.get("source", "")),
        "note": str(raw.get("note", "")),
        "ebay_query": str(raw.get("ebay_query", "")),
        "tag": str(raw.get("tag", "")),
        "tcgplayer_id": int(raw["tcgplayer_id"]) if raw.get("tcgplayer_id") else None,
        "qty": int(raw.get("qty", 1)),
        "scenarios": list(raw.get("scenarios") or []),
    }


def _sale(raw: dict) -> dict:
    for k in ("date", "item", "gross"):
        if raw.get(k) is None:
            raise ValueError(f"sale entry missing {k}: {raw}")
    return {
        "date": str(raw["date"]),
        "item": str(raw["item"]),
        "gross": float(raw["gross"]),
        "fees": float(raw.get("fees", 0.0)),
        "shipping_cost": float(raw.get("shipping_cost", 0.0)),
        "tag": str(raw.get("tag", "")),
    }


# -- terminal report --------------------------------------------------------


def _money(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def report(ledger: Ledger) -> None:
    w = 62
    print("=" * w)
    print("PORTFOLIO")
    print("=" * w)

    print(f"\n  Spent to date{'':<20}{_money(ledger.spent):>12}")
    for cat, amt in ledger.by_category().items():
        print(f"    {cat:<30}{_money(amt):>12}")

    if ledger.planned:
        print(f"\n  Planned (committed, not yet paid){'':<1}{_money(ledger.planned):>12}")
        for cat, amt in ledger.by_category(planned=True).items():
            print(f"    {cat:<30}{_money(amt):>12}")
        print(f"    {'COMMITTED TOTAL':<30}{_money(ledger.committed):>12}")

    print(f"\n  Realised (sales, net of fees){'':<4}{_money(ledger.realised):>12}")
    print(f"  Holdings at estimate{'':<13}{_money(ledger.unrealised_gross):>12}")
    print(f"  Holdings after selling fees{'':<6}{_money(ledger.unrealised_net):>12}"
          f"   ({ledger.fee_pct}% + ${ledger.fee_flat:.2f})")

    print("\n" + "-" * w)
    pos = ledger.position_net
    uncosted = ledger.uncosted()
    verdict = "UP" if pos > 0 else ("DOWN" if pos < 0 else "EVEN")
    print(f"  NET POSITION (if you sold everything today){_money(pos):>17}")
    if uncosted:
        print(f"  ** NOT TRUSTWORTHY — {len(uncosted)} cost(s) still at $0.00 **")
    else:
        print(f"  {verdict}"
              + (f" · ROI {ledger.roi_pct:+.1f}%" if ledger.roi_pct is not None else ""))
    print("-" * w)

    if uncosted:
        print("\n  Costs not yet entered (the position above is inflated until they are):")
        for e in uncosted:
            print(f"      {e.date}  {e.item}")

    live = "live" if ledger.card_prices_applied else ""
    stamp = f" · {ledger.card_prices_applied} priced live {ledger.card_prices_at[:16]}" \
        if ledger.card_prices_applied else ""
    print(f"\n  Holdings:{stamp}")
    for h in sorted(ledger.holdings, key=lambda x: -(x.estimate or 0)):
        est = _money(h.estimate) if h.estimate is not None else "unpriced"
        net = f"→ {_money(ledger.net_if_sold(h.estimate))} net" if h.estimate else ""
        src = "TCG" if h.estimate_source == "tcgplayer" else "   "
        print(f"    {h.name[:38]:<38} {est:>10} {net:<18} {src} [{h.status}]")
        # Booked at the floor, so say out loud what the other branches are
        # worth. Hiding them makes the floor look like a valuation.
        for s in h.scenarios:
            share = f"{s['share']*100:.1f}%" if s.get("share") is not None else ""
            print(f"      if {s.get('label',''):<16}{_money(float(s['estimate'])):>10}"
                  f"   {share:>6}")

    tags = ledger.by_tag()
    if len(tags) > 1:
        print(f"\n  Per tag:")
        print(f"    {'tag':<16}{'spent':>10}{'realised':>11}{'held':>10}{'net':>11}")
        for t, r in tags.items():
            print(f"    {t:<16}{_money(r['spent']):>10}{_money(r['realised']):>11}"
                  f"{_money(r['held']):>10}{_money(r['net']):>11}")

    if (missing := ledger.unpriced()):
        print(f"\n  ! {len(missing)} holding(s) have no estimate — the position is understated:")
        for h in missing:
            print(f"      {h.name}")

    print()
