"""Demo master data and the expected extraction for each demo fixture (source of fixtures/manifest.json)."""
from __future__ import annotations

import copy
from datetime import date

from .domain import Allocation, Context, Invoice, InvoiceLine, POLine, PriorInvoice, PurchaseOrder, Vendor
from .normalize import bank_fingerprint

TODAY = date(2026, 10, 26)
Q = 1000  # one unit in qty_milli


# Remittance account numbers printed on the fixture PDFs; the vendor master stores only the fingerprint.
BANK_ACCOUNTS = {"V-NW": "0000007781", "V-GX": "0000002210", "V-AC": "0000009034", "V-IN": "0000004410"}


def vendors() -> list[Vendor]:
    fp = lambda vid: bank_fingerprint(BANK_ACCOUNTS[vid])
    return [
        Vendor("V-NW", "Northwind Supply Co.", "94-1234567", bank_hash=fp("V-NW")),
        Vendor("V-GX", "Globex Services LLC", "36-7654321", bank_hash=fp("V-GX")),
        Vendor("V-AC", "Acme Industrial Inc.", "27-1111111", bank_hash=fp("V-AC")),
        Vendor("V-IN", "Initech Office Supplies", "51-2223334", bank_hash=fp("V-IN")),
    ]


def vendors_by_id() -> dict[str, Vendor]:
    return {v.id: v for v in vendors()}


def purchase_orders() -> dict[str, PurchaseOrder]:
    pos = [
        PurchaseOrder("PO-1042", "V-NW", "USD", [
            POLine(1, "Widget A, steel, 10 mm", 20 * Q, 10_000, 20 * Q, "WA-10"),
            POLine(2, "Mounting bracket B", 40 * Q, 4_500, 40 * Q, "MB-02"),
            POLine(3, "Fastener kit, 100 pc", 10 * Q, 10_000, 10 * Q, "FK-100"),
        ], issued=date(2026, 10, 1)),
        PurchaseOrder("PO-1051", "V-NW", "USD", [
            POLine(1, "Widget A, steel, 10 mm", 40 * Q, 10_000, 40 * Q, "WA-10"),
            POLine(2, "Widget B, aluminium, 12 mm", 20 * Q, 10_000, 20 * Q, "WB-12"),
        ], issued=date(2026, 10, 5)),
        PurchaseOrder("PO-1047", "V-GX", "USD", [
            POLine(1, "Implementation consulting (day rate)", 10 * Q, 100_000, 10 * Q, "SVC-DAY"),
        ], issued=date(2026, 9, 20)),
        PurchaseOrder("PO-1046", "V-AC", "USD", [
            POLine(1, "Hydraulic hose assembly 1/2in", 30 * Q, 10_000, 30 * Q, "HH-050"),
        ], issued=date(2026, 10, 2)),
        PurchaseOrder("PO-1038", "V-NW", "USD", [
            POLine(1, "Widget A, steel, 10 mm", 30 * Q, 10_000, 30 * Q, "WA-10"),
            POLine(2, "Fastener kit, 100 pc", 18 * Q, 10_000, 18 * Q, "FK-100"),
        ], issued=date(2026, 9, 15)),
        PurchaseOrder("PO-1029", "V-NW", "USD", [
            POLine(1, "Widget A, steel, 10 mm", 10 * Q, 10_000, 10 * Q, "WA-10"),
        ], status="closed", issued=date(2026, 8, 1)),
        PurchaseOrder("PO-1060", "V-IN", "USD", [
            POLine(1, "Copy paper A4, case", 50 * Q, 4_000, 50 * Q, "CP-A4"),
        ], tax_rate_bp=825, issued=date(2026, 10, 10)),
        PurchaseOrder("PO-1070", "V-AC", "EUR", [
            POLine(1, "Hydraulic hose assembly 1/2in", 10 * Q, 10_000, 10 * Q, "HH-050"),
        ], issued=date(2026, 10, 10)),
        PurchaseOrder("PO-1080", "V-GX", "USD", [
            POLine(1, "Implementation consulting (day rate)", 15 * Q, 100_000, 15 * Q, "SVC-DAY"),
        ], issued=date(2026, 10, 1)),
        # R-INCL: priced net; Initech bills tax-inclusive prices.
        PurchaseOrder("PO-1090", "V-IN", "USD", [
            POLine(1, "Ergonomic office chair", 12 * Q, 20_000, 12 * Q, "CH-ERG"),
        ], tax_rate_bp=825, issued=date(2026, 10, 12)),
        # R-BUNDLE: Northwind bills lines 1-3 as one workstation kit.
        PurchaseOrder("PO-1095", "V-NW", "USD", [
            POLine(1, "Widget A, steel, 10 mm", 10 * Q, 10_000, 10 * Q, "WA-10"),
            POLine(2, "Mounting bracket B", 20 * Q, 4_500, 20 * Q, "MB-02"),
            POLine(3, "Fastener kit, 100 pc", 5 * Q, 10_000, 5 * Q, "FK-100"),
            POLine(4, "Installation labour (hours)", 8 * Q, 7_500, 8 * Q, "SVC-INST"),
        ], issued=date(2026, 10, 12)),
    ]
    return {p.po_number: p for p in pos}


