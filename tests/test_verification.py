"""Verification tests.

These encode the safety properties the tracker is supposed to guarantee. Run
them after changing any threshold or phrase list:

    python -m pytest -q

The bias throughout is asymmetric on purpose. A test that lets a fake through
is a real failure; a test that rejects a genuine bargain is an acceptable cost.
"""

from __future__ import annotations

import pytest

from tracker.ebay import Detail, Listing
from tracker.filters import peer_median, screen_relevance
from tracker.__main__ import pick_winner
from tracker.trust import TrustPolicy, evaluate

POLICY = TrustPolicy(
    min_trust_score=70,
    min_seller_score=100,
    min_seller_pct=98.0,
    implausible_below=75.0,
    expect_terms=("op 17", "op17"),
)


def good_detail(**over) -> Detail:
    base = dict(
        returns_accepted=True,
        return_days=30,
        aspects={
            "Language": "English",
            "Configuration": "Booster Box",
            "Set": "OP-17 The World's Strongest Warriors",
            "Game": "One Piece Card Game",
        },
        quantity=3,
        location_country="US",
        category_path="Toys & Hobbies|Collectible Card Games|CCG Sealed Products",
        description_text="Brand new factory sealed booster box. Ships same day.",
    )
    base.update(over)
    return Detail(**base)


def listing(
    title="One Piece OP-17 Booster Box Factory Sealed English",
    price=119.99,
    *,
    ship=0.0,
    score=5000,
    pct=99.8,
    top=True,
    detail=None,
    seller="goodseller",
    country="US",
    programs=None,
) -> Listing:
    return Listing(
        item_id="v1|1|0",
        title=title,
        url="https://ebay.com/itm/1",
        price=price,
        shipping=ship,
        currency="USD",
        condition="New",
        seller_name=seller,
        seller_score=score,
        seller_pct=pct,
        top_rated=top,
        programs=programs or [],
        location_country=country,
        categories=["CCG Sealed Products"],
        detail=detail if detail is not None else good_detail(),
    )


# -- the listing we should be happy to buy ---------------------------------


def test_clean_listing_passes_with_high_score():
    r = evaluate(listing(), POLICY, median_price=120.0)
    assert r.passed, r.vetoes
    assert r.score >= 85, [(s.name, s.earned, s.detail) for s in r.signals]
    assert r.verified


# -- vetoes: each must disqualify on its own -------------------------------


@pytest.mark.parametrize(
    "kwargs,expect",
    [
        (dict(detail=good_detail(aspects={"Language": "Japanese"})), "Language: Japanese"),
        (dict(detail=good_detail(returns_accepted=False)), "does not accept returns"),
        (dict(detail=good_detail(location_country="JP")), "ships from JP"),
        (dict(price=60.0), "below the $75.00 floor"),
        (dict(score=12), "below the 100 minimum"),
        (dict(pct=94.0), "below the 98.0% minimum"),
        (dict(title="One Piece OP-17 Booster Box RESEALED"), "resealed"),
        (dict(title="One Piece OP-17 Custom Booster Box"), "custom"),
        (dict(title="One Piece OP-17 Booster Box replica"), "replica"),
        (
            dict(detail=good_detail(description_text="This is a bootleg reproduction")),
            "description contains",
        ),
        (dict(detail=Detail(fetch_error="HTTP 404")), "could not verify"),
    ],
)
def test_each_veto_disqualifies(kwargs, expect):
    r = evaluate(listing(**kwargs), POLICY, median_price=120.0)
    assert not r.passed, f"expected a veto for {kwargs}"
    assert any(expect in v for v in r.vetoes), r.vetoes


def test_blocked_seller_is_vetoed():
    policy = TrustPolicy(**{**POLICY.__dict__, "blocked_sellers": ("BadGuy",)})
    r = evaluate(listing(seller="badguy"), policy, median_price=120.0)
    assert not r.passed
    assert any("blocklist" in v for v in r.vetoes)


# -- scoring behaviour ------------------------------------------------------


def test_unverified_listing_scores_far_below_verified():
    verified = evaluate(listing(), POLICY, median_price=120.0)
    loose = TrustPolicy(**{**POLICY.__dict__, "require_verified_detail": False})
    unverified = evaluate(listing(detail=Detail(fetch_error="timeout")), loose, median_price=120.0)
    assert unverified.score < 50
    assert unverified.score < verified.score - 30


