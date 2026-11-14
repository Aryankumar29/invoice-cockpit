"""Node 5 - PO resolution: link the invoice to one purchase order (exact reference, then fuzzy fit)."""
from __future__ import annotations

from ..core.domain import Context, Invoice
from ..core.resolve import resolve_po

STAGE, TITLE = "po", "PO resolution"


def run(inv: Invoice, vendor, ctx: Context):
    pm = resolve_po(inv, vendor, ctx.pos, ctx.history)
    data = {"status": pm.status, "method": pm.method, "score": pm.score, "candidates": pm.candidates,
            "po_number": pm.po.po_number if pm.po else None, "reference_on_invoice": inv.po_reference}
    if pm.status in ("exact", "fuzzy"):
        return "ok", f"Linked to {pm.po.po_number} ({pm.method}, {pm.po.status}).", data, pm
    msg = {"ambiguous": "More than one open PO fits; a reviewer must pick.",
           "missing": "No PO could be linked."}.get(pm.status, pm.status)
    return "warn", msg, data, pm
