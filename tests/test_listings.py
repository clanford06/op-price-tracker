"""Checks on the listing generator.

Two failure modes matter: pricing that quietly loses money, and titles that
describe the wrong card.
"""

from __future__ import annotations

from tracker.listings import _parse, _title, build, price_for
from tracker.portfolio import Holding, Ledger

FEE_PCT, FEE_FLAT = 13.25, 0.40


def test_the_ask_actually_nets_market():
    # The whole promise of the ask is that eBay's cut comes out and you are
    # left holding TCGplayer market. If that is off, every number is off.
    for market in (10.0, 48.29, 216.78):
        ask, floor = price_for(market, FEE_PCT, FEE_FLAT)
        net = ask - (ask * FEE_PCT / 100 + FEE_FLAT)
        assert abs(net - market) < 0.01
        net_floor = floor - (floor * FEE_PCT / 100 + FEE_FLAT)
        assert abs(net_floor - market * 0.85) < 0.01


def _led(*holdings):
    return Ledger(holdings=list(holdings), fee_pct=FEE_PCT, fee_flat=FEE_FLAT)


def h(**kw):
    kw.setdefault("status", "owned")
    kw.setdefault("tag", "op17")
    return Holding(id=kw.pop("id", "x"), name=kw.pop("name", "OP17-062 Kaido (062)"), **kw)


def test_cheap_cards_go_to_bulk_not_their_own_listing():
    # A $2 card nets $1.33 before an envelope. Listing it loses money.
    cheap = h(id="c", name="OP17-048 Shiki (048)", estimate=2.73, qty=3)
    dear = h(id="d", estimate=216.78)
    drafts, bulk, _ = _led(cheap, dear).holdings and build(
        _led(cheap, dear), tags={"op17"}, single_floor=10.0)
    assert [d.holding_id for d in drafts] == ["d"]
    assert [b[0].id for b in bulk] == ["c"]


def test_copies_already_in_a_live_listing_are_not_offered_twice():
    two = h(id="s", name="OP17-020 Shanks (020) (Alternate Art)",
            estimate=96.58, qty=2, tcgplayer_id=705923)
    drafts, _, _ = build(_led(two), tags={"op17"}, committed={705923: 1})
    assert drafts[0].qty == 1                 # one is in the leader lot
    assert drafts[0].market == 48.29          # priced per copy, not per line


def test_a_card_at_psa_is_never_listed():
    slab = h(id="m", name="Ms. All Sunday SP OP14-084", estimate=500.0, status="grading")
    drafts, bulk, blocked = build(_led(slab), tags={"op17"})
    assert not drafts and not bulk
    assert "PSA" in blocked[0][1]


def test_the_two_don_cards_get_different_titles():
    # Both are "DON!! Card (World United)"; only the second parenthetical tells
    # them apart, and dropping it made both titles identical.
    luffy = _title(*_parse("DON!! Card (World United) (Luffy)"), "DON!!")
    zoro = _title(*_parse("DON!! Card (World United) (Zoro)"), "DON!!")
    assert luffy != zoro and "Luffy" in luffy and "Zoro" in zoro


def test_an_sp_reprint_does_not_repeat_its_card_number():
    num, base, variant = _parse("ST27-005 Marshall.D.Teach (ST27-005) (SP)")
    assert num == "ST27-005" and variant == "SP"
    assert _title(num, base, variant, "Super Rare").count("ST27-005") == 1


def test_a_slab_leads_with_its_grade():
    title = _title(*_parse('EB04-039 PSA 10 Eustass "Captain" Kidd SP'), "Rare")
    assert title.startswith("PSA 10")
    assert '"' not in title          # quotes break eBay title fields
    assert "NM" not in title         # a graded card is not described as NM


def test_titles_stay_within_ebays_limit():
    long = _title("OP17-114", "Sweet 3 Generals and a Very Long Character Name Here",
                  "Super Alternate Art Parallel", "Secret Rare")
    assert len(long) <= 80
