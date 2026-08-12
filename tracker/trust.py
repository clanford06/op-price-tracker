"""Trust scoring for individual listings.

Nothing here proves a sealed box is genuine -- no data source can. What it does
is gather every independently checkable signal eBay exposes, weight them, and
show its working, so a recommendation is auditable rather than a bare number.

Two mechanisms, deliberately separate:

  VETOES  Disqualifying facts. Any single one drops the listing outright, no
          matter how good everything else looks. A 40,000-feedback Top Rated
          seller listing a Japanese box is still the wrong box.

  SCORE   Weighted evidence, 0-100. Listings must clear `min_trust_score` to be
          recommended. Missing evidence scores zero rather than failing, so an
          unverifiable listing sinks instead of silently passing.

The asymmetry is intentional: this is built to leave money on the table rather
than lose it. Rejecting a genuine bargain costs a missed deal; accepting a fake
costs the whole purchase.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .ebay import Detail, Listing

# Phrases that make a listing disqualifying on sight. Whole-word matched, so
# "custom" cannot fire inside "customer" and "repro" cannot fire inside
# "reproduction rights". Deliberately excludes ambiguous words -- "opened"
# is absent because "never opened" is both extremely common and a good sign.
FAKE_PHRASES = (
    "resealed",
    "re sealed",
    "reseal",
    "proxy",
    "proxies",
    "replica",
    "reproduction",
    "repro",
    "counterfeit",
    "bootleg",
    "unofficial",
    "fan made",
    "fanmade",
    "custom",
    "handmade",
)

# Softer signals: penalised in the description, not disqualifying.
DESCRIPTION_WARNINGS = (
    "no returns",
    "as is",
    "all sales final",
    "sold as seen",
)

# Ceiling for a listing whose item detail could not be fetched. Seller
# reputation alone was otherwise worth ~50/100, which is halfway to trusted on
# the strength of knowing nothing at all about the actual item. Reputation says
# the seller ships and answers messages; it says nothing about what is in the
# box. Kept below every sane min_trust_score so unverified never wins on its own.
UNVERIFIED_SCORE_CAP = 45

NON_ENGLISH_LANGUAGES = (
    "japanese",
    "japan",
    "korean",
    "chinese",
    "simplified chinese",
    "traditional chinese",
    "thai",
    "german",
    "french",
    "spanish",
    "italian",
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _has_phrase(haystack: str, phrase: str) -> bool:
    p = _norm(phrase)
    if not p:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", haystack) is not None


@dataclass
class Signal:
    name: str
    earned: float
    possible: float
    detail: str
    good: bool = True
    bonus: bool = False
    """Bonus signals add to the score but not to the denominator.

    Used for evidence that most legitimate listings structurally cannot supply,
    where counting it against them would dock every listing for nothing. If a
    signal is unearnable by an honest seller, it is not a fair test -- it is a
    flat penalty wearing a scoring component's clothes.
    """


@dataclass
class TrustReport:
    score: int = 0
    vetoes: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    verified: bool = False          # did we successfully fetch item detail?

    @property
    def passed(self) -> bool:
        return not self.vetoes

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "vetoes": self.vetoes,
            "verified": self.verified,
            "signals": [
                {
                    "name": s.name,
                    "earned": round(s.earned, 1),
                    "possible": s.possible,
                    "detail": s.detail,
                    "good": s.good,
                }
                for s in self.signals
            ],
        }


@dataclass
class TrustPolicy:
    """Per-product thresholds. Every one is tunable from watchlist.yaml."""

    min_trust_score: int = 70
    min_seller_score: int = 100
    min_seller_pct: float = 98.0
    implausible_below: float | None = None
    require_returns: bool = True
    require_us_location: bool = True
    require_verified_detail: bool = True
    require_known_shipping: bool = True
    max_quantity: int = 12
    blocked_sellers: tuple[str, ...] = ()
    trusted_sellers: tuple[str, ...] = ()
    expect_language: str = "english"
    exclude_terms: tuple[str, ...] = ()   # re-checked against the UNTRUNCATED title
    expect_terms: tuple[str, ...] = ()      # e.g. set codes, for aspect matching


def evaluate(
    listing: Listing,
    policy: TrustPolicy,
    *,
    median_price: float | None = None,
) -> TrustReport:
    """Score one listing. `median_price` is the median of its peer group."""
    report = TrustReport()
    detail = listing.detail
    report.verified = bool(detail and not detail.fetch_error)

    title = _norm(listing.title)
    seller_key = listing.seller_name.strip().lower()

    # ---- vetoes ----------------------------------------------------------

    if seller_key and seller_key in {s.lower() for s in policy.blocked_sellers}:
        report.vetoes.append(f"seller '{listing.seller_name}' is on your blocklist")

    for phrase in FAKE_PHRASES:
        if _has_phrase(title, phrase):
            report.vetoes.append(f"title contains '{phrase}' — not a genuine sealed box")

    if policy.implausible_below and listing.total < policy.implausible_below:
        report.vetoes.append(
            f"${listing.total:.2f} is below the ${policy.implausible_below:.2f} floor for a "
            f"genuine sealed box"
        )

    if listing.seller_score < policy.min_seller_score:
        report.vetoes.append(
            f"seller has {listing.seller_score} sales, below the {policy.min_seller_score} minimum"
        )

    if listing.seller_pct < policy.min_seller_pct:
        report.vetoes.append(
            f"seller rated {listing.seller_pct}%, below the {policy.min_seller_pct}% minimum"
        )

    if policy.require_known_shipping and not getattr(listing, "shipping_known", True):
        report.vetoes.append(
            "seller did not report a shipping cost, so the delivered price is "
            "unknown — a $6.29 pack with $4.39 postage is not a $6.29 pack"
        )

    if policy.require_us_location:
        country = (detail.location_country if detail else None) or listing.location_country
        if country and country.upper() != "US":
            report.vetoes.append(f"ships from {country}, not the US")

    if detail and not detail.fetch_error:
        # Search truncates titles at 80 chars. Re-run the exclusions against the
        # full title from item detail, or "(Japanese)" survives as "(Jap...".
        if detail.full_title:
            full = _norm(detail.full_title)
            for term in policy.exclude_terms:
                if _has_phrase(full, term):
                    report.vetoes.append(
                        f"full title contains '{term}' (search result was truncated)"
                    )
                    break

        lang = detail.aspect("Language", "Card Language", "Game Language")
        if lang:
            low = _norm(lang)
            found_bad = [b for b in NON_ENGLISH_LANGUAGES if _has_phrase(low, b)]
            has_english = _has_phrase(low, policy.expect_language)
            if found_bad and has_english:
                # Dual-tagged. Either the seller stocks both and you cannot tell
                # which arrives, or it is a non-English product mislabelled.
                # Ambiguity about the actual product is not something to score
                # around -- it is a reason to walk away.
                report.vetoes.append(
                    f"item specifics list more than one language ({lang}) — "
                    f"cannot tell which printing you would receive"
                )
            elif found_bad:
                report.vetoes.append(f"item specifics say Language: {lang}")

        for phrase in FAKE_PHRASES:
            if _has_phrase(_norm(detail.description_text), phrase):
                report.vetoes.append(f"description contains '{phrase}'")
                break

        if policy.require_returns and detail.returns_accepted is False:
            report.vetoes.append("seller does not accept returns")

    elif policy.require_verified_detail:
        report.vetoes.append(
            "could not verify item details"
            + (f" ({detail.fetch_error})" if detail and detail.fetch_error else "")
        )

    # ---- weighted signals ------------------------------------------------

    report.signals.append(_score_language(detail))
    report.signals.append(_score_seller_volume(listing))
    report.signals.append(_score_seller_rating(listing))
    report.signals.append(_score_returns(detail))
    report.signals.append(_score_top_rated(listing))
    report.signals.append(_score_price_plausibility(listing, median_price))
    report.signals.append(_score_aspects(detail, policy))
    report.signals.append(_score_category(listing, detail))
    report.signals.append(_score_quantity(detail, policy))
    report.signals.append(_score_programs(listing))
    report.signals.append(_score_description(detail))
    report.signals.append(_score_trusted_seller(listing, policy))

    # Bonus signals contribute to the numerator only, so a listing is never
    # docked for evidence an honest seller could not have provided.
    earned = sum(s.earned for s in report.signals if not s.bonus)
    possible = sum(s.possible for s in report.signals if not s.bonus)
    bonus = sum(s.earned for s in report.signals if s.bonus)
    score = int(round(100 * (earned + bonus) / possible)) if possible else 0
    score = min(score, 100)

    if not report.verified and score > UNVERIFIED_SCORE_CAP:
        report.signals.append(
            Signal(
                "Unverified cap",
                0,
                0,
                f"item detail unavailable — score capped at {UNVERIFIED_SCORE_CAP} "
                f"(would have been {score} on seller reputation alone)",
                good=False,
            )
        )
        score = UNVERIFIED_SCORE_CAP

    report.score = score
    return report


# -- individual signals ----------------------------------------------------


def _score_language(detail: Detail | None) -> Signal:
    """Structured 'Language: English' beats inferring it from title keywords."""
    if not detail or detail.fetch_error:
        return Signal("Language verified", 0, 18, "item specifics unavailable", good=False)
    lang = detail.aspect("Language", "Card Language", "Game Language")
    if not lang:
        return Signal("Language verified", 4, 18, "seller did not state a language", good=False)
    if _has_phrase(_norm(lang), "english"):
        return Signal("Language verified", 18, 18, f"item specifics: {lang}")
    return Signal("Language verified", 0, 18, f"item specifics: {lang}", good=False)


def _score_seller_volume(listing: Listing) -> Signal:
    """Log-scaled: 100 -> 10,000 sales matters far more than 10,000 -> 20,000."""
    n = max(listing.seller_score, 0)
    frac = min(math.log10(n + 1) / 4.0, 1.0)  # saturates at 10,000 sales
    return Signal("Seller volume", 14 * frac, 14, f"{n:,} completed sales")


def _score_seller_rating(listing: Listing) -> Signal:
    pct = listing.seller_pct
    # 98% is the floor, 100% is full marks; below the floor a veto already fired.
    frac = max(0.0, min((pct - 98.0) / 2.0, 1.0))
    return Signal("Seller rating", 14 * frac, 14, f"{pct}% positive")


def _score_returns(detail: Detail | None) -> Signal:
    if not detail or detail.fetch_error:
        return Signal("Returns", 0, 12, "unknown", good=False)
    if detail.returns_accepted is None:
        return Signal("Returns", 3, 12, "not stated", good=False)
    if not detail.returns_accepted:
        return Signal("Returns", 0, 12, "not accepted", good=False)
    days = detail.return_days or 0
    frac = 0.7 + 0.3 * min(days / 30.0, 1.0)
    label = f"accepted ({days} days)" if days else "accepted"
    return Signal("Returns", 12 * frac, 12, label)


def _score_top_rated(listing: Listing) -> Signal:
    """Bonus only. Live calibration found 0 of 16 real listings were Top Rated
    -- at the cheap end of the market almost nobody holds the badge, so scoring
    its absence was a flat 10-point penalty on every honest listing."""
    if listing.top_rated:
        return Signal("Top Rated Seller", 10, 10, "eBay Top Rated", bonus=True)
    return Signal("Top Rated Seller", 0, 10, "not Top Rated (bonus only)", good=False, bonus=True)


def _score_price_plausibility(listing: Listing, median: float | None) -> Signal:
    """Reward sitting in a believable band; punish being far under peers."""
    if median is None or median <= 0:
        return Signal("Price plausibility", 6, 12, "no peer group to compare against", good=False)
    ratio = listing.total / median
    if ratio < 0.6:
        return Signal(
            "Price plausibility", 0, 12,
            f"${listing.total:.2f} is {(1-ratio)*100:.0f}% under the ${median:.2f} median",
            good=False,
        )
    if ratio < 0.8:
        return Signal(
            "Price plausibility", 6, 12,
            f"${listing.total:.2f} is well under the ${median:.2f} median — verify carefully",
            good=False,
        )
    return Signal("Price plausibility", 12, 12, f"in line with the ${median:.2f} median")


def _score_aspects(detail: Detail | None, policy: TrustPolicy) -> Signal:
    """Do the seller's own item specifics corroborate the set and format?"""
    if not detail or detail.fetch_error:
        return Signal("Item specifics", 0, 8, "unavailable", good=False)

    config = detail.aspect("Configuration", "Product Type", "Type", "Format") or ""
    blob = _norm(" ".join(detail.aspects.values()))

    hits: list[str] = []
    if _has_phrase(_norm(config), "booster box") or _has_phrase(blob, "booster box"):
        hits.append("booster box")
    if policy.expect_terms and any(_has_phrase(blob, t) for t in policy.expect_terms):
        hits.append("set matches")

    if not detail.aspects:
        return Signal("Item specifics", 0, 8, "seller listed none", good=False)
    if not hits:
        return Signal("Item specifics", 2, 8, "present but do not confirm set/format", good=False)
    return Signal("Item specifics", 8 * min(len(hits) / 2.0, 1.0), 8, ", ".join(hits))


