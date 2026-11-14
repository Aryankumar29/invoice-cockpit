"""Two demo invoices in a layout none of the test invoices use, written to ../demo-invoices:
  12_new-layout_normal_Globex_GX-2026-0412.pdf   clean invoice against PO-1047 (expected: auto-approved)
  13_new-layout_no-PO-number_Initech_IN-7702.pdf  no PO number printed; the PO must be found from vendor + items
Usage: python scripts/make_demo_invoices.py"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[2] / "demo-invoices"
W, H = A4
M = 50


def money(c: int) -> str:
    return f"${c / 100:,.2f}"


def draw(path: Path, *, vendor: str, address: list[str], tax_id: str, number: str, date: str, due: str,
         po: str | None, lines: list[tuple[str, str, int, int]], tax_rate: float, tax_label: str, notes: list[str]):
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle(f"Invoice {number}")
    # left accent column, vendor block
    c.setFillColor(colors.HexColor("#0E3B43"))
    c.rect(0, 0, 18, H, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(M, H - 64, vendor)
    c.setFont("Helvetica", 9)
    for i, s in enumerate(address + [f"Tax ID: {tax_id}"]):
        c.drawString(M, H - 80 - i * 12, s)
    # title and meta box on the right
    c.setFont("Helvetica-Bold", 30)
    c.drawRightString(W - M, H - 70, "INVOICE")
    meta = [("Invoice Number", number), ("Invoice Date", date), ("Due Date", due)]
    if po:
        meta.append(("PO Number", po))
    c.setFont("Helvetica", 9.5)
    y = H - 96
    for k, v in meta:
        c.drawRightString(W - M - 110, y, f"{k}:")
        c.drawRightString(W - M, y, v)
        y -= 14
    # bill to
    y = H - 190
    c.setFont("Helvetica-Bold", 9)
    c.drawString(M, y, "BILL TO")
    c.setFont("Helvetica", 9.5)
    for i, s in enumerate(["Demo Buyer Corp", "Attn: Accounts Payable", "500 Market Street, Floor 9",
                           "San Francisco, CA 94105"]):
        c.drawString(M, y - 14 - i * 12, s)
    # line table
    y = H - 290
    cols = [M, M + 70, W - M - 200, W - M - 120, W - M]
    c.setFillColor(colors.HexColor("#E8F0F1"))
    c.rect(M - 4, y - 6, W - 2 * M + 8, 20, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(cols[0], y, "Item")
    c.drawString(cols[1], y, "Description")
    c.drawRightString(cols[2] + 40, y, "Qty")
    c.drawRightString(cols[3] + 40, y, "Unit Price")
    c.drawRightString(cols[4], y, "Amount")
    c.setFont("Helvetica", 9.5)
    subtotal = 0
    for sku, desc, qty, unit in lines:
        y -= 22
        amount = qty * unit
        subtotal += amount
        c.drawString(cols[0], y, sku)
        c.drawString(cols[1], y, desc)
        c.drawRightString(cols[2] + 40, y, str(qty))
        c.drawRightString(cols[3] + 40, y, money(unit))
        c.drawRightString(cols[4], y, money(amount))
    c.setStrokeColor(colors.HexColor("#BBBBBB"))
    c.line(M, y - 12, W - M, y - 12)
    # totals
    tax = round(subtotal * tax_rate)
    y -= 34
    c.setFont("Helvetica", 9.5)
    for k, v in (("Subtotal", money(subtotal)), (tax_label, money(tax))):
        c.drawRightString(W - M - 110, y, k)
        c.drawRightString(W - M, y, v)
        y -= 15
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(W - M - 110, y - 4, "Total Due (USD)")
    c.drawRightString(W - M, y - 4, money(subtotal + tax))
    # notes
    c.setFont("Helvetica", 8.5)
    c.setFillColor(colors.HexColor("#555555"))
    for i, s in enumerate(notes):
        c.drawString(M, 90 - i * 12, s)
    c.showPage()
    c.save()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    draw(OUT / "12_new-layout_normal_Globex_GX-2026-0412.pdf",
         vendor="Globex Services LLC", address=["1200 Harbor Blvd, Suite 400", "New York, NY 10004",
                                                 "ar@globex-services.example"],
         tax_id="36-7654321", number="GX-2026-0412", date="October 22, 2026", due="November 21, 2026",
         po="PO-1047", lines=[("SVC-DAY", "Implementation consulting, sprint 3 (5 days)", 5, 100_000)],
         tax_rate=0.0, tax_label="Tax (services, exempt)",
         notes=["Payment terms: Net 30. Please quote the invoice number with your payment.",
                "Thank you for your business."])
    draw(OUT / "13_new-layout_no-PO-number_Initech_IN-7702.pdf",
         vendor="Initech Office Supplies", address=["88 Commerce Park", "Austin, TX 78701",
                                                     "billing@initech-office.example"],
         tax_id="51-2223334", number="IN-7702", date="October 23, 2026", due="November 22, 2026",
         po=None, lines=[("CP-A4", "Copy paper A4, case (first delivery)", 30, 4_000)],
         tax_rate=0.0825, tax_label="Sales Tax 8.25%",
         notes=["Order placed by Sam Cole (Office). Remaining 20 cases ship next week.",
                "Payment terms: Net 30."])
    print(f"Wrote 2 invoices to {OUT}")


if __name__ == "__main__":
    main()
