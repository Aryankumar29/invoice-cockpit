"""Node 6 - Line matching & ledger: invoice lines to PO lines (incl. bundles, net of included tax),
remaining PO balance, and the duplicate check."""
from __future__ import annotations

from ..core.domain import Context, Invoice
from ..core.duplicates import check_duplicates
from ..core.engine import _jsonable, prepare
from ..core.rules import money
from ._shared import line_rows

STAGE, TITLE = "matching", "Line matching & ledger"


def run(inv: Invoice, po, vendor, ctx: Context):
    dup = check_duplicates(inv, vendor, ctx.history)
    dup_data = _jsonable(dup)
    if po is None:
        return "warn", "No PO, so lines cannot be matched.", {"duplicates": dup_data}, None
    work, tax, lines, ledger = prepare(inv, po, ctx.history)
    matched = sum(1 for m in lines if m.matched)
    remaining = ledger.po_total_cents - ledger.billed_cents
    data = {"line_matches": line_rows(work, po, lines), "tax_basis": tax, "ledger": {
        "po_total": ledger.po_total_cents, "billed_before": ledger.billed_cents, "remaining_before": remaining,
        "this_invoice": inv.total_cents, "prior_runs": ledger.prior_runs}, "duplicates": dup_data}
    msg = (f"{matched}/{len(lines)} line(s) matched to {po.po_number}; {money(remaining)} of "
           f"{money(ledger.po_total_cents)} remained before this invoice.")
    if tax["basis"] != "exclusive":
        msg += f" Prices include {money(tax['tax_cents'])} tax; lines compared net of it."
    for m in lines:
        if m.bundle:
            msg += f" Line {m.inv_index + 1} is a bundle of PO lines {', '.join(map(str, m.bundle))}."
        elif m.options:
            msg += f" Line {m.inv_index + 1} could be a bundle in more than one way; left for review."
    if dup.kind != "none":
        msg += f" Duplicate check: {dup.kind} match."
    return ("ok" if matched == len(lines) and dup.kind == "none" else "warn"), msg, data, None
