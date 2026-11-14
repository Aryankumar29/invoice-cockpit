"""Vendor resolution, PO resolution and the cumulative commitment ledger."""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import policy as P
from .domain import Invoice, PriorInvoice, PurchaseOrder, Vendor
from .normalize import norm_name, norm_po_ref, norm_tax_id, norm_text, ocr_key


@dataclass
class VendorMatch:
    status: str  # matched | ambiguous | unknown | tax_conflict
    vendor: Vendor | None = None
    score: float = 0.0
    method: str = ""
    candidates: list[tuple[str, float]] = field(default_factory=list)


def resolve_vendor(inv: Invoice, vendors: list[Vendor]) -> VendorMatch:
    name = norm_name(inv.vendor_name)
    scored = sorted(
        ((v, fuzz.token_sort_ratio(name, norm_name(v.name)) if name else 0.0) for v in vendors),
        key=lambda t: -t[1],
    )
    cands = [(v.id, round(s, 1)) for v, s in scored[:3]]
    tid = norm_tax_id(inv.vendor_tax_id)
    if tid:
        exact = [v for v in vendors if norm_tax_id(v.tax_id) == tid]
        if exact:
            return VendorMatch("matched", exact[0], 100.0, "tax_id", cands)
        if scored and scored[0][1] >= P.VENDOR_REVIEW_SCORE:
            # Name says vendor X, tax ID says someone else: hard gate, never a silent link.
            return VendorMatch("tax_conflict", scored[0][0], scored[0][1], "tax_id_conflict", cands)
        return VendorMatch("unknown", None, 0.0, "tax_id", cands)
    if not scored or not name:
        return VendorMatch("unknown", None, 0.0, "none", cands)
    top, second = scored[0][1], (scored[1][1] if len(scored) > 1 else 0.0)
    if top >= P.VENDOR_LINK_SCORE and top - second >= P.VENDOR_LINK_MARGIN:
        return VendorMatch("matched", scored[0][0], top, "name", cands)
    if top >= P.VENDOR_REVIEW_SCORE:
        return VendorMatch("ambiguous", scored[0][0], top, "name", cands)
    return VendorMatch("unknown", None, top, "name", cands)


@dataclass
class Ledger:
    po_total_cents: int
    billed_cents: int
    billed_by_line: dict[int, tuple[int, int]]  # line_no -> (qty_milli, amount_cents)
    prior_runs: list[str]

    @property
    def remaining_cents(self) -> int:
        return self.po_total_cents - self.billed_cents


def ledger_for(po: PurchaseOrder, history: list[PriorInvoice]) -> Ledger:
    by_line: dict[int, tuple[int, int]] = {}
    billed, runs = 0, []
    for h in history:
        if h.po_number != po.po_number or h.outcome not in P.CONSUMING_OUTCOMES:
            continue
        runs.append(h.run_id)
        for a in h.allocations:
            q, amt = by_line.get(a.po_line_no, (0, 0))
            by_line[a.po_line_no] = (q + a.qty_milli, amt + a.amount_cents)
            billed += a.amount_cents
    return Ledger(po.total_cents, billed, by_line, runs)


@dataclass
class POMatch:
    status: str  # exact | fuzzy | ambiguous | missing
    po: PurchaseOrder | None = None
    score: float = 0.0
    method: str = ""
    candidates: list[tuple[str, float]] = field(default_factory=list)


def _line_similarity(inv: Invoice, po: PurchaseOrder) -> float:
    if not inv.lines:
        return 0.0
    best = [
        max((fuzz.token_set_ratio(norm_text(il.description), norm_text(pl.description)) for pl in po.lines), default=0)
        for il in inv.lines
    ]
    return sum(best) / len(best) / 100


def resolve_po(inv: Invoice, vendor: Vendor | None, pos: dict[str, PurchaseOrder],
               history: list[PriorInvoice]) -> POMatch:
    ref = norm_po_ref(inv.po_reference)
    if ref:
        for po in pos.values():
            if norm_po_ref(po.po_number) == ref:
                return POMatch("exact", po, 1.0, "reference")
        hits = [po for po in pos.values() if ocr_key(po.po_number) == ocr_key(inv.po_reference)]
        if len(hits) == 1:
            return POMatch("fuzzy", hits[0], 0.95, "ocr_tolerant_reference")
    if vendor is None:
        return POMatch("missing", None, 0.0, "no_vendor")

    # No usable reference: score the vendor's open POs; weights 20/20/10/10 renormalised
    # over the components present (reference absent), minus a missing-reference penalty.
    scored = []
    for po in pos.values():
        if po.vendor_id != vendor.id or po.status != "open":
            continue
        remaining = ledger_for(po, history).remaining_cents
        total = inv.total_cents or 0
        amount = max(0.0, 1 - abs(total - remaining) / max(remaining, total, 1)) if total else 0.0
        dated = 1.0 if (po.issued is None or inv.invoice_date is None or inv.invoice_date >= po.issued) else 0.0
        s = (0.2 * 1.0 + 0.2 * amount + 0.1 * dated + 0.1 * _line_similarity(inv, po)) / 0.6
        scored.append((po, round(s - P.PO_MISSING_REF_PENALTY, 4)))
    scored.sort(key=lambda t: -t[1])
    cands = [(po.po_number, s) for po, s in scored[:3]]
    if not scored:
        return POMatch("missing", None, 0.0, "no_open_po", cands)
    top = scored[0][1]
    margin = top - (scored[1][1] if len(scored) > 1 else 0.0)
    if top >= P.PO_LINK_SCORE and margin >= P.PO_LINK_MARGIN:
        return POMatch("fuzzy", scored[0][0], top, "vendor_window", cands)
    if top >= P.PO_REVIEW_SCORE:
        return POMatch("ambiguous", scored[0][0], top, "vendor_window", cands)
    return POMatch("missing", None, top, "vendor_window", cands)
