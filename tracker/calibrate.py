"""Calibration report.

The trust thresholds shipped in watchlist.yaml are estimates. This measures them
against real listings so they can be set from data instead.

Two failure modes it is designed to expose:

  Too strict -- nothing ever passes, the dashboard reads "none verified"
                forever, and it looks broken when it is only miscalibrated.
  Too loose  -- everything passes, the score is decorative, and the whole
                point of the exercise is lost.

Writes nothing and notifies nobody.
"""

from __future__ import annotations

import statistics
from collections import Counter

from .config import Product
from .ebay import EbayClient
from .filters import peer_median, screen_relevance
from .trust import TrustReport, evaluate

THRESHOLDS = (55, 60, 65, 70, 75, 80, 85, 90)


def _bar(n: int, scale: int = 1) -> str:
    return "█" * max(1, n * scale) if n else ""


def calibrate(client: EbayClient, products: list[Product], policy_for) -> None:
    for product in products:
        print(f"\n{'=' * 72}\n{product.name}\n{'=' * 72}")

        try:
            raw = client.search(
                product.query, min_price=product.min_price, max_price=product.max_price
            )
        except Exception as exc:  # noqa: BLE001 - report and continue to next product
            print(f"  search failed: {exc}")
            continue

        candidates = screen_relevance(
            raw,
            require_all=product.require_all,
            require_any=product.require_any,
            exclude_any=product.exclude_any,
        )
        relevant = [c for c in candidates if c.relevant]
        median = peer_median(candidates)
        policy = policy_for(product)

        print(f"  found {len(raw)} · relevant {len(relevant)} · verifying {min(len(relevant), product.verify_top_n)}")

        if not relevant:
            print("\n  Nothing passed title screening. Your filters are too tight.")
            _show_rejection_reasons(candidates)
            continue

        shortlist = sorted((c.listing for c in relevant), key=lambda l: l.total)[
            : product.verify_top_n
        ]
        reports: list[TrustReport] = []
        for item in shortlist:
            item.detail = client.get_detail(item.item_id)
            reports.append(evaluate(item, policy, median_price=median))

        _show_scores(reports)
        _show_signals(reports)
        _show_vetoes(reports)
        _show_prices(shortlist, median)
        _show_suggestions(product, reports, median)


def _show_rejection_reasons(candidates) -> None:
    reasons = Counter()
    for c in candidates:
        for r in c.reasons:
            reasons[r.split(":")[0]] += 1
    print("\n  Why listings were screened out:")
    for reason, n in reasons.most_common(6):
        print(f"    {n:>3}  {reason}")


def _show_scores(reports: list[TrustReport]) -> None:
    print("\n  Trust score distribution:")
    buckets = Counter()
    for r in reports:
        buckets[min(r.score // 10 * 10, 90)] += 1
    for lo in range(0, 100, 10):
        n = buckets.get(lo, 0)
        if n:
            print(f"    {lo:>3}-{lo+9:<3} {_bar(n, 2):<20} {n}")


def _show_signals(reports: list[TrustReport]) -> None:
    """Which signals are dead weight on real listings?"""
    total = len(reports)
    empty = Counter()
    partial = Counter()
    for r in reports:
        for s in r.signals:
            if s.possible == 0:
                continue
            if s.earned == 0:
                empty[s.name] += 1
            elif s.earned < s.possible:
                partial[s.name] += 1

    print("\n  Signals scoring ZERO (these are dragging every score down):")
    for name, n in empty.most_common():
        flag = "  <-- consider reweighting" if n == total and total > 1 else ""
        print(f"    {n}/{total}  {name}{flag}")
    if partial:
        print("\n  Signals scoring partial:")
        for name, n in partial.most_common():
            print(f"    {n}/{total}  {name}")


def _show_vetoes(reports: list[TrustReport]) -> None:
    vetoed = [r for r in reports if not r.passed]
    if not vetoed:
        print("\n  No listings were vetoed.")
        return
    print(f"\n  Vetoed {len(vetoed)}/{len(reports)}:")
    reasons = Counter()
    for r in vetoed:
        for v in r.vetoes:
            reasons[v.split("—")[0].split("(")[0].strip()[:60]] += 1
    for reason, n in reasons.most_common():
        print(f"    {n:>3}  {reason}")


def _show_prices(shortlist, median: float | None) -> None:
    prices = [l.total for l in shortlist]
    if not prices:
        return
    print(
        f"\n  Delivered price: min ${min(prices):.2f} · "
        f"median ${statistics.median(prices):.2f} · max ${max(prices):.2f}"
        + (f" · peer median ${median:.2f}" if median else "")
    )


def _show_suggestions(product: Product, reports: list[TrustReport], median: float | None) -> None:
    passing = [r for r in reports if r.passed]
    print(f"\n  How many listings pass at each threshold (of {len(passing)} un-vetoed):")
    for t in THRESHOLDS:
        n = sum(1 for r in passing if r.score >= t)
        marker = "  <-- your current setting" if t == product.min_trust_score else ""
        print(f"    min_trust_score: {t:>3}  ->  {n} pass{marker}")

    print("\n  Suggested watchlist.yaml values:")
    workable = [t for t in THRESHOLDS if sum(1 for r in passing if r.score >= t) >= 2]
    if workable:
        best = max(workable)
        print(f"    min_trust_score: {best}    # strictest setting that still leaves 2+ options")
    else:
        print("    min_trust_score: lower it — fewer than 2 listings pass at any threshold")

    if median:
        print(f"    implausible_below: {median * 0.6:.0f}   # 60% of the ${median:.2f} peer median")