def history() -> list[PriorInvoice]:
    """NW-2026-001 is the original that E1 duplicates: approved 12 Oct, paid 18 Oct."""
    return [
        PriorInvoice("run-0007", "V-NW", "NW-2026-001", date(2026, 10, 9), 480_000, "USD", "approved",
                     file_hash="sha-original-nw-2026-001", po_number="PO-1038",
                     allocations=[Allocation(1, 30 * Q, 300_000), Allocation(2, 18 * Q, 180_000)],
                     line_descriptions=["Widget A, steel, 10 mm", "Fastener kit, 100 pc"],
                     approved_on=date(2026, 10, 12), paid_on=date(2026, 10, 18)),
    ]


def context() -> Context:
    return Context(vendors(), purchase_orders(), history(), TODAY)


def _line(desc, qty, unit, sku=None, amount=None):
    return InvoiceLine(desc, qty * Q, unit, amount if amount is not None else qty * unit, sku)


def _inv(number, vendor, tax_id, po, lines, *, d=date(2026, 10, 20), tax=0, freight=0, total=None, **kw):
    sub = sum(l.amount_cents for l in lines)
    return Invoice(number, d, vendor, "USD", total if total is not None else sub + tax + freight, lines,
                   subtotal_cents=sub, tax_cents=tax, freight_cents=freight, due_date=date(2026, 11, 19),
                   vendor_tax_id=tax_id, po_reference=po, file_hash=f"sha-{number or 'noref'}-{po}", **kw)


FIXTURES: dict[str, Invoice] = {
    "H1": _inv("INV-H-001", "Northwind Supply Co.", "94-1234567", "PO-1042", [
        _line("Widget A, steel, 10 mm", 20, 10_000, "WA-10"),
        _line("Mounting bracket B", 40, 4_500, "MB-02"),
        _line("Fastener kit, 100 pc", 10, 10_000, "FK-100"),
    ]),
    "S1": _inv("INV-S-001", "Globex Services LLC", "36-7654321", "PO-1047", [
        _line("Implementation consulting - phase 1 (6 days)", 6, 100_000, "SVC-DAY"),
    ], d=date(2026, 10, 10)),
    "S2": _inv("INV-S-002", "Globex Services LLC", "36-7654321", "PO-1047", [
        _line("Implementation consulting - phase 2 (4 days)", 4, 100_000, "SVC-DAY"),
    ], d=date(2026, 10, 22)),
    "D1": _inv("INV-D-001", "Northwind Supply Co.", "94-1234567", "PO-1051", [
        _line("Widget A, steel, 10 mm", 40, 10_000, "WA-10"),
        _line("Widget B, aluminium, 12 mm", 20, 10_300, "WB-12"),
    ]),
    # E1: altered template of NW-2026-001 (different layout, so a different file hash).
    "E1": _inv("NW-2026-001", "Northwind Supply Co", "94-1234567", "PO-1038", [
        _line("Widget A - steel 10mm", 30, 10_000, "WA-10"),
        _line("Fastener kit (100 pcs)", 18, 10_000, "FK-100"),
    ], d=date(2026, 10, 9)),
    # E2: scanned, no invoice number. Vendor, PO and total readable.
    "E2": _inv(None, "Acme Industrial Inc.", "27-1111111", "PO-1046", [
        _line("Hydraulic hose assembly 1/2in", 30, 10_000, "HH-050"),
    ], d=date(2026, 10, 21)),
}

EXPECTED = {
    "H1": ("auto_approved", "STP_CLEAN"),
    "S1": ("auto_approved", "STP_CLEAN"),
    "S2": ("auto_approved", "STP_CLEAN"),
    "D1": ("needs_review", "PRICE_VARIANCE"),
    "E1": ("rejected", "DUPLICATE_INVOICE"),
    "E2": ("needs_review", "MISSING_INVOICE_NUMBER"),
}


def fixture(key: str) -> Invoice:
    return copy.deepcopy(FIXTURES[key])
