"""Domain types. Money is integer cents, quantity is integer thousandths (D5)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Vendor:
    id: str
    name: str
    tax_id: str
    status: str = "active"
    bank_hash: str | None = None


@dataclass
class POLine:
    line_no: int
    description: str
    qty_milli: int
    unit_price_cents: int
    received_qty_milli: int
    sku: str | None = None

    @property
    def amount_cents(self) -> int:
        return self.qty_milli * self.unit_price_cents // 1000


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_id: str
    currency: str
    lines: list[POLine]
    status: str = "open"
    tax_rate_bp: int = 0
    issued: date | None = None

    @property
    def total_cents(self) -> int:
        return sum(l.amount_cents for l in self.lines)


@dataclass
class InvoiceLine:
    description: str
    qty_milli: int
    unit_price_cents: int
    amount_cents: int
    sku: str | None = None
    extra: list[dict] = field(default_factory=list)  # other columns of the line table: [{label, value}]


@dataclass
class Invoice:
    invoice_number: str | None
    invoice_date: date | None
    vendor_name: str | None
    currency: str | None
    total_cents: int | None
    lines: list[InvoiceLine] = field(default_factory=list)
    subtotal_cents: int | None = None
    tax_cents: int = 0
    freight_cents: int = 0
    due_date: date | None = None
    vendor_tax_id: str | None = None
    po_reference: str | None = None
    bank_hash: str | None = None
    file_hash: str | None = None
    readable: bool = True
    discount_cents: int = 0  # header discount, stored positive: lines - discount + tax + freight = total
    # Every other labelled value printed on the invoice (company-specific fields): [{label, value, section,
    # verified}]. Captured for the reviewer and for export; the rules never read them.
    other_fields: list[dict] = field(default_factory=list)
    tax_inclusive: bool = False  # the invoice states its prices already include tax ("incl. VAT")


@dataclass
class Allocation:
    po_line_no: int
    qty_milli: int
    amount_cents: int


@dataclass
class PriorInvoice:
    run_id: str
    vendor_id: str
    invoice_number: str | None
    invoice_date: date | None
    total_cents: int
    currency: str
    outcome: str
    file_hash: str | None = None
    po_number: str | None = None
    allocations: list[Allocation] = field(default_factory=list)
    line_descriptions: list[str] = field(default_factory=list)
    approved_on: date | None = None
    paid_on: date | None = None


@dataclass
class Context:
    vendors: list[Vendor]
    pos: dict[str, PurchaseOrder]
    history: list[PriorInvoice]
    today: date
