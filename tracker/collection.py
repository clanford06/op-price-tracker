"""Import the collection from the TCGplayer app's CSV export.

This replaces transcribing screenshots, which is why the holdings list went
three weeks without an update: a sync that costs an evening does not happen,
and a stale collection understates the position exactly as badly as missing
costs overstate it.

The export is the right input because it already carries the TCGplayer
**Product ID**. That is the same id this tracker pins prices to, so no name
matching is involved anywhere -- and name matching is the thing that goes
wrong. "OP17-020 Shanks" resolves to the OP13-028 Shanks SP; searching a card
number alone still returns four different Kaidos between $0.86 and $1,234.
The app hands us the exact printing, in the exact quantity, and we keep it.

Division of labour after an import:

    the CSV      decides WHAT is owned and HOW MANY
    the price job decides WHAT IT IS WORTH, twice a day, by product id

So re-importing never fights the pricing, and re-pricing never invents a card.

Two kinds of holding are deliberately NOT importable, listed by product id in
`settings.graded_product_ids`. Carter tracks his slabs in the app alongside
everything else, but the app quotes the RAW card: importing Ms. All Sunday
would overwrite a reasoned $500 floor and its grade scenarios with a $527 raw
quote, and the PSA 10 Kidd would drop from $169 to $88.66. A graded card is a
different asset from the card inside it.

    python -m tracker --import-collection ~/Downloads/OP-TCG_092726.csv
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

START = "  # <<TCG_COLLECTION_START>>"
END = "  # <<TCG_COLLECTION_END>>"


class CollectionError(RuntimeError):
    pass


@dataclass
class Row:
    product_id: int
    name: str
    number: str
    set_name: str
    rarity: str
    printing: str
    condition: str
    qty: int
    market: float

    @property
    def value(self) -> float:
        return round(self.market * self.qty, 2)

    @property
    def slug(self) -> str:
        """Stable id: card number plus printing, falling back to the name.

        Number alone collides -- OP17-005 is both the base Super Rare and the
        Alternate Art, and OP17-063 is two different Kaidos. The product id is
        appended so a slug can never silently merge two printings.
        """
        base = (self.number or self.name).lower()
        base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
        return f"{base}-{self.product_id}"


def _num(v: str, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return default


def read_csv(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"Product ID", "Product Name", "Add to Quantity"} - set(reader.fieldnames or [])
        if missing:
            raise CollectionError(
                f"{path.name} is not a TCGplayer collection export -- missing {sorted(missing)}. "
                f"In the app: Collection tab, open the list, ... menu, Send via Email, "
                f"with Email Export set to CSV."
            )
        rows: list[Row] = []
        for r in reader:
            pid = r.get("Product ID", "").strip()
            if not pid.isdigit():
                continue
            # Quantity lives in "Add to Quantity"; "Total Quantity" exports blank.
            qty = int(_num(r.get("Add to Quantity") or r.get("Total Quantity") or 1, 1)) or 1
            rows.append(Row(
                product_id=int(pid),
                name=(r.get("Product Name") or "").strip(),
                number=(r.get("Number") or "").strip(),
                set_name=(r.get("Set Name") or "").strip(),
                rarity=(r.get("Rarity") or "").strip(),
                printing=(r.get("Printing") or "").strip(),
                condition=(r.get("Condition") or "").strip(),
                qty=qty,
                market=_num(r.get("TCG Market Price")),
            ))
    if not rows:
        raise CollectionError(f"No card rows found in {path.name}.")
    return rows


def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_yaml(rows: list[Row], *, exported: str) -> str:
    """The generated holdings block. Everything here is disposable."""
    total = sum(r.value for r in rows)
    copies = sum(r.qty for r in rows)
    out = [
        START,
        "  # GENERATED from the TCGplayer app export. Do not hand-edit: the next",
        "  # --import-collection run overwrites everything between these markers.",
        "  # Anything that must survive a re-import belongs ABOVE the start marker.",
        "  #",
        f"  # Export {exported} -- {len(rows)} lines, {copies} copies, "
        f"${total:,.2f} at app market.",
        "  # `estimate` here is the app's market price at export time; the twice-daily",
        "  # price job re-quotes it from the same product id and takes over from there.",
        "",
    ]
    for r in sorted(rows, key=lambda x: -x.value):
        label = r.name if not r.number else f"{r.number} {r.name}"
        out += [
            f"  - id: {r.slug}",
            f"    name: {_yaml_str(label)}",
            f"    status: owned",
            f"    tcgplayer_id: {r.product_id}",
            f"    estimate: {r.value:.2f}",
        ]
        if r.qty > 1:
            out.append(f"    qty: {r.qty}")
        out += [
            f"    tag: {_yaml_str(_tag_for(r))}",
            f"    source: {_yaml_str(f'{r.set_name} · {r.rarity} · {r.printing} · {r.condition}')}",
            "",
        ]
    out.append(END)
    return "\n".join(out) + "\n"


def _tag_for(row: Row) -> str:
    """Group by set, so per-set profit still rolls up after an import."""
    m = re.match(r"^(OP\d\d|EB\d\d|ST\d\d|P)-", row.number or "")
    if m:
        return m.group(1).lower()
    return "promo"


def import_into(ledger_path: Path, csv_path: Path, *, graded: set[int]) -> dict:
    rows = read_csv(csv_path)
    kept = [r for r in rows if r.product_id not in graded]
    skipped = [r for r in rows if r.product_id in graded]

    text = ledger_path.read_text(encoding="utf-8")
    if START not in text or END not in text:
        raise CollectionError(
            f"Markers missing from {ledger_path.name}. Add {START.strip()} and "
            f"{END.strip()} around the generated holdings region first."
        )
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    block = to_yaml(kept, exported=csv_path.name)
    text = head + block.rstrip("\n") + tail

    # Record the sync date so the staleness warning clears itself.
    text = re.sub(r"(?m)^(  collection_synced:).*$",
                  rf"\1 {date.today().isoformat()}", text, count=1)
    ledger_path.write_text(text, encoding="utf-8")
    return {
        "lines": len(kept),
        "copies": sum(r.qty for r in kept),
        "value": round(sum(r.value for r in kept), 2),
        "skipped": skipped,
    }
