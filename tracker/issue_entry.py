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


def parse_body(body: str) -> dict[str, str]:
    """Split an issue-form body into {lowercased label: value}."""
    fields: dict[str, str] = {}
    # Split on "### Heading" while keeping the heading text.
    parts = re.split(r"^###\s+(.+?)\s*$", body or "", flags=re.M)
    # parts[0] is any preamble; then alternating heading, content.
    for i in range(1, len(parts) - 1, 2):
        label = parts[i].strip().lower()
        value = parts[i + 1].strip()
        fields[label] = value
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

    planned = "[x]" in (f.get("not paid yet") or "").lower()

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
