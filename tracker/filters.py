"""Stage 1: relevance screening.

Cheap, title-only checks that answer one question -- "is this listing even the
product I asked for?" Wrong set, wrong language, singles, empty boxes, cases,
group breaks. This runs on every search result.

It deliberately does NOT judge trustworthiness. Everything about sellers,
authenticity and price plausibility lives in trust.py and runs afterwards on a
shortlist, because those checks cost an API call each.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .ebay import Listing


@dataclass
class Candidate:
    listing: Listing
    relevant: bool
    reasons: list[str] = field(default_factory=list)


def normalise(title: str) -> str:
    """Strip punctuation and case so 'OP-17', 'OP 17' and 'op17' compare equal."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def has_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word phrase match, so 'case' does not fire inside 'showcase'."""
    p = normalise(phrase)
    if not p:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", haystack) is not None


def screen_relevance(
    listings: list[Listing],
    *,
    require_all: list[str],
    require_any: list[str],
    exclude_any: list[str],
) -> list[Candidate]:
    out: list[Candidate] = []
    for item in listings:
        title = normalise(item.title)
        reasons: list[str] = []

        missing = [t for t in require_all if not has_phrase(title, t)]
        if missing:
            reasons.append(f"title missing required term(s): {', '.join(missing)}")

        if require_any and not any(has_phrase(title, t) for t in require_any):
            reasons.append(f"title matched none of: {', '.join(require_any)}")

        hits = [t for t in exclude_any if has_phrase(title, t)]
        if hits:
            reasons.append(f"title matched excluded term(s): {', '.join(hits)}")

        out.append(Candidate(listing=item, relevant=not reasons, reasons=reasons))
    return out


def peer_median(candidates: list[Candidate]) -> float | None:
    """Median delivered price of relevant listings, used for plausibility.

    Computed from relevance-passing listings only. Including singles and empty
    boxes would drag the median down and stop a counterfeit looking like an
    outlier -- which is the whole point of having it.
    """
    prices = [c.listing.total for c in candidates if c.relevant]
    return statistics.median(prices) if len(prices) >= 3 else None
