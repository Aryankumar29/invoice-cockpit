"""Local PO book readers and the sample-book writer: an .xlsx workbook or a folder of three CSV files."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from .book import REQUIRED, TABS, BookError

CSV_NAMES = {"Vendors": "vendors.csv", "PurchaseOrders": "purchase_orders.csv", "POLines": "po_lines.csv"}

GUIDE = [
    ("PO book: how this spreadsheet is read", ""),
    ("", ""),
    ("Tabs", "Vendors, PurchaseOrders and POLines. Keep the tab names and the header row exactly as they are."),
    ("One row per", "Vendors: one vendor. PurchaseOrders: one PO. POLines: one line of a PO."),
    ("Money", "Plain numbers, no currency sign: 100.00. The currency is on the PurchaseOrders tab."),
    ("Quantities", "Plain numbers; decimals allowed (2.5 hours)."),
    ("Dates", "YYYY-MM-DD, e.g. 2026-10-01."),
    ("Tax", "tax_rate_pct on the PO, e.g. 8.25 for 8.25%. Leave 0 for tax-exempt."),
    ("Received", "qty_received is what the warehouse booked in (goods receipt). Invoices are paid only up to it."),
    ("Status", "PO status: open or closed. Vendor status: active or inactive."),
    ("Bank account", "Format the column as plain text so leading zeros are kept. The cockpit stores only a "
                     "fingerprint of it and flags any invoice whose remittance account differs."),
    ("", ""),
    ("Matching keys", "An email or invoice is tied to a PO when at least two of these agree, one of them the vendor:"),
    ("  1. PO number", "po_number appears in the email subject, body or the attached invoice."),
    ("  2. Vendor", "the sender's address ends in the vendor's email_domain, or the vendor_name is on the invoice; "
                    "and that vendor is the one the PO was issued to."),
    ("  3. Buyer", "buyer_company (our legal entity on the PO) appears as the bill-to on the invoice."),
    ("", ""),
    ("Amounts", "line_amount (POLines) and po_total (PurchaseOrders) are calculated by formulas. Do not type "
                "over them; the cockpit calculates the same amounts itself."),
    ("Bad rows", "A row with a missing or invalid required value is skipped and listed on the PO book page; "
                 "it is never guessed."),
    ("Reload", "The cockpit re-reads this file when it changes (Google Sheets: every minute)."),
]


def read_xlsx(path: Path) -> dict[str, list[dict]]:
    from openpyxl import load_workbook
    # from memory: read-only worksheets keep their own handle on the file, which Windows then cannot delete
    wb = load_workbook(io.BytesIO(Path(path).read_bytes()), read_only=True, data_only=True)
    try:
        out = {}
        for tab in TABS:
            if tab not in wb.sheetnames:
                raise BookError(f"{path.name} has no '{tab}' tab. Keep the three tabs of the template: "
                                f"{', '.join(TABS)}.")
            it = wb[tab].iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(it, [])]
            missing = [c for c in REQUIRED[tab] if c not in [h.lower() for h in header]]
            if missing:
                raise BookError(f"The '{tab}' tab is missing the column(s): {', '.join(missing)}. "
                                f"Keep the header row of the template.")
            out[tab] = [dict(zip(header, row)) for row in it]
        return out
    finally:
        wb.close()


def read_csv_dir(path: Path) -> dict[str, list[dict]]:
    out = {}
    for tab, name in CSV_NAMES.items():
        f = path / name
        if not f.is_file():
            raise BookError(f"{path} has no {name}.")
        out[tab] = list(csv.DictReader(io.StringIO(f.read_text(encoding="utf-8-sig"))))
    return out


def read_book(path: Path) -> dict[str, list[dict]]:
    if path.is_dir():
        return read_csv_dir(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return read_xlsx(path)
    raise BookError(f"{path.name}: use an .xlsx workbook or a folder of CSV files.")


def write_xlsx(path: Path, tables: dict[str, list[dict]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    guide = wb.active
    guide.title = "Guide"
    for row in GUIDE:
        guide.append(row)
    guide["A1"].font = Font(bold=True, size=14)
    guide.column_dimensions["A"].width = 22
    guide.column_dimensions["B"].width = 110
    for c in guide["B"]:
        c.alignment = Alignment(wrap_text=True, vertical="top")
    head = PatternFill("solid", fgColor="14213D")
    for tab, cols in TABS.items():
        ws = wb.create_sheet(tab)
        ws.append(cols)
        for cell in ws[1]:
            cell.font, cell.fill = Font(bold=True, color="FFFFFF"), head
        for r in tables.get(tab, []):
            ws.append([r.get(c, "") for c in cols])
        for i, c in enumerate(cols, start=1):
            letter = ws.cell(1, i).column_letter
            ws.column_dimensions[letter].width = max(12, min(40, max(len(str(x.value or "")) for x in ws[letter]) + 2))
            if c in ("bank_account", "tax_id", "po_number", "sku", "vendor_id"):
                for cell in ws[letter][1:]:
                    cell.number_format = "@"
        ws.freeze_panes = "A2"
    _add_amounts(wb, head)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _add_amounts(wb, head) -> None:
    """Calculated columns: line_amount = qty_ordered x unit_price, po_total = sum of its lines.
    They are formulas, recalculated by Excel or Google Sheets, and the cockpit ignores them when reading."""
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    lines, pos = wb["POLines"], wb["PurchaseOrders"]
    col = {c.value: c.column for c in lines[1]}
    q, u, n = (get_column_letter(col[k]) for k in ("qty_ordered", "unit_price", "po_number"))
    la = get_column_letter(lines.max_column + 1)
    lines[f"{la}1"] = "line_amount"
    for r in range(2, max(lines.max_row, 2) + 200):
        lines[f"{la}{r}"] = f'=IF({q}{r}="","",{q}{r}*{u}{r})'
        lines[f"{la}{r}"].number_format = "#,##0.00"
    pt = get_column_letter(pos.max_column + 1)
    pos[f"{pt}1"] = "po_total"
    for r in range(2, max(pos.max_row, 2) + 200):
        pos[f"{pt}{r}"] = f'=IF(A{r}="","",SUMIF(POLines!{n}:{n},A{r},POLines!{la}:{la}))'
        pos[f"{pt}{r}"].number_format = "#,##0.00"
    for ws, letter in ((lines, la), (pos, pt)):
        ws[f"{letter}1"].font, ws[f"{letter}1"].fill = Font(bold=True, color="FFFFFF"), head
        ws.column_dimensions[letter].width = 14


def blank_tables() -> dict[str, list[dict]]:
    """One example row per tab, so a new user sees the format. Replace them with real data."""
    return {
        "Vendors": [{"vendor_id": "V-001", "vendor_name": "Example Supplies Ltd", "tax_id": "12-3456789",
                     "status": "active", "billing_email": "billing@example-supplies.com",
                     "email_domain": "example-supplies.com", "bank_account": "0001234567"}],
        "PurchaseOrders": [{"po_number": "PO-0001", "vendor_id": "V-001", "vendor_name": "Example Supplies Ltd",
                            "buyer_company": "Your Company Inc.", "currency": "USD", "status": "open",
                            "issued_on": "2026-10-01", "tax_rate_pct": "0", "requester": "Jane Doe"}],
        "POLines": [{"po_number": "PO-0001", "line_no": "1", "sku": "ITEM-01", "description": "Example item",
                     "qty_ordered": "10", "unit_price": "25.00", "qty_received": "10"}],
    }


def write_csv_dir(path: Path, tables: dict[str, list[dict]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for tab, name in CSV_NAMES.items():
        with (path / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TABS[tab])
            w.writeheader()
            w.writerows(tables.get(tab, []))