def test_missing_language_aspect_scores_low_but_does_not_veto():
    r = evaluate(listing(detail=good_detail(aspects={"Configuration": "Booster Box"})), POLICY, median_price=120.0)
    assert r.passed, r.vetoes
    lang = next(s for s in r.signals if s.name == "Language verified")
    assert lang.earned < lang.possible


def test_price_far_under_median_scores_zero_on_plausibility():
    r = evaluate(listing(price=80.0), POLICY, median_price=200.0)
    sig = next(s for s in r.signals if s.name == "Price plausibility")
    assert sig.earned == 0


def test_excess_stock_scores_zero():
    r = evaluate(listing(detail=good_detail(quantity=99)), POLICY, median_price=120.0)
    sig = next(s for s in r.signals if s.name == "Stock level")
    assert sig.earned == 0
    assert r.passed  # a signal, not a veto


def test_authenticity_programme_is_rewarded():
    """Base listing is deliberately NOT Top Rated.

    Top Rated is a bonus too, and an otherwise-ideal listing already scores 100
    with it, so comparing two capped scores would prove nothing.
    """
    with_prog = evaluate(
        listing(top=False, programs=["EBAY_AUTHENTICITY_GUARANTEE"]), POLICY, median_price=120.0
    )
    without = evaluate(listing(top=False), POLICY, median_price=120.0)
    assert with_prog.score > without.score


def test_bonus_signals_cannot_push_past_100():
    r = evaluate(
        listing(programs=["EBAY_AUTHENTICITY_GUARANTEE"]),
        TrustPolicy(**{**POLICY.__dict__, "trusted_sellers": ("goodseller",)}),
        median_price=120.0,
    )
    assert r.score == 100


# -- the property that actually protects the wallet ------------------------


def test_cheapest_fake_never_wins_over_pricier_genuine():
    """The core guarantee: price never overrides verification."""
    fake = listing(title="One Piece OP-17 Booster Box", price=42.0, score=4, pct=92.0,
                   top=False, detail=Detail(fetch_error="HTTP 404"), seller="newbie")
    genuine = listing(price=119.99)
    scored = [(l, evaluate(l, POLICY, median_price=120.0)) for l in (fake, genuine)]

    winner = pick_winner(scored, POLICY.min_trust_score)
    assert winner is not None
    assert winner[0].total == 119.99, "the cheap unverifiable listing must not win"


def test_no_winner_when_everything_fails_verification():
    bad = [
        listing(title="One Piece OP-17 Booster Box", price=40.0, detail=Detail(fetch_error="x")),
        listing(title="One Piece OP-17 Booster Box resealed", price=95.0),
    ]
    scored = [(l, evaluate(l, POLICY, median_price=120.0)) for l in bad]
    assert pick_winner(scored, POLICY.min_trust_score) is None


def test_low_score_but_no_veto_still_blocked_by_threshold():
    weak = listing(score=150, pct=98.1, top=False,
                   detail=good_detail(aspects={}, returns_accepted=None, quantity=None))
    r = evaluate(weak, POLICY, median_price=120.0)
    assert r.passed, "no hard veto expected here"
    assert r.score < 70
    assert pick_winner([(weak, r)], 70) is None


# -- stage 1 relevance ------------------------------------------------------


@pytest.mark.parametrize(
    "title,relevant",
    [
        ("One Piece OP-17 Booster Box Sealed", True),
        ("One Piece OP17 Booster Box English", True),
        ("One Piece OP-16 Time of Battle Booster Box", False),   # wrong set
        ("One Piece OP-17 Japanese Booster Box", False),
        ("One Piece OP-17 Booster Box EMPTY", False),
        ("One Piece OP-17 Booster Box Case of 12", False),
        ("One Piece OP-17 random break spot box", False),
        ("One Piece OP-17 single card lot", False),
    ],
)
def test_relevance_screening(title, relevant):
    out = screen_relevance(
        [listing(title=title)],
        require_all=["box"],
        require_any=["op 17", "op17"],
        exclude_any=["japanese", "empty", "case", "break", "spot", "random", "single", "lot"],
    )
    assert out[0].relevant is relevant, out[0].reasons


def test_peer_median_needs_a_real_sample():
    two = screen_relevance(
        [listing(price=100.0), listing(price=120.0)],
        require_all=["box"], require_any=["op 17", "op17"], exclude_any=[],
    )
    assert peer_median(two) is None

    three = screen_relevance(
        [listing(price=100.0), listing(price=120.0), listing(price=110.0)],
        require_all=["box"], require_any=["op 17", "op17"], exclude_any=[],
    )
    assert peer_median(three) == 110.0
