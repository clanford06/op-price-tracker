"""Parse a GitHub issue-form body into a ledger entry.

GitHub renders issue forms into markdown as:

    ### Field label

    value

Unfilled optional fields render the literal string "_No response_", which is
why every value is checked for it -- treating that as data would put the text
"_No response_" into the ledger as a tag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NO_RESPONSE = "_no response_"


@dataclass
class ParsedEntry:
    kind: str
    description: str
    amount: float
    shipping: float
    fees: float | None
    tag: str
    category: str
    when: str | None
    planned: bool


class IssueParseError(ValueError):
    pass


# Fuzzy aliases so a hand-typed issue works as well as the form. Order matters:
# the first alias whose key appears in the line wins.
ALIASES = {
    "type": ("type", "kind"),
    "what was it?": ("what", "item", "description", "desc", "name"),
    "amount ($)": ("amount", "cost", "price", "paid", "gross"),
    "shipping & handling ($)": ("shipping", "ship", "postage", "handling"),
    "selling fees ($) — sales only": ("fees", "fee"),
    "tag": ("tag", "group", "label"),
    "category (expenses only)": ("category", "cat"),
    "date (yyyy-mm-dd)": ("date", "when"),
    "not paid yet": ("planned", "unpaid", "not paid"),
}


def parse_body(body: str) -> dict[str, str]:
    """Split an issue body into {canonical field: value}.

    Handles two shapes. GitHub's issue form renders '### Label' blocks. A
    hand-typed issue is far more likely to be 'amount: 39.39' lines, and that
    has to work too -- the form UI is not always cooperative, and a tracker you
    cannot enter data into is worthless.
    """
    text = body or ""
    fields: dict[str, str] = {}

    if "###" in text:
        parts = re.split(r"^###\s+(.+?)\s*$", text, flags=re.M)
        for i in range(1, len(parts) - 1, 2):
            fields[parts[i].strip().lower()] = parts[i + 1].strip()
        return fields

    # Fallback: "key: value" lines, matched loosely against the aliases.
    for line in text.splitlines():
        if ":" not in line:
            continue
        raw_key, _, raw_val = line.partition(":")
        key = re.sub(r"[^a-z ]", "", raw_key.strip().lower()).strip()
        val = raw_val.strip()
        if not key or not val:
            continue
        for canonical, names in ALIASES.items():
            if any(key == n or key.startswith(n) for n in names):
                fields.setdefault(canonical, val)
                break
    return fields


def _num(raw: str | None, *, field: str, default: float | None = 0.0) -> float | None:
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    if not cleaned or cleaned == NO_RESPONSE:
        return default
    cleaned = cleaned.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError as exc:
        raise IssueParseError(f"'{field}' must be a number, got {raw!r}") from exc


def _text(raw: str | None) -> str:
    if not raw:
        return ""
    v = raw.strip()
    return "" if v.lower() == NO_RESPONSE else v


def parse_entry(body: str, *, fee_pct: float, fee_flat: float) -> ParsedEntry:
    f = parse_body(body)

    kind_raw = _text(f.get("type")).lower()
    kind = next((k for k in ("expense", "sale", "holding") if kind_raw.startswith(k)), "")
    if not kind:
        raise IssueParseError(f"Type must be expense, sale or holding — got {kind_raw!r}")

    description = _text(f.get("what was it?"))
    if not description:
        raise IssueParseError("Description is required")

    amount = _num(f.get("amount ($)"), field="Amount")
    if amount is None or amount <= 0:
        raise IssueParseError("Amount must be greater than zero")

    shipping = _num(f.get("shipping & handling ($)"), field="Shipping") or 0.0
    fees = _num(f.get("selling fees ($) — sales only"), field="Fees", default=None)

    tag = _text(f.get("tag"))
    if not tag:
        raise IssueParseError("Tag is required — it is what groups your entries")

    category = _text(f.get("category (expenses only)")) or "other"
    when = _text(f.get("date (yyyy-mm-dd)")) or None
    if when and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        raise IssueParseError(f"Date must be YYYY-MM-DD, got {when!r}")

    planned_raw = (f.get("not paid yet") or "").lower()
    planned = "[x]" in planned_raw or planned_raw.strip() in {"yes", "true", "y", "1"}

    # A sale with no stated fees: estimate eBay's cut rather than recording zero,
    # which would overstate what you actually netted.
    if kind == "sale" and fees is None:
        fees = round(amount * fee_pct / 100 + fee_flat, 2)

    return ParsedEntry(
        kind=kind,
        description=description,
        amount=amount,
        shipping=shipping,
        fees=fees or 0.0,
        tag=tag,
        category=category,
        when=when,
        planned=planned,
    )
