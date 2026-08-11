"""Append entries to ledger.yaml from the command line.

Hand-editing YAML is where this kind of tracker dies: one bad indent and the
whole file stops loading. These commands append a correctly-formed block at a
marker and re-parse the file immediately, rolling back if the result is broken.

    python -m tracker add expense "OP-17 box"        180.00 --tag op17 --category sealed
    python -m tracker add holding "OP-17 Luffy alt"   40.00 --tag op17
    python -m tracker add sale    "Kuzan alt"          6.50 --tag op16 --fees 0.86

Comments in the file are preserved because nothing is ever re-serialised --
text is inserted at the marker and the rest is left untouched.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

MARKERS = {
    "expense": "# <<ADD_EXPENSES_HERE>>",
    "holding": "# <<ADD_HOLDINGS_HERE>>",
    "sale": "# <<ADD_SALES_HERE>>",
}


class LedgerEditError(RuntimeError):
    pass


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "item"


def _q(text: str) -> str:
    """Quote for YAML, escaping embedded quotes and backslashes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unique_id(existing: set[str], base: str) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def add_entry(
    ledger_path: Path,
    kind: str,
    description: str,
    amount: float,
    *,
    tag: str = "",
    category: str = "other",
    when: str | None = None,
    planned: bool = False,
    fees: float = 0.0,
    shipping_cost: float = 0.0,
    status: str = "owned",
) -> str:
    if kind not in MARKERS:
        raise LedgerEditError(f"kind must be one of {sorted(MARKERS)}, got {kind!r}")
    if not ledger_path.exists():
        raise LedgerEditError(f"Ledger not found: {ledger_path}")

    original = ledger_path.read_text(encoding="utf-8")
    marker = MARKERS[kind]
    if marker not in original:
        raise LedgerEditError(
            f"Marker {marker} missing from {ledger_path.name}. "
            f"Add it under the {kind}s list and try again."
        )

    when = when or date.today().isoformat()
    tag_line = f"\n    tag: {_q(tag)}" if tag else ""

    if kind == "expense":
        block = (
            f"\n  - date: {when}\n"
            f"    item: {_q(description)}\n"
            f"    category: {_q(category)}\n"
            f"    amount: {amount:.2f}"
            + (f"\n    planned: true" if planned else "")
            + tag_line
            + "\n"
        )
    elif kind == "sale":
        block = (
            f"\n  - date: {when}\n"
            f"    item: {_q(description)}\n"
            f"    gross: {amount:.2f}\n"
            f"    fees: {fees:.2f}\n"
            f"    shipping_cost: {shipping_cost:.2f}"
            + tag_line
            + "\n"
        )
    else:  # holding
        existing = set(re.findall(r"^\s*-\s*id:\s*(\S+)", original, re.M))
        hid = _unique_id(existing, _slug(description))
        block = (
            f"\n  - id: {hid}\n"
            f"    name: {_q(description)}\n"
            f"    status: {_q(status)}\n"
            f"    estimate: {amount:.2f}\n"
            f"    acquired: {_q(when)}"
            + tag_line
            + "\n"
        )

    updated = original.replace(marker, block.rstrip("\n") + "\n\n  " + marker, 1)
    ledger_path.write_text(updated, encoding="utf-8")

    # Parse it back. A ledger that no longer loads is worse than a missing entry.
    try:
        from .portfolio import load_ledger

        load_ledger(ledger_path)
    except Exception as exc:  # noqa: BLE001 - any parse failure must roll back
        ledger_path.write_text(original, encoding="utf-8")
        raise LedgerEditError(f"Entry rejected, ledger rolled back unchanged: {exc}") from exc

    return f"Added {kind}: {description} ${amount:.2f}" + (f" [{tag}]" if tag else "")
