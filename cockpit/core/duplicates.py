"""Two-tier duplicate detection (docs/DESIGN.md D10)."""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import policy as P
from .domain import Invoice, PriorInvoice, Vendor
from .normalize import invoice_key_loose, invoice_key_numeric, norm_po_ref, norm_text


@dataclass
class DupHit:
    run_id: str
    invoice_number: str | None
    score: float
    signals: list[str]


@dataclass
class DupResult:
    kind: str  # exact | near | review | none
    ref_check_ran: bool
    hits: list[DupHit] = field(default_factory=list)


def _near_score(inv: Invoice, vendor: Vendor | None, h: PriorInvoice) -> tuple[float, list[str]]:
    comps: list[tuple[float, float]] = []
    strong: list[str] = []
    total = inv.total_cents or 0
    if total:
        diff = abs(total - h.total_cents) / max(abs(total), 1)
        amt = 1.0 if diff == 0 else max(0.0, 1 - diff * 10)
        comps.append((0.30, amt))
        if diff == 0:
            strong.append("same_amount")
    if inv.invoice_number:
        # Numeric-key equality only. A fuzzy ratio would score INV-1001 vs INV-1002 as similar
        # and flag every recurring monthly bill.
        same = invoice_key_numeric(inv.invoice_number) == invoice_key_numeric(h.invoice_number)
        comps.append((0.25, 1.0 if same else 0.0))
        if same:
            strong.append("same_reference_digits")
    same_vendor = vendor is not None and vendor.id == h.vendor_id
    comps.append((0.20, 1.0 if same_vendor else 0.0))
    if same_vendor:
        strong.append("same_vendor")
    if inv.invoice_date and h.invoice_date:
        days = abs((inv.invoice_date - h.invoice_date).days)
        comps.append((0.10, max(0.0, 1 - days / 30)))
        if days <= 3:
            strong.append("same_date")
    if inv.lines and h.line_descriptions:
        a = " ".join(norm_text(l.description) for l in inv.lines)
        b = " ".join(norm_text(d) for d in h.line_descriptions)
        comps.append((0.15, fuzz.token_set_ratio(a, b) / 100))
    w = sum(c[0] for c in comps)
    return (round(sum(c[0] * c[1] for c in comps) / w, 4) if w else 0.0), strong


def check_duplicates(inv: Invoice, vendor: Vendor | None, history: list[PriorInvoice]) -> DupResult:
    live = [h for h in history if h.outcome not in {"rejected", "failed", "returned"}]
    exact: list[DupHit] = []
    for h in live:
        if inv.file_hash and h.file_hash == inv.file_hash:
            exact.append(DupHit(h.run_id, h.invoice_number, 1.0, ["file_hash"]))
        elif (vendor and inv.invoice_number and h.vendor_id == vendor.id
              and invoice_key_loose(inv.invoice_number) == invoice_key_loose(h.invoice_number)
              and (inv.currency or "") == h.currency):
            exact.append(DupHit(h.run_id, h.invoice_number, 1.0, ["vendor", "invoice_number", "currency"]))
    ref_ran = bool(inv.invoice_number and vendor)
    if exact:
        return DupResult("exact", ref_ran, exact)

    near, review = [], []
    for h in live:
        # Counter-signal: billed against a different PO with a different number is a different
        # obligation. Same-number resubmissions against another PO are caught by the exact check.
        if (inv.po_reference and h.po_number and norm_po_ref(inv.po_reference) != norm_po_ref(h.po_number)
                and invoice_key_numeric(inv.invoice_number) != invoice_key_numeric(h.invoice_number)):
            continue
        s, strong = _near_score(inv, vendor, h)
        if s >= P.DUP_NEAR_SCORE and len(strong) >= 2:
            near.append(DupHit(h.run_id, h.invoice_number, s, strong))
        elif s >= P.DUP_REVIEW_SCORE:
            review.append(DupHit(h.run_id, h.invoice_number, s, strong))
    if near:
        return DupResult("near", ref_ran, sorted(near, key=lambda d: -d.score))
    if review:
        return DupResult("review", ref_ran, sorted(review, key=lambda d: -d.score))
    return DupResult("none", ref_ran)
