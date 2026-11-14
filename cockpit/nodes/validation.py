"""Node 3 - Validation: required fields and tax-basis-aware arithmetic (lines - discount + tax + freight = total)."""
from __future__ import annotations

from ..core import policy as P
from ..core.domain import Invoice
from ..core.engine import tax_basis
from ..core.rules import money

STAGE, TITLE = "validation", "Normalize & validate"
_REQUIRED = ("invoice_number", "invoice_date", "vendor_name", "currency", "total_cents")


def run(inv: Invoice):
    missing = [k for k in _REQUIRED if getattr(inv, k) in (None, "")]
    lines_sum = sum(l.amount_cents for l in inv.lines)
    basis = tax_basis(inv)
    added_tax = inv.tax_cents if basis == "exclusive" else 0
    computed = lines_sum - inv.discount_cents + added_tax + inv.freight_cents
    diff = (inv.total_cents - computed) if inv.total_cents is not None else None
    data = {"missing": missing, "lines_sum": lines_sum, "computed_total": computed,
            "stated_total": inv.total_cents, "difference": diff, "tax_basis": basis}
    problems = []
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    if diff is not None and abs(diff) > P.ARITH_TOL_CENTS:
        problems.append(f"lines + tax + freight = {money(computed)} but total is {money(inv.total_cents)}")
    if problems:
        return "warn", "Validation issues: " + "; ".join(problems) + ". Values are kept as read.", data, None
    incl = {"inclusive": " Prices include tax.",
            "inclusive_inferred": " Tax is inside the prices (the total does not add it)."}.get(basis, "")
    return "ok", f"All required fields present; arithmetic reconciles ({money(computed)}).{incl}", data, None
