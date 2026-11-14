"""R01-R16. Pure functions over an Assessment. All rules always run (docs/DESIGN.md D11).

Precedence tiers: 0 document, 1 identity/payment risk, 2 obligation/quantity,
3 evidence/arithmetic, 4 commercial variance, 5 governance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import policy as P
from .domain import Context, Invoice
from .duplicates import DupResult
from .matching import LineMatch
from .resolve import Ledger, POMatch, VendorMatch


@dataclass
class Assessment:
    inv: Invoice
    ctx: Context
    vendor: VendorMatch
    po: POMatch
    lines: list[LineMatch]
    ledger: Ledger | None
    dup: DupResult
    # Tax-inclusive invoices: inv is the net-of-tax view the PO checks compare against, gross is the document
    # as printed (R03 checks its arithmetic), tax describes the basis (engine.tax_view).
    gross: Invoice | None = None
    tax: dict = field(default_factory=dict)


@dataclass
class RuleResult:
    rule: str
    title: str
    status: str  # pass | fail | na | cannot_run | error
    message: str
    tier: int | None = None
    outcome: str | None = None
    reason: str | None = None
    evidence: dict = field(default_factory=dict)


def _ok(rule, title, msg, **ev):
    return RuleResult(rule, title, "pass", msg, evidence=ev)


def _na(rule, title, msg):
    return RuleResult(rule, title, "na", msg)


def _fail(rule, title, msg, tier, outcome, reason, **ev):
    return RuleResult(rule, title, "fail", msg, tier, outcome, reason, ev)


def money(c: int | None) -> str:
    if c is None:
        return "n/a"
    return f"{'-' if c < 0 else ''}${abs(c) / 100:,.2f}"


def r01(a: Assessment):
    t = "Document readable"
    if not a.inv.readable:
        return _fail("R01", t, "The document could not be read by any extraction route.",
                     0, "needs_review", "DOCUMENT_UNREADABLE")
    return _ok("R01", t, "Document text was extracted.")


def r02(a: Assessment):
    t = "Required fields present"
    inv = a.inv
    missing = [k for k, v in [("invoice_number", inv.invoice_number), ("invoice_date", inv.invoice_date),
                              ("vendor_name", inv.vendor_name), ("total", inv.total_cents),
                              ("currency", inv.currency)] if v in (None, "")]
    if not missing:
        return _ok("R02", t, "All required header fields are present.")
    reason = "MISSING_INVOICE_NUMBER" if missing == ["invoice_number"] else "MISSING_FIELD"
    return _fail("R02", t, f"Missing: {', '.join(missing)}. The system will not invent a value.",
                 3, "needs_review", reason, missing=missing)


def r03(a: Assessment):
    t = "Totals reconcile"
    inv = a.gross or a.inv  # the arithmetic is the document's, as printed
    if inv.total_cents is None or not inv.lines:
        return RuleResult("R03", t, "cannot_run", "No total or no line items to reconcile.")
    bad_lines = []
    for i, l in enumerate(inv.lines):
        expect = l.qty_milli * l.unit_price_cents // 1000
        if abs(expect - l.amount_cents) > P.ARITH_TOL_CENTS:
            bad_lines.append({"line": i + 1, "computed": expect, "stated": l.amount_cents})
    basis = a.tax.get("basis", "exclusive")
    line_sum = sum(l.amount_cents for l in inv.lines)
    computed = line_sum - inv.discount_cents + (0 if basis != "exclusive" else inv.tax_cents) + inv.freight_cents
    diff = inv.total_cents - computed
    sub_diff = (inv.subtotal_cents - line_sum) if inv.subtotal_cents is not None else 0
    ev = dict(line_sum=line_sum, discount=inv.discount_cents, tax=inv.tax_cents, freight=inv.freight_cents,
              computed_total=computed, stated_total=inv.total_cents, difference=diff, tolerance=P.ARITH_TOL_CENTS,
              bad_lines=bad_lines, tax_basis=basis)
    formula = "Lines" + (" - discount" if inv.discount_cents else "") + (
        " (tax included) + freight" if basis != "exclusive" else " + tax + freight")
    note = {"inclusive": " Prices include tax, so tax is not added again.",
            "inclusive_inferred": f" The {money(inv.tax_cents)} tax is inside the prices: the stated total does not "
                                  f"add it, and the invoice reconciles only on that reading."}.get(basis, "")
    if bad_lines or abs(diff) > P.ARITH_TOL_CENTS or abs(sub_diff) > P.ARITH_TOL_CENTS:
        return _fail("R03", t, f"{formula} = {money(computed)}, invoice states {money(inv.total_cents)} "
                               f"(difference {money(diff)}, tolerance {money(P.ARITH_TOL_CENTS)}).",
                     3, "needs_review", "ARITHMETIC_MISMATCH", **ev)
    return _ok("R03", t, f"{formula} = {money(computed)}, matches the stated total." + note, **ev)


def r04(a: Assessment):
    t = "Vendor approved"
    vm = a.vendor
    ev = dict(method=vm.method, score=vm.score, candidates=vm.candidates)
    if vm.status == "tax_conflict":
        return _fail("R04", t, f"Name resembles {vm.vendor.name}, but the tax ID does not match the vendor master.",
                     1, "needs_review", "TAX_ID_CONFLICT", **ev)
    if vm.status in ("unknown", "ambiguous"):
        return _fail("R04", t, "Vendor is not in the approved vendor master or cannot be identified uniquely.",
                     5, "needs_review", "UNKNOWN_VENDOR", **ev)
    if vm.vendor.status != "active":
        return _fail("R04", t, f"{vm.vendor.name} is {vm.vendor.status}.", 1, "needs_review", "VENDOR_INACTIVE", **ev)
    return _ok("R04", t, f"{vm.vendor.name} matched by {vm.method}; status active.", vendor_id=vm.vendor.id, **ev)


def r05(a: Assessment):
    t = "Bank details verified"
    v = a.vendor.vendor if a.vendor.status == "matched" else None
    if v is None:
        return _na("R05", t, "No resolved vendor to compare bank details against.")
    if not a.inv.bank_hash:
        return _ok("R05", t, "Invoice carries no remittance details; vendor master account applies.")
    if a.inv.bank_hash == v.bank_hash:
        return _ok("R05", t, "Remittance account matches the vendor master.")
    return _fail("R05", t, "Remittance account differs from the vendor master. Verify out-of-band before payment.",
                 1, "needs_review", "BANK_CHANGE", dual_control=True)


def r06(a: Assessment):
    t = "PO exists and is open"
    pm = a.po
    ev = dict(method=pm.method, score=pm.score, candidates=pm.candidates)
    if pm.status == "missing":
        return _fail("R06", t, "No purchase order matches this invoice" + ("" if a.inv.po_reference else
                                    " and it quotes no PO number") + "; it needs a PO or a budget owner's approval.",
                     2, "needs_review", "PO_MISSING", **ev)
    if pm.status == "ambiguous":
        ref = "The invoice quotes no PO number" if not a.inv.po_reference else f"PO {a.inv.po_reference} on the invoice was not found"
        return _fail("R06", t, f"{ref}. The closest match is {pm.po.po_number}, but it is not certain enough to link "
                               f"automatically (match {pm.score:.0%}); confirm the PO.",
                     2, "needs_review", "PO_AMBIGUOUS", **ev)
    if pm.po.status != "open":
        return _fail("R06", t, f"{pm.po.po_number} is {pm.po.status}.", 1, "rejected", "PO_CLOSED", **ev)
    return _ok("R06", t, f"{pm.po.po_number} found by {pm.method}; status open.", po_number=pm.po.po_number, **ev)


def _po(a):
    return a.po.po if a.po.status in ("exact", "fuzzy") else None


def r07(a: Assessment):
    t = "PO vendor matches"
    po, v = _po(a), a.vendor.vendor if a.vendor.status == "matched" else None
    if po is None or v is None:
        return _na("R07", t, "Needs both a resolved vendor and a PO.")
    if po.vendor_id != v.id:
        return _fail("R07", t, f"{po.po_number} belongs to vendor {po.vendor_id}, invoice is from {v.id}.",
                     1, "rejected", "VENDOR_PO_MISMATCH")
    return _ok("R07", t, f"{po.po_number} was issued to {v.name}.")


def r08(a: Assessment):
    t = "Currency matches"
    po = _po(a)
    if po is None or not a.inv.currency:
        return _na("R08", t, "Needs a PO and an invoice currency.")
    if a.inv.currency.upper() != po.currency.upper():
        return _fail("R08", t, f"Invoice is in {a.inv.currency}, PO is in {po.currency}.",
                     1, "rejected", "CURRENCY_MISMATCH")
    return _ok("R08", t, f"Both in {po.currency}.")


def r09(a: Assessment):
    t = "Lines map to PO"
    if _po(a) is None:
        return _na("R09", t, "No PO to map lines against.")
    if not a.lines:
        return _fail("R09", t, "Invoice has no line items to verify.", 3, "needs_review", "LINE_UNVERIFIED")
    unmatched = [m for m in a.lines if not m.matched]
    mapping = [{"invoice_line": m.inv_index + 1, "po_line": m.po_line_no, "score": m.score}
               | ({"bundle": m.bundle} if m.bundle else {}) for m in a.lines]
    if unmatched:
        msg = f"Invoice line(s) {[m.inv_index + 1 for m in unmatched]} do not correspond to any PO line."
        for m in unmatched:
            if m.options:
                msg += (f" Line {m.inv_index + 1} looks like a bundle, but more than one set of open PO lines "
                        f"adds up to its amount (lines " + " or ".join("+".join(map(str, o)) for o in m.options)
                        + "); a reviewer must pick.")
        return _fail("R09", t, msg, 3, "needs_review", "LINE_UNVERIFIED", mapping=mapping)
    bundles = "".join(f" Line {m.inv_index + 1} is a bundle of PO lines {', '.join(map(str, m.bundle))} "
                      f"(their open balance adds up to its amount)." for m in a.lines if m.bundle)
    return _ok("R09", t, f"All {len(a.lines)} invoice lines mapped to PO lines." + bundles, mapping=mapping)


def _pairs(a):
    po = _po(a)
    by_no = {l.line_no: l for l in po.lines}
    return [(a.inv.lines[m.inv_index], by_no[m.po_line_no], m) for m in a.lines if m.po_line_no is not None]


def _bundles(a):
    return [(a.inv.lines[m.inv_index], m) for m in a.lines if m.bundle]


def r10(a: Assessment):
    t = "Price within line tolerance"
    if _po(a) is None or not a.lines:
        return _na("R10", t, "No mapped lines.")
    details, worst = [], None
    for il, pl, m in _pairs(a):
        expected = il.qty_milli * pl.unit_price_cents // 1000
        variance = il.amount_cents - expected
        allowed = P.line_allowance(expected)
        row = dict(invoice_line=m.inv_index + 1, po_line=pl.line_no, invoice_unit=il.unit_price_cents,
                   po_unit=pl.unit_price_cents, expected=expected, billed=il.amount_cents,
                   variance=variance, allowed=allowed, ok=abs(variance) <= allowed)
        details.append(row)
    for il, m in _bundles(a):
        expected = sum(p["expected_cents"] for p in m.parts)
        variance = il.amount_cents - expected
        allowed = P.line_allowance(expected)
        details.append(dict(invoice_line=m.inv_index + 1, po_line=m.bundle[0], po_lines=m.bundle, bundle=True,
                            invoice_unit=il.unit_price_cents, po_unit=expected, expected=expected,
                            billed=il.amount_cents, variance=variance, allowed=allowed, ok=abs(variance) <= allowed))
    for row in details:
        if not row["ok"] and (worst is None or abs(row["variance"]) - row["allowed"]
                              > abs(worst["variance"]) - worst["allowed"]):
            worst = row
    if worst:
        side = "above" if worst["variance"] > 0 else "below"
        return _fail("R10", t, f"Line {worst['invoice_line']} is {money(abs(worst['variance']))} {side} the PO line; "
                               f"the allowed variance is {money(worst['allowed'])}.",
                     4, "needs_review", "PRICE_VARIANCE", lines=details, worst=worst,
                     formula="min(2% of expected line amount, $50.00)")
    net = " Prices include tax, so each line is compared net of tax." if a.tax.get("basis", "exclusive") != "exclusive" else ""
    return _ok("R10", t, "Every line is within min(2%, $50) of the PO price." + net, lines=details)


def r11(a: Assessment):
    t = "Quantity within receipt"
    if _po(a) is None or not a.lines or a.ledger is None:
        return _na("R11", t, "No mapped lines.")
    this: dict[int, int] = {}
    for il, pl, _ in _pairs(a):
        this[pl.line_no] = this.get(pl.line_no, 0) + il.qty_milli
    for _, m in _bundles(a):  # a bundle bills the open quantity of each line it covers
        for p in m.parts:
            this[p["po_line_no"]] = this.get(p["po_line_no"], 0) + p["qty_milli"]
    details, over = [], []
    for pl in _po(a).lines:
        if pl.line_no not in this:
            continue
        prior = a.ledger.billed_by_line.get(pl.line_no, (0, 0))[0]
        cum = prior + this[pl.line_no]
        cap = pl.received_qty_milli + P.RECEIPT_TOL_MILLI
        row = dict(po_line=pl.line_no, prior_billed_qty=prior, this_qty=this[pl.line_no], cumulative=cum,
                   received=pl.received_qty_milli, ordered=pl.qty_milli)
        details.append(row)
        if cum > cap:
            over.append(row)
    if over:
        o = over[0]
        return _fail("R11", t, f"PO line {o['po_line']}: cumulative billed {o['cumulative'] / 1000:g} exceeds "
                               f"received {o['received'] / 1000:g}.", 2, "needs_review", "QTY_VARIANCE", lines=details)
    return _ok("R11", t, "Cumulative billed quantity is within received quantity on every line.", lines=details)


def r12(a: Assessment):
    t = "Within PO remaining"
    po = _po(a)
    if po is None or a.ledger is None:
        return _na("R12", t, "No PO.")
    this = sum(l.amount_cents for l in a.inv.lines) if a.inv.lines else (a.inv.total_cents or 0)
    remaining = a.ledger.remaining_cents
    allowed = P.po_allowance(po.total_cents)
    after = remaining - this
    ev = dict(po_total=po.total_cents, billed_before=a.ledger.billed_cents, remaining_before=remaining,
              this_invoice=this, remaining_after=after, allowance=allowed, prior_runs=a.ledger.prior_runs,
              formula="max(2% of PO total, $50.00)")
    if this > remaining + allowed:
        return _fail("R12", t, f"Invoice {money(this)} exceeds the {money(remaining)} remaining on {po.po_number} "
                               f"by {money(this - remaining)}; allowance is {money(allowed)}.",
                     2, "rejected", "OVER_PO_REMAINING", **ev)
    final = " This is the final amount against the PO." if remaining > 0 and after <= 0 else ""
    return _ok("R12", t, f"{money(this)} against {money(remaining)} remaining; {money(max(after, 0))} left after."
               + final, **ev)


def r13(a: Assessment):
    t = "Not a duplicate"
    d = a.dup
    hits = [h.__dict__ for h in d.hits]
    if d.kind == "exact":
        h = d.hits[0]
        return _fail("R13", t, f"Same obligation as {h.invoice_number} (run {h.run_id}); matched on "
                               f"{', '.join(h.signals)}.", 1, "rejected", "DUPLICATE_INVOICE", hits=hits)
    if d.kind in ("near", "review"):
        h = d.hits[0]
        return _fail("R13", t, f"Resembles {h.invoice_number} (run {h.run_id}), similarity {h.score:.2f}.",
                     1, "needs_review", "DUPLICATE_SUSPECTED", hits=hits)
    if not d.ref_check_ran:
        return RuleResult("R13", t, "cannot_run",
                          "Invoice-number check cannot run without a number; file-hash and amount/date checks passed.")
    return _ok("R13", t, "No prior invoice matches by file, number, or amount/date/lines.")


def r14(a: Assessment):
    t = "Tax recalculates"
    po = _po(a)
    if po is None:
        return _na("R14", t, "No PO tax rate to recalculate against.")
    subtotal = sum(l.amount_cents for l in a.inv.lines)
    expected = (subtotal * po.tax_rate_bp + 5_000) // 10_000
    diff = a.inv.tax_cents - expected
    allowed = P.tax_allowance(a.inv.total_cents or 0)
    basis = a.tax.get("basis", "exclusive")
    ev = dict(rate_bp=po.tax_rate_bp, subtotal=subtotal, expected_tax=expected, stated_tax=a.inv.tax_cents,
              difference=diff, allowance=allowed, tax_basis=basis, tax_derived=a.tax.get("derived", False))
    where = " included in the prices" if basis != "exclusive" else ""
    if a.tax.get("derived"):
        where += " (not printed; derived from the gross prices at the PO rate)"
    if abs(diff) > allowed:
        return _fail("R14", t, f"Stated tax {money(a.inv.tax_cents)}{where}, expected {money(expected)} at "
                               f"{po.tax_rate_bp / 100:g}% of the net {money(subtotal)} (allowed {money(allowed)}).",
                     3, "needs_review", "TAX_VARIANCE", **ev)
    return _ok("R14", t, f"Tax {money(a.inv.tax_cents)}{where} matches {po.tax_rate_bp / 100:g}% of "
                         f"{money(subtotal)}.", **ev)


def r15(a: Assessment):
    t = "Within approval authority"
    total = a.inv.total_cents
    if total is None:
        return RuleResult("R15", t, "cannot_run", "No total.")
    approvers = 2 if total > P.DUAL_APPROVAL_CENTS else 1
    if total > P.AUTO_APPROVE_CAP_CENTS:
        return _fail("R15", t, f"{money(total)} is above the {money(P.AUTO_APPROVE_CAP_CENTS)} auto-approval cap; "
                               f"{approvers} approver(s) required.", 5, "needs_review", "APPROVAL_REQUIRED",
                     approvers=approvers, dual_control=approvers >= 2)
    return _ok("R15", t, f"{money(total)} is within the auto-approval cap.")


def r16(a: Assessment):
    t = "Due date valid"
    inv = a.inv
    if inv.invoice_date is None:
        return _na("R16", t, "No invoice date.")
    age = (a.ctx.today - inv.invoice_date).days
    if inv.due_date and inv.due_date < inv.invoice_date:
        return _fail("R16", t, "Due date is before the invoice date.", 5, "needs_review", "DUE_DATE_INVALID")
    if age > P.STALE_INVOICE_DAYS:
        return _fail("R16", t, f"Invoice is {age} days old (limit {P.STALE_INVOICE_DAYS}).",
                     5, "needs_review", "DUE_DATE_INVALID", age_days=age)
    if age < 0:
        return _fail("R16", t, "Invoice is dated in the future.", 5, "needs_review", "DUE_DATE_INVALID")
    return _ok("R16", t, f"Invoice dated {age} days ago; due date consistent.")


RULES: list[Callable[[Assessment], RuleResult]] = [r01, r02, r03, r04, r05, r06, r07, r08,
                                                   r09, r10, r11, r12, r13, r14, r15, r16]
# Auto-approval needs evidence, not just absence of failure: these must not be cannot_run.
MANDATORY = {"R01", "R02", "R03", "R04", "R06", "R09", "R10", "R11", "R12", "R13"}
