"""Write the sample PO book from the demo master data:
  fixtures/po_book.xlsx        upload to Google Drive and open as a Google Sheet, or point COCKPIT_PO_BOOK at it
  fixtures/po_book_csv/*.csv   the same three tabs as CSV
Usage: python scripts/make_po_book.py [out.xlsx]"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cockpit.core.seed import BANK_ACCOUNTS  # noqa: E402
from cockpit.sources import seed_records, to_tables  # noqa: E402
from cockpit.sources.files import write_csv_dir, write_xlsx  # noqa: E402

REQUESTERS = {"V-NW": "Priya Shah (Operations)", "V-GX": "Dan Ortiz (IT)", "V-AC": "Mei Lin (Maintenance)",
              "V-IN": "Sam Cole (Office)"}


def build() -> dict:
    rec = seed_records()
    tables = to_tables(rec, BANK_ACCOUNTS)
    for r in tables["PurchaseOrders"]:
        r["requester"] = REQUESTERS.get(r["vendor_id"], "")
    return tables


def main(out: Path) -> None:
    tables = build()
    write_xlsx(out, tables)
    write_csv_dir(out.parent / "po_book_csv", tables)
    print(f"Wrote {out} and {out.parent / 'po_book_csv'}: {len(tables['Vendors'])} vendors, "
          f"{len(tables['PurchaseOrders'])} POs, {len(tables['POLines'])} lines.")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "po_book.xlsx")
