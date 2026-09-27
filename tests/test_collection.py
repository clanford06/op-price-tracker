"""Checks on the TCGplayer collection import.

The import decides what Carter owns. The two ways it can lie are dropping a
card and importing one it must not touch, so both are pinned down here.
"""

from __future__ import annotations

import pytest

from tracker.collection import END, START, CollectionError, import_into, read_csv

HEADER = ("Product ID,TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,"
          "Rarity,Condition,Printing,TCG Market Price,TCG Direct Low,"
          "TCG Low Price With Shipping,TCG Low Price,Total Quantity,Add to Quantity,"
          "TCG Marketplace Price,Photo URL\n")


def row(pid, name, number, price, qty, printing="Foil"):
    return (f"{pid},9,One Piece Card Game,Set,{name},,{number},Super Rare,Near Mint,"
            f"{printing},{price},,,,,{qty},,http://img\n")


def write_csv(tmp_path, *rows):
    p = tmp_path / "OP-TCG.csv"
    p.write_text(HEADER + "".join(rows))
    return p


def ledger(tmp_path, body=""):
    p = tmp_path / "ledger.yaml"
    p.write_text("settings:\n  fee_pct: 13.25\n  fee_flat: 0.40\n"
                 "  collection_synced: 2020-01-01\n\nholdings:\n"
                 f"{body}{START}\n{END}\n\nsales: []\n")
    return p


def test_quantity_comes_from_add_to_quantity(tmp_path):
    # "Total Quantity" exports blank; reading it would make every line qty 1
    # and silently undercount a collection with duplicates.
    rows = read_csv(write_csv(tmp_path, row(1, "Card", "OP17-005", "1.44", 4)))
    assert rows[0].qty == 4
    assert rows[0].value == 5.76      # 1.44 x 4, not 1.44


def test_graded_products_are_never_imported(tmp_path):
    # The app lists slabs at their RAW price. Importing Ms. All Sunday would
    # overwrite a reasoned floor with a raw quote for a card that is at PSA.
    csv_path = write_csv(tmp_path,
                         row(695324, "Ms. All Sunday (SP)", "OP14-084", "527.10", 1),
                         row(712091, "Kaido (Super Alt)", "OP17-062", "217.00", 1))
    led = ledger(tmp_path)
    res = import_into(led, csv_path, graded={695324})
    assert res["lines"] == 1
    assert [r.product_id for r in res["skipped"]] == [695324]
    assert "695324" not in led.read_text()


def test_two_printings_of_one_number_stay_separate(tmp_path):
    # OP17-005 is both the base Super Rare and the Alternate Art. A slug built
    # from the card number alone would merge them into one holding.
    csv_path = write_csv(tmp_path,
                         row(711499, "Edward.Newgate (005)", "OP17-005", "1.44", 4),
                         row(708077, "Edward.Newgate (005) (Alt)", "OP17-005", "16.74", 1))
    rows = read_csv(csv_path)
    assert rows[0].slug != rows[1].slug


def test_import_replaces_the_region_and_keeps_what_is_above_it(tmp_path):
    manual = ('  - id: keep-me\n    name: "slab"\n    status: owned\n'
              '    estimate: 169.00\n\n')
    led = ledger(tmp_path, manual)
    import_into(led, write_csv(tmp_path, row(1, "A", "OP17-001", "5.00", 1)), graded=set())
    import_into(led, write_csv(tmp_path, row(2, "B", "OP17-002", "7.00", 1)), graded=set())
    text = led.read_text()
    assert "keep-me" in text                 # survived both runs
    assert "tcgplayer_id: 2" in text
    assert "tcgplayer_id: 1" not in text     # the first import was replaced, not appended


def test_import_stamps_the_sync_date(tmp_path):
    from datetime import date

    led = ledger(tmp_path)
    import_into(led, write_csv(tmp_path, row(1, "A", "OP17-001", "5.00", 1)), graded=set())
    assert f"collection_synced: {date.today().isoformat()}" in led.read_text()


def test_a_wrong_csv_is_refused_with_instructions(tmp_path):
    p = tmp_path / "nope.csv"
    p.write_text("Name,Price\nfoo,1\n")
    with pytest.raises(CollectionError, match="Send via Email"):
        read_csv(p)
