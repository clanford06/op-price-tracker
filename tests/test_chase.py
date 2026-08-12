"""Chase-card scraping tests.

Built around the bug that shipped: a card page's prints table lists every
printing across ALL sets, so Prize Cards and cross-set reprints were being
reported as chases you could pull from a box. OP12-015's real Legacy of the
Master alternate art is $24.55; the prize-card printing is $1,866.67, and that
is the number that reached the dashboard.

The fixture is the real page, saved verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracker.chase import DISAGREE_RATIO, card_printings

FIXTURE = Path(__file__).parent / "fixtures" / "OP12-015.html"


class FakeSession:
    """Serves the fixture regardless of URL."""

    def __init__(self, body: str, status: int = 200):
        self.body = body
        self.status = status

    def get(self, *_a, **_k):
        class R:
            status_code = self.status
            text = self.body

        return R()


@pytest.fixture
def page() -> str:
    return FIXTURE.read_text(encoding="utf-8", errors="replace")


def test_only_the_target_sets_printings_are_returned(page):
    """The bug, pinned. Prize Cards must not become an OP-12 chase."""
    got = card_printings(
        FakeSession(page), "OP12-015", "Monkey.D.Luffy", "Super Rare",
        "Legacy of the Master",
    )
    assert got, "expected at least the Legacy of the Master printing"
    prices = [p.price for p in got]
    assert max(prices) < 100, (
        f"a printing from another set leaked in: {prices}. The prize-card "
        f"printing is $1,866.67 and must be excluded."
    )


def test_without_a_set_name_everything_is_returned(page):
    """Guards the guard: the filter must actually be what excludes them."""
    unfiltered = card_printings(
        FakeSession(page), "OP12-015", "Monkey.D.Luffy", "Super Rare", ""
    )
    filtered = card_printings(
        FakeSession(page), "OP12-015", "Monkey.D.Luffy", "Super Rare",
        "Legacy of the Master",
    )
    assert len(unfiltered) > len(filtered), (
        "if these match, the set filter is not doing anything and the test "
        "above would pass for the wrong reason"
    )


def test_set_name_match_is_case_insensitive(page):
    got = card_printings(
        FakeSession(page), "OP12-015", "Monkey.D.Luffy", "Super Rare",
        "LEGACY OF THE MASTER",
    )
    assert got


def test_unreachable_page_yields_nothing_rather_than_raising():
    assert card_printings(FakeSession("", 404), "OP12-015", "x", "y", "z") == []


@pytest.mark.parametrize(
    "a,b,agree",
    [
        (810.52, 858.64, True),    # normal cross-source spread
        (1866.67, 24.55, False),   # the shipped bug, as two sources would see it
        (100.0, 60.0, True),       # 0.6 -- inside tolerance
        (100.0, 40.0, False),      # 0.4 -- outside
    ],
)
def test_disagreement_threshold(a, b, agree):
    hi, lo = max(a, b), min(a, b)
    assert ((lo / hi) >= DISAGREE_RATIO) is agree


# -- second-source comparison ----------------------------------------------

from tracker.chase import _same_printing  # noqa: E402


@pytest.mark.parametrize(
    "variant,stacked_label,same",
    [
        # The real pairs that exposed the flawed comparison.
        ("manga rare", "jewelry bonney 118 manga", True),
        ("manga rare", "shanks op01 120 reprint", False),
        ("manga rare", "roronoa zoro op06 118 reprint", False),
        ("alt art", "monkey d luffy 022 alternate art", True),
        ("base", "roronoa zoro op06 118 reprint", True),
        # No label at all must never count as a match.
        ("manga rare", "", False),
        # An unfamiliar label is not comparable either.
        ("manga rare", "some unfamiliar printing name", False),
    ],
)
def test_printings_are_matched_before_comparing(variant, stacked_label, same):
    """A bare card-id URL on the second source lands on whichever printing it
    defaults to. Comparing a $3,999 manga rare against a $2 reprint of the same
    card number marked 70 of 161 cards 'disputed' when nothing was wrong."""
    assert _same_printing(variant, stacked_label) is same
