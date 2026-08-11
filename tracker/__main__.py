"""Entry point: check every watchlist product, store history, push alerts.

Pipeline per product:

  1. search        eBay item_summary/search, cheapest first
  2. relevance     title screening -- is this even the right product?
  3. peer median   from relevant listings, for price-plausibility scoring
  4. verify        fetch item detail for the N cheapest relevant listings
  5. trust         veto checks + weighted score on each verified listing
  6. pick          cheapest listing with no vetoes AND score >= threshold

Detail fetches are limited to the cheapest N because each costs an API call,
and the cheapest listings are both the ones you would buy and the ones most
likely to be fake.

    python -m tracker              # normal run
    python -m tracker --dry-run    # search + print, write nothing, notify nobody
    python -m tracker --explain    # show every screening and scoring decision
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import storage
from .config import DEFAULT_DATA_FILE, DEFAULT_WATCHLIST, Product, load_products, load_settings
from .ebay import EbayClient, EbayError, Listing
from .filters import Candidate, peer_median, screen_relevance
from .notify import Notifier
from .purchase import evaluate_purchase
from .trust import TrustPolicy, TrustReport, evaluate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tracker", description="Track verified English One Piece sealed box prices."
    )
    p.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    p.add_argument("--data-file", type=Path, default=DEFAULT_DATA_FILE)
    p.add_argument("--dry-run", action="store_true", help="Do not write history or send alerts.")
    p.add_argument("--explain", action="store_true", help="Print every screening decision.")
    p.add_argument("--only", help="Check a single product id.")
    p.add_argument(
        "--calibrate",
        action="store_true",
        help="Measure thresholds against real listings and suggest values. Writes nothing.",
    )
    p.add_argument(
        "--portfolio",
        action="store_true",
        help="Show expenses, holdings and profit/loss from ledger.yaml.",
    )
    p.add_argument("--ledger", type=Path, default=None)
    p.add_argument("add", nargs="*", default=[], metavar="...",
                   help='add expense|holding|sale "description" AMOUNT')
    p.add_argument("--tag", "-t", default="", help="grouping label, e.g. op17")
    p.add_argument("--category", "-c", default="other", help="expense category")
    p.add_argument("--date", dest="when", default=None, help="YYYY-MM-DD (default today)")
    p.add_argument("--planned", action="store_true", help="committed but not yet paid")
    p.add_argument("--fees", type=float, default=0.0, help="sale: fees charged")
    p.add_argument("--ship", type=float, default=0.0, help="sale: your shipping cost")
    p.add_argument("--status", default="owned", help="holding: owned|grading|listed")
    p.add_argument(
        "--chase",
        action="store_true",
        help="Refresh top chase cards per set from Limitless. Slow; run nightly.",
    )
    p.add_argument(
        "--selftest",
        action="store_true",
        help="Verify the live eBay response shape matches the parsers. Run this first.",
    )
    return p.parse_args(argv)


def policy_for(product: Product) -> TrustPolicy:
    return TrustPolicy(
        min_trust_score=product.min_trust_score,
        min_seller_score=product.min_seller_score,
        min_seller_pct=product.min_seller_pct,
        implausible_below=product.implausible_below,
        require_returns=product.require_returns,
        require_us_location=product.require_us_location,
        require_verified_detail=product.require_verified_detail,
        max_quantity=product.max_quantity,
        blocked_sellers=tuple(product.blocked_sellers),
        trusted_sellers=tuple(product.trusted_sellers),
        expect_terms=tuple(product.require_any),
        exclude_terms=tuple(product.exclude_any),
    )


def baseline_cpp(candidates, product) -> float | None:
    """Market cost-per-pack for this set: the peer MEDIAN, not the minimum.

    Using the cheapest passing listing was circular -- the winner is by
    definition the cheapest passing listing, so every winner compared itself
    against itself and scored full marks. The median is an independent
    reference the listing can actually be better or worse than.
    """
    med = peer_median(candidates)
    return (med / max(product.packs_in_unit, 1)) if med else None


def listing_dict(item: Listing, report: TrustReport | None = None) -> dict:
    d = {
        "title": item.title,
        "url": item.url,
        "price": item.price,
        "shipping": item.shipping,
        "total": item.total,
        "seller": item.seller_name,
        "seller_score": item.seller_score,
        "seller_pct": item.seller_pct,
        "top_rated": item.top_rated,
    }
    if report:
        d["trust"] = report.as_dict()
    return d


def verify_candidates(
    client: EbayClient,
    product: Product,
    candidates: list[Candidate],
    *,
    explain: bool,
) -> list[tuple[Listing, TrustReport]]:
    """Fetch detail for the cheapest relevant listings and score each."""
    relevant = sorted(
        (c.listing for c in candidates if c.relevant), key=lambda l: l.total
    )[: product.verify_top_n]
    median = peer_median(candidates)
    policy = policy_for(product)

    scored: list[tuple[Listing, TrustReport]] = []
    for item in relevant:
        item.detail = client.get_detail(item.item_id)
        report = evaluate(item, policy, median_price=median)
        scored.append((item, report))

        if explain:
            state = (
                "VETOED"
                if report.vetoes
                else ("PASS" if report.score >= product.min_trust_score else "LOW")
            )
            print(f"    [{state:6}] ${item.total:>8.2f}  score {report.score:>3}  {item.title[:52]}")
            for v in report.vetoes:
                print(f"             veto: {v}")
            for s in report.signals:
                if not s.good:
                    print(f"             -{s.name}: {s.detail} ({s.earned:.0f}/{s.possible})")
    return scored


def pick_winner(
    scored: list[tuple[Listing, TrustReport]], min_score: int
) -> tuple[Listing, TrustReport] | None:
    """Cheapest listing that passed every veto and cleared the score bar."""
    ok = [(l, r) for l, r in scored if r.passed and r.score >= min_score]
    return min(ok, key=lambda pair: pair[0].total) if ok else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()

    if args.add and args.add[0] == "add":
        from .config import DEFAULT_LEDGER
        from .ledger_edit import LedgerEditError, add_entry
        from .portfolio import load_ledger, report

        rest = args.add[1:]
        if len(rest) < 3:
            print('usage: python -m tracker add expense|holding|sale "description" AMOUNT '
                  '[--tag X] [--category Y] [--planned]', file=sys.stderr)
            return 2
        kind, desc, amount = rest[0], " ".join(rest[1:-1]), rest[-1]
        try:
            msg = add_entry(
                args.ledger or DEFAULT_LEDGER, kind, desc, float(amount),
                tag=args.tag, category=args.category, when=args.when,
                planned=args.planned, fees=args.fees, shipping_cost=args.ship,
                status=args.status,
            )
        except (LedgerEditError, ValueError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
        print(msg + "\n")
        report(load_ledger(args.ledger or DEFAULT_LEDGER))
        return 0

    if args.portfolio:
        from .config import DEFAULT_LEDGER
        from .portfolio import load_ledger, report

        try:
            report(load_ledger(args.ledger or DEFAULT_LEDGER))
        except (FileNotFoundError, ValueError) as exc:
            print(f"Ledger error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.chase:
        import json
        from .chase import top_chase
        from .config import DEFAULT_CHASE_FILE
        from .storage import utc_now_iso

        out = {"updated_at": utc_now_iso(), "sets": {}}
        for product in load_products(args.watchlist):
            if product.unit_kind == "pack":
                continue                      # packs share their set's chase list
            code = product.limitless_set or product.id.upper()
            cards = top_chase(code)
            out["sets"][product.id] = {"set": code, "name": product.name, "cards": cards}
            top = f"${cards[0]['price']:,.2f} {cards[0]['name']}" if cards else "nothing found"
            print(f"  {product.id:<8} {len(cards)} cards · top {top}")

        DEFAULT_CHASE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_CHASE_FILE.write_text(json.dumps(out, indent=2) + "\n")
        print(f"\nWrote {DEFAULT_CHASE_FILE}")
        return 0

    if args.selftest:
        from .selftest import run

        return run(settings)

    try:
        products = load_products(args.watchlist)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.only:
        products = [p for p in products if p.id == args.only]
        if not products:
            print(f"No product with id {args.only!r}", file=sys.stderr)
            return 2

    try:
        client = EbayClient(settings.ebay_client_id, settings.ebay_client_secret)
    except EbayError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    if args.calibrate:
        from .calibrate import calibrate

        calibrate(client, products, policy_for)
        return 0

    # Chase prices come from the nightly job; a missing file just means the
    # profit outlook is omitted, never that the price run fails.
    chase_by_set: dict = {}
    try:
        import json as _json
        from .config import DEFAULT_CHASE_FILE

        if DEFAULT_CHASE_FILE.exists():
            chase_by_set = (_json.loads(DEFAULT_CHASE_FILE.read_text()).get("sets") or {})
    except Exception as exc:  # noqa: BLE001
        print(f"  (chase data unavailable: {exc})")

    notifier = Notifier(settings.ntfy_server, settings.ntfy_topic, enabled=not args.dry_run)
    data = storage.load(args.data_file)
    alerts: list[str] = []
    failures = 0

    for product in products:
        print(f"- {product.name}")
        try:
            raw = client.search(
                product.query, min_price=product.min_price, max_price=product.max_price
            )
            error = None
        except EbayError as exc:
            print(f"  ! {exc}")
            raw, error = [], str(exc)
            failures += 1

        candidates = screen_relevance(
            raw,
            require_all=product.require_all,
            require_any=product.require_any,
            exclude_any=product.exclude_any,
        )
        relevant_n = sum(1 for c in candidates if c.relevant)

        scored = (
            verify_candidates(client, product, candidates, explain=args.explain)
            if relevant_n
            else []
        )
        winner = pick_winner(scored, product.min_trust_score)
        rejected = [(l, r) for l, r in scored if not (r.passed and r.score >= product.min_trust_score)]

        buy = None
        if winner:
            item, report = winner
            buy = evaluate_purchase(
                total_price=item.total,
                packs_in_unit=product.packs_in_unit,
                trust_score=report.score,
                baseline_cost_per_pack=baseline_cpp(candidates, product),
                ev_per_pack=product.ev_per_pack,
                unit_kind=product.unit_kind,
                sp_per_box=product.sp_per_box,
                chase_cards=(chase_by_set.get(product.chase_from or product.id) or {}).get("cards"),
                chase_hit_rate=product.chase_hit_rate,
                packs_per_box=product.packs_in_unit if product.unit_kind == 'box' else 24,
            )
            print(
                f"  ${item.total:.2f} verified (trust {report.score}/100) — "
                f"{item.seller_name} [{item.seller_score:,} @ {item.seller_pct}%]"
            )
            print(
                f"  BUY SCORE {buy.score}/100 [{buy.verdict}] · "
                f"${buy.cost_per_pack:.2f}/pack"
            )
            for n in buy.notes:
                print(f"    - {n}")
            if buy.profit and buy.profit.profit_chance is not None:
                print(f"    - modelled profit chance {buy.profit.profit_chance*100:.0f}%")
        else:
            print(
                f"  no listing passed verification "
                f"({len(raw)} found, {relevant_n} relevant, {len(scored)} checked)"
            )

        prior_low = storage.previous_low(data, product.id)
        price = winner[0].total if winner else None

        if not args.dry_run:
            storage.record(
                data,
                product_id=product.id,
                name=product.name,
                price=price,
                listing=(
                    {**listing_dict(*winner), "purchase": buy.as_dict()} if winner else None
                ),
                considered=len(raw),
                relevant=relevant_n,
                verified=len(scored),
                kept=1 if winner else 0,
                rejected=[
                    {
                        **listing_dict(l, r),
                        "why": r.vetoes or [f"trust score {r.score} below {product.min_trust_score}"],
                    }
                    for l, r in rejected
                ],
                min_trust_score=product.min_trust_score,
                error=error,
            )

        if winner and price is not None:
            item, report = winner
            below_target = product.alert_below is not None and price <= product.alert_below
            new_low = settings.alert_on_new_low and prior_low is not None and price < prior_low
            if below_target or new_low:
                why = "below your target" if below_target else "a new all-time low"
                notifier.send(
                    title=f"{product.name} — ${price:.2f}",
                    message=(
                        f"${price:.2f} delivered is {why}.\n"
                        f"Trust {report.score}/100 · BUY {buy.score}/100 ({buy.verdict})\n"
                        f"${buy.cost_per_pack:.2f} per pack\n"
                        f"Seller {item.seller_name} ({item.seller_score:,} sales, {item.seller_pct}%)\n"
                        f"{item.title[:110]}"
                    ),
                    priority="high" if below_target else "default",
                    tags=["moneybag"],
                    click_url=item.url,
                )
                alerts.append(f"{product.name} ${price:.2f}")

    if not args.dry_run:
        # Fold the ledger into the same file the dashboard reads, so expenses
        # and profit render alongside prices instead of living in a separate tool.
        try:
            from .config import DEFAULT_LEDGER
            from .portfolio import load_ledger

            data["portfolio"] = load_ledger(args.ledger or DEFAULT_LEDGER).as_dict()
        except (FileNotFoundError, ValueError) as exc:
            print(f"  (portfolio skipped: {exc})")
            data["portfolio"] = None

        storage.save(args.data_file, data)
        print(f"\nWrote {args.data_file}")
    if alerts:
        print(f"Alerts sent: {', '.join(alerts)}")

    if failures and failures == len(products):
        print("All products failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
