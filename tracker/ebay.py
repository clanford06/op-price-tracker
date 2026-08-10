"""eBay Browse API client.

Two stages, because search results alone are too thin to trust:

  search()      -- item_summary/search. Cheap, returns many listings, but only
                   carries title/price/seller. Used to build a shortlist.
  get_detail()  -- item/{id}. One call per finalist. Returns the structured
                   evidence that actually matters: item specifics (Language,
                   Set, Configuration), return terms, stock quantity, seller
                   location, and eBay programme membership.

The Browse API returns *active* listings, not sold comps -- sold data lives
behind Marketplace Insights, which needs approval most individuals will not
get. For "what can I buy this box for right now", active listings are correct.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
SCOPE = "https://api.ebay.com/oauth/api_scope"

PAGE_SIZE = 200


class EbayError(RuntimeError):
    pass


@dataclass
class Detail:
    """Structured evidence from item/{id}. Absent fields stay None, never False.

    The distinction matters: "seller did not state a return policy" and "seller
    refuses returns" are different signals, and collapsing them to False would
    silently punish listings that simply omitted the field.
    """

    returns_accepted: bool | None = None
    return_days: int | None = None
    aspects: dict[str, str] = field(default_factory=dict)
    quantity: int | None = None
    location_country: str | None = None
    category_path: str = ""
    description_text: str = ""
    fetch_error: str | None = None

    def aspect(self, *names: str) -> str | None:
        """Case-insensitive lookup across possible aspect spellings."""
        lowered = {k.lower(): v for k, v in self.aspects.items()}
        for n in names:
            if (v := lowered.get(n.lower())) is not None:
                return v
        return None


@dataclass
class Listing:
    item_id: str
    title: str
    url: str
    price: float
    shipping: float
    currency: str
    condition: str
    seller_name: str
    seller_score: int
    seller_pct: float
    top_rated: bool = False
    programs: list[str] = field(default_factory=list)
    location_country: str | None = None
    categories: list[str] = field(default_factory=list)
    detail: Detail | None = None

    @property
    def total(self) -> float:
        """Delivered cost. A $60 box with $25 shipping is not a $60 box."""
        return round(self.price + self.shipping, 2)


class EbayClient:
    def __init__(self, client_id: str, client_secret: str, marketplace: str = "EBAY_US"):
        if not client_id or not client_secret:
            raise EbayError(
                "Missing eBay credentials. Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET "
                "(see .env.example)."
            )
        self._id = client_id
        self._secret = client_secret
        self._marketplace = marketplace
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._session = requests.Session()

    # -- auth ---------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        resp = self._session.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if resp.status_code != 200:
            raise EbayError(
                f"eBay token request failed ({resp.status_code}). "
                f"Check your credentials are Production, not Sandbox. Body: {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 7200))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace,
            "X-EBAY-C-ENDUSERCTX": "affiliateCampaignId=,contextualLocation=country%3DUS",
        }

    # -- search -------------------------------------------------------------

    def search(
        self, query: str, *, min_price: float, max_price: float, max_results: int = 200
    ) -> list[Listing]:
        filters = [
            "buyingOptions:{FIXED_PRICE}",
            "conditions:{NEW}",
            "itemLocationCountry:US",
            f"price:[{min_price}..{max_price}]",
            "priceCurrency:USD",
        ]
        listings: list[Listing] = []
        for offset in range(0, max_results, PAGE_SIZE):
            batch = self._search_page(
                query,
                ",".join(filters),
                limit=min(PAGE_SIZE, max_results - offset),
                offset=offset,
            )
            listings.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return listings

    def _search_page(self, query: str, filter_str: str, *, limit: int, offset: int) -> list[Listing]:
        resp = self._session.get(
            SEARCH_URL,
            headers=self._headers(),
            params={
                "q": query,
                "filter": filter_str,
                "limit": limit,
                "offset": offset,
                "sort": "price",
            },
            timeout=45,
        )
        if resp.status_code == 429:
            raise EbayError("eBay rate limit hit (429). The free tier allows ~5000 calls/day.")
        if resp.status_code != 200:
            raise EbayError(f"eBay search failed ({resp.status_code}): {resp.text[:300]}")
        return list(_parse_items(resp.json().get("itemSummaries") or []))

    # -- detail -------------------------------------------------------------

    def get_detail(self, item_id: str) -> Detail:
        """Fetch structured evidence for one listing.

        Never raises: a listing we cannot verify is recorded as unverified and
        scored down, which is the safe direction. Throwing here would let one
        dead listing abort an entire price run.
        """
        try:
            resp = self._session.get(
                ITEM_URL.format(item_id=requests.utils.quote(item_id, safe="")),
                headers=self._headers(),
                params={"fieldgroups": "PRODUCT"},
                timeout=45,
            )
        except requests.RequestException as exc:
            return Detail(fetch_error=str(exc))

        if resp.status_code != 200:
            return Detail(fetch_error=f"HTTP {resp.status_code}")

        try:
            return _parse_detail(resp.json())
        except (ValueError, KeyError, TypeError) as exc:
            return Detail(fetch_error=f"parse failed: {exc}")


def _parse_detail(raw: dict[str, Any]) -> Detail:
    terms = raw.get("returnTerms") or {}
    period = (terms.get("returnPeriod") or {}).get("value")

    aspects: dict[str, str] = {}
    for a in raw.get("localizedAspects") or []:
        name, value = a.get("name"), a.get("value")
        if name and value:
            aspects[str(name)] = str(value)

    avail = raw.get("estimatedAvailabilities") or []
    qty = None
    if avail:
        qty = avail[0].get("estimatedAvailableQuantity")

    desc = raw.get("description") or ""
    # Description is HTML; strip tags crudely -- we only scan it for red-flag phrases.
    import re as _re

    desc_text = _re.sub(r"<[^>]+>", " ", desc)
    desc_text = _re.sub(r"\s+", " ", desc_text).strip()[:4000]

    return Detail(
        returns_accepted=terms.get("returnsAccepted"),
        return_days=int(period) if isinstance(period, (int, float)) else None,
        aspects=aspects,
        quantity=int(qty) if isinstance(qty, (int, float)) else None,
        location_country=(raw.get("itemLocation") or {}).get("country"),
        category_path=str(raw.get("categoryPath") or ""),
        description_text=desc_text,
    )


def _parse_items(raw_items: list[dict[str, Any]]) -> Iterator[Listing]:
    for item in raw_items:
        price = _money(item.get("price"))
        if price is None:
            continue
        seller = item.get("seller") or {}
        yield Listing(
            item_id=item.get("itemId", ""),
            title=item.get("title", ""),
            url=item.get("itemWebUrl", ""),
            price=price,
            shipping=_shipping_cost(item),
            currency=(item.get("price") or {}).get("currency", "USD"),
            condition=item.get("condition", "") or "",
            seller_name=seller.get("username", "") or "",
            seller_score=int(seller.get("feedbackScore") or 0),
            seller_pct=float(seller.get("feedbackPercentage") or 0.0),
            top_rated=bool(item.get("topRatedBuyingExperience")),
            programs=[str(p) for p in (item.get("qualifiedPrograms") or [])],
            location_country=(item.get("itemLocation") or {}).get("country"),
            categories=[
                str(c.get("categoryName"))
                for c in (item.get("categories") or [])
                if c.get("categoryName")
            ],
        )


def _money(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    try:
        return float(node["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _shipping_cost(item: dict[str, Any]) -> float:
    """Cheapest advertised shipping. Unknown shipping is treated as free.

    Deliberately optimistic: it can make a listing look cheaper than it
    delivers for, so the dashboard always links out to the real listing.
    """
    options = item.get("shippingOptions") or []
    costs = [c for c in (_money(o.get("shippingCost")) for o in options) if c is not None]
    return min(costs) if costs else 0.0