def _score_category(listing: Listing, detail: Detail | None) -> Signal:
    path = _norm((detail.category_path if detail else "") or " ".join(listing.categories))
    if not path:
        return Signal("eBay category", 0, 6, "unknown", good=False)
    if _has_phrase(path, "ccg") or "card" in path or "trading" in path:
        return Signal("eBay category", 6, 6, "listed under trading cards")
    return Signal("eBay category", 0, 6, "listed outside trading cards", good=False)


def _score_quantity(detail: Detail | None, policy: TrustPolicy) -> Signal:
    """A seller with 80 sealed boxes of a hot set is a distributor or a problem."""
    if not detail or detail.fetch_error or detail.quantity is None:
        return Signal("Stock level", 1, 3, "unknown", good=False)
    q = detail.quantity
    if q > policy.max_quantity:
        return Signal("Stock level", 0, 3, f"{q} in stock — unusually high", good=False)
    return Signal("Stock level", 3, 3, f"{q} in stock")


def _score_programs(listing: Listing) -> Signal:
    """Bonus only: eBay's Authenticity Guarantee covers graded and raw single
    cards, not sealed boxes. Scoring its absence would dock every sealed-box
    listing for failing a test none of them can sit."""
    good = [p for p in listing.programs if "AUTHENTICITY" in p.upper()]
    if good:
        return Signal("eBay programmes", 5, 5, ", ".join(good), bonus=True)
    return Signal("eBay programmes", 0, 5, "none (bonus only)", good=False, bonus=True)


def _score_description(detail: Detail | None) -> Signal:
    if not detail or detail.fetch_error:
        return Signal("Description", 0, 4, "unavailable", good=False)
    text = _norm(detail.description_text)
    if not text:
        return Signal("Description", 1, 4, "empty", good=False)
    hits = [w for w in DESCRIPTION_WARNINGS if _has_phrase(text, w)]
    if hits:
        return Signal("Description", 0, 4, f"contains: {', '.join(hits)}", good=False)
    return Signal("Description", 4, 4, "no warning phrases")


def _score_trusted_seller(listing: Listing, policy: TrustPolicy) -> Signal:
    """Bonus only: the list starts empty, so counting its absence would dock
    every listing 6 points on day one for a list you have not built yet."""
    if listing.seller_name.strip().lower() in {s.lower() for s in policy.trusted_sellers}:
        return Signal("Your trusted list", 8, 8, "seller is on your trusted list", bonus=True)
    return Signal(
        "Your trusted list", 0, 8, "not on your trusted list (bonus only)", good=False, bonus=True
    )
