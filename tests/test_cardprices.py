"""Offline checks on the card-value overlay.

No network. These guard the two ways an auto-refresh can quietly lie: pricing
the wrong printing, and letting a failed quote look like a real number.
"""

from __future__ import annotations

import json

from tracker import cardprices
from tracker.portfolio import Holding, Ledger


def q(**kw) -> cardprices.Quote:
    return cardprices.Quote(kw.pop("pid", 1), **kw)


def test_foil_wins_over_normal():
    # Nearly every hit in this collection is a foil printing, and foil and
    # normal are different products at different prices.
    assert q(foil_market=341.25, normal_market=1.29).price == 341.25
    assert q(foil_market=341.25).printing == "foil"


def test_normal_is_the_fallback_not_the_default():
    # The standard P-110 promo has no foil printing at all.
    only_normal = q(normal_market=16.67)
    assert only_normal.price == 16.67
    assert only_normal.printing == "normal"


def test_no_market_price_is_not_zero():
    # A presale card that has never sold must stay unpriced. Treating it as
    # $0.00 would silently delete a holding from the position.
    unsold = q()
    assert unsold.price is None
    assert unsold.as_dict()["price"] is None


def _ledger(estimate, qty=1, pid=999):
    return Ledger(holdings=[Holding(id="h", name="card", status="owned",
                                    estimate=estimate, tcgplayer_id=pid, qty=qty)])


def test_quantity_multiplies_the_unit_price(monkeypatch):
    monkeypatch.setattr(cardprices, "quote", lambda pid, s=None: q(foil_market=4.43))
    result = cardprices.refresh(_ledger(23.22, qty=3), verbose=False)
    assert result.cards["h"]["value"] == 13.29     # 4.43 x 3, not 4.43
    assert result.applied == 1


def test_wild_swing_on_an_expensive_card_is_rejected(monkeypatch):
    # A 10x move on a $340 card is a mis-pinned product id, not the market.
    monkeypatch.setattr(cardprices, "quote", lambda pid, s=None: q(foil_market=3400.0))
    result = cardprices.refresh(_ledger(341.25), verbose=False)
    assert result.applied == 0 and result.rejected


def test_wild_swing_on_a_cheap_card_is_allowed(monkeypatch):
    # $6.72 -> $1.65 is 4.07x and also exactly what a $7 single does when a set
    # leaves presale. The ratio alone would veto it; the dollar floor saves it.
    monkeypatch.setattr(cardprices, "quote", lambda pid, s=None: q(foil_market=1.65))
    result = cardprices.refresh(_ledger(6.72), verbose=False)
    assert result.applied == 1 and not result.rejected


def test_overlay_only_applies_what_was_applied(tmp_path):
    led = _ledger(341.25)
    f = tmp_path / "cardprices.json"
    f.write_text(json.dumps({"cards": {"h": {"value": 3400.0, "applied": False}}}))
    assert cardprices.apply_to(led, f) == 0
    assert led.holdings[0].estimate == 341.25       # untouched


def test_overlay_records_where_the_number_came_from(tmp_path):
    led = _ledger(285.00)
    f = tmp_path / "cardprices.json"
    f.write_text(json.dumps({"cards": {"h": {"value": 213.39, "applied": True,
                                             "url": "https://tcg/1"}}}))
    assert cardprices.apply_to(led, f) == 1
    h = led.holdings[0]
    assert (h.estimate, h.estimate_manual, h.estimate_source) == (213.39, 285.00, "tcgplayer")


def test_missing_overlay_file_keeps_the_manual_estimate(tmp_path):
    # A failed refresh must never blank the position out.
    led = _ledger(285.00)
    assert cardprices.apply_to(led, tmp_path / "nope.json") == 0
    assert led.holdings[0].estimate == 285.00
