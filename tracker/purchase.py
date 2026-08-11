"""Purchase scoring: is this listing actually worth buying?

Trust answers "is this real?". This answers "is this a good buy?" -- a
different question with a different failure mode. A perfectly genuine box at a
terrible price scores 100 on trust and should still be a no.

The score is built to keep hard data and speculation separate:

  COST EFFICIENCY (45)  Cost per pack against the cheapest verified alternative
                        for the same set. Purely arithmetic. Packs and boxes
                        contain identical cards at identical odds, so the only
                        thing that differs is what you pay per pack.

  TRUST (25)            Carried over from trust.py. You cannot buy what you
                        cannot verify.

  EV COVERAGE (30)      Estimated singles value per pack against cost per pack,
                        from configured pull rates. This is the speculative
                        part, so it is weighted least and only counts when you
                        have actually configured an estimate for that set.

Deliberately weighted so that cost efficiency dominates: it is the only
component that cannot be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PurchaseVerdict:
    score: int
    cost_per_pack: float
    baseline_cost_per_pack: float | None
    ev_per_pack: float | None
    verdict: str
    notes: list[str]
    dud_chance: float | None = None   # P(this unit contains no SP at all)

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "dud_chance": round(self.dud_chance, 3) if self.dud_chance is not None else None,
            "cost_per_pack": round(self.cost_per_pack, 2),
            "baseline_cost_per_pack": (
                round(self.baseline_cost_per_pack, 2) if self.baseline_cost_per_pack else None
            ),
            "ev_per_pack": round(self.ev_per_pack, 2) if self.ev_per_pack else None,
            "verdict": self.verdict,
            "notes": self.notes,
        }


def label_for(score: int) -> str:
    if score >= 80:
        return "STRONG BUY"
    if score >= 65:
        return "GOOD"
    if score >= 50:
        return "FAIR"
    if score >= 35:
        return "POOR"
    return "AVOID"


def evaluate_purchase(
    *,
    total_price: float,
    packs_in_unit: int,
    trust_score: int,
    baseline_cost_per_pack: float | None,
    ev_per_pack: float | None,
    unit_kind: str = "box",
    sp_per_box: float | None = None,
    packs_per_box: int = 24,
) -> PurchaseVerdict:
    notes: list[str] = []
    cpp = total_price / max(packs_in_unit, 1)

    earned = 0.0
    possible = 0.0

    # -- cost efficiency (55) ---------------------------------------------
    possible += 45
    if baseline_cost_per_pack and baseline_cost_per_pack > 0:
        ratio = baseline_cost_per_pack / cpp          # >1 means cheaper than market
        # Sitting AT the market is average, not excellent: 0.7x market -> 0,
        # market -> half marks, 1.3x cheaper than market -> full marks.
        earned += 45 * max(0.0, min((ratio - 0.7) / 0.6, 1.0))
        delta = (cpp / baseline_cost_per_pack - 1) * 100
        if delta > 5:
            notes.append(
                f"${cpp:.2f}/pack is {delta:.0f}% above the ${baseline_cost_per_pack:.2f}/pack "
                f"baseline for this set"
            )
        elif delta < -5:
            notes.append(
                f"${cpp:.2f}/pack is {abs(delta):.0f}% below the "
                f"${baseline_cost_per_pack:.2f}/pack baseline"
            )
        else:
            notes.append(f"${cpp:.2f}/pack, in line with the market")
    else:
        earned += 45 * 0.5      # no baseline to judge against -- neither reward nor punish
        notes.append(f"${cpp:.2f}/pack, no verified alternative to compare against")

    # -- trust (30) --------------------------------------------------------
    possible += 25
    earned += 25 * (trust_score / 100)

    # -- EV coverage (15), only when configured ----------------------------
    if ev_per_pack and ev_per_pack > 0:
        possible += 30
        coverage = ev_per_pack / cpp
        earned += 30 * min(coverage, 1.0)
        notes.append(
            f"estimated ${ev_per_pack:.2f}/pack in singles vs ${cpp:.2f} paid "
            f"({coverage*100:.0f}% cover) — estimate, not a measurement"
        )
        if coverage < 0.75:
            notes.append(
                f"Opening this is negative EV: you would expect back about "
                f"{coverage*100:.0f} cents of singles per dollar spent. Buy it to open "
                f"for fun, not to profit."
            )

    # -- dud chance ---------------------------------------------------------
    # The average EV above hides the shape of the distribution. At ~1 SP per
    # 12-box case, the overwhelming majority of boxes contain no SP at all --
    # a handful carry the entire expected value. Knowing the modal outcome is
    # "nothing" matters more than knowing the mean.
    dud = None
    if sp_per_box and sp_per_box > 0:
        per_unit_sp = sp_per_box if unit_kind != "pack" else sp_per_box / max(packs_per_box, 1)
        dud = max(0.0, min(1.0, 1.0 - per_unit_sp))
        notes.append(
            f"~{dud*100:.0f}% chance this {unit_kind} contains no SP at all — "
            f"the average payout is carried by the rare {(1-dud)*100:.0f}%"
        )

    score = int(round(100 * earned / possible)) if possible else 0

    if unit_kind == "pack":
        notes.append(
            "Loose packs almost always cost more per pack than a box of the same set. "
            "Compare against the box before buying."
        )

    return PurchaseVerdict(
        score=score,
        dud_chance=dud,
        cost_per_pack=cpp,
        baseline_cost_per_pack=baseline_cost_per_pack,
        ev_per_pack=ev_per_pack,
        verdict=label_for(score),
        notes=notes,
    )


def estimate_ev_per_pack(
    *,
    packs_per_box: int,
    sp_per_box: float,
    sp_avg_value: float,
    sr_per_box: float,
    sr_avg_value: float,
    base_value_per_box: float,
) -> float:
    """Rough expected singles value per pack from configured pull rates.

    Every input is a community estimate -- Bandai publishes no pull rates -- so
    treat the output as an order of magnitude, not a number. It exists so the
    watchlist can hold your assumptions explicitly instead of leaving them
    implicit in a gut feeling.
    """
    per_box = sp_per_box * sp_avg_value + sr_per_box * sr_avg_value + base_value_per_box
    return per_box / max(packs_per_box, 1)
