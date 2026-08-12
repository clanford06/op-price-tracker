"""Live API contract check.

Everything in this project was tested against stubs written from my *belief*
about eBay's response shape. Stubs cannot tell you that belief is wrong -- if a
field is named differently or nested elsewhere, the tests still pass and the
tracker quietly returns nothing.

This makes real calls and reports, field by field, whether the data the parsers
depend on is actually present. Run it first, the moment credentials work:

    python -m tracker --selftest

Costs three API calls and one notification. Writes nothing.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from .config import Settings
from .ebay import SEARCH_URL, EbayClient, EbayError
from .notify import Notifier

PROBE_QUERY = "One Piece Card Game booster box"

# Dotted paths the parsers read. "[]" means "first element of a list".
SUMMARY_FIELDS = {
    "itemId": True,
    "title": True,
    "itemWebUrl": True,
    "price.value": True,
    "price.currency": True,
    "condition": False,
    "seller.username": True,
    "seller.feedbackScore": True,
    "seller.feedbackPercentage": True,
    "topRatedBuyingExperience": False,
    "qualifiedPrograms": False,
    "itemLocation.country": False,
    "categories[].categoryName": False,
    "shippingOptions[].shippingCost.value": False,
}

DETAIL_FIELDS = {
    "returnTerms.returnsAccepted": False,
    "returnTerms.returnPeriod.value": False,
    "localizedAspects[].name": True,
    "localizedAspects[].value": True,
    "estimatedAvailabilities[].estimatedAvailableQuantity": False,
    "itemLocation.country": False,
    "categoryPath": False,
    "description": False,
}


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if part.endswith("[]"):
            key = part[:-2]
            cur = cur.get(key) if isinstance(cur, dict) else None
            if not isinstance(cur, list) or not cur:
                return None
            cur = cur[0]
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def _report(label: str, raw: dict, spec: dict[str, bool]) -> tuple[int, list[str]]:
    print(f"\n  {label}")
    critical_missing: list[str] = []
    ok = 0
    for path, critical in spec.items():
        value = _dig(raw, path)
        if value is None:
            mark = "MISSING " + ("(CRITICAL)" if critical else "(optional)")
            if critical:
                critical_missing.append(path)
            print(f"    ✗ {path:<52} {mark}")
        else:
            ok += 1
            shown = str(value)
            if len(shown) > 40:
                shown = shown[:37] + "…"
            print(f"    ✓ {path:<52} {shown}")
    return ok, critical_missing


def run(settings: Settings) -> int:
    print("=" * 78)
    print("LIVE API CONTRACT CHECK")
    print("=" * 78)
    problems: list[str] = []

    # -- 1. auth ----------------------------------------------------------
    print("\n[1/4] OAuth token")
    try:
        client = EbayClient(settings.ebay_client_id, settings.ebay_client_secret,
                            postal_code=settings.postal_code)
        token = client._get_token()  # noqa: SLF001 - intentional: this is the contract check
        print(f"    ✓ token acquired ({len(token)} chars)")
    except EbayError as exc:
        print(f"    ✗ {exc}")
        print("\nCannot continue without a token. Check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET")
        print("are your PRODUCTION keys (not Sandbox) and the account is approved.")
        return 1

    # -- 2. search --------------------------------------------------------
    print("\n[2/4] item_summary/search")
    try:
        resp = requests.get(
            SEARCH_URL,
            headers=client._headers(),  # noqa: SLF001
            params={
                "q": PROBE_QUERY,
                "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW},price:[30..500],priceCurrency:USD",
                "limit": 3,
                "sort": "price",
            },
            timeout=45,
        )
    except requests.RequestException as exc:
        print(f"    ✗ request failed: {exc}")
        return 1

    if resp.status_code != 200:
        print(f"    ✗ HTTP {resp.status_code}: {resp.text[:400]}")
        print("\n    The filter syntax is the usual culprit here.")
        return 1

    items = resp.json().get("itemSummaries") or []
    print(f"    ✓ HTTP 200, {len(items)} item(s) returned")
    if not items:
        print("    ✗ no items -- cannot validate the response shape")
        return 1

    _, missing = _report("Fields the parser reads from each search result:", items[0], SUMMARY_FIELDS)
    problems += [f"search: {m}" for m in missing]

    # -- 3. detail --------------------------------------------------------
    print("\n[3/4] item/{item_id}")
    detail = client.get_detail(items[0]["itemId"])
    if detail.fetch_error:
        print(f"    ✗ {detail.fetch_error}")
        problems.append("detail: endpoint unreachable")
    else:
        raw = requests.get(
            f"https://api.ebay.com/buy/browse/v1/item/{requests.utils.quote(items[0]['itemId'], safe='')}",
            headers=client._headers(),  # noqa: SLF001
            params={"fieldgroups": "PRODUCT"},
            timeout=45,
        ).json()
        print("    ✓ HTTP 200")
        _, missing = _report("Fields the parser reads from item detail:", raw, DETAIL_FIELDS)
        problems += [f"detail: {m}" for m in missing]

        aspects = {a.get("name"): a.get("value") for a in raw.get("localizedAspects") or []}
        print(f"\n    Item specifics actually present ({len(aspects)}):")
        for k, v in list(aspects.items())[:14]:
            print(f"      {k}: {v}")
        if not any(k.lower() in {"language", "card language", "game language"} for k in aspects):
            print("\n    ! No Language aspect on this listing.")
            print("      The 18-point language signal will score 0 for listings like this.")
            print("      If --calibrate shows that is common, that weighting needs revisiting.")

    # -- 4. notification --------------------------------------------------
    print("\n[4/4] ntfy push")
    if not settings.ntfy_topic:
        print("    - skipped (NTFY_TOPIC not set)")
    else:
        sent = Notifier(settings.ntfy_server, settings.ntfy_topic).send(
            title="Tracker self-test",
            message="If this arrived on your phone, notifications are wired correctly.",
            tags=["white_check_mark"],
        )
        print("    ✓ sent — check your phone" if sent else "    ✗ failed to send")
        if not sent:
            problems.append("ntfy: push failed")

    # -- verdict ----------------------------------------------------------
    print("\n" + "=" * 78)
    if problems:
        print("PROBLEMS FOUND — the parsers need adjusting before trusting output:")
        for p in problems:
            print(f"  - {p}")
        print("=" * 78)
        return 1
    print("ALL CONTRACT CHECKS PASSED")
    print("Next: python -m tracker --calibrate    (tune thresholds against real listings)")
    print("=" * 78)
    return 0
