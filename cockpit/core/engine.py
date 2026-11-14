"""Run every rule, adjudicate by precedence, and build a hashable decision packet."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from . import policy as P
from .domain import Allocation, Context, Invoice, PriorInvoice
from .duplicates import check_duplicates
from .matching import match_lines
from .resolve import ledger_for, resolve_po, resolve_vendor
from .rules import MANDATORY, RULES, Assessment, RuleResult, money

_SEVERITY = {"auto_approved": 0, "approved": 1, "needs_review": 2, "rejected": 3, "failed": 4}

_ACTIONS = {
    "STP_CLEAN": "Release to payment queue",
    "PRICE_VARIANCE": "Request a credit note",
    "DUPLICATE_INVOICE": "Reject and link the original",
    "DUPLICATE_SUSPECTED": "Compare with the possible original",
    "MISSING_INVOICE_NUMBER": "Return to vendor",
    "MISSING_FIELD": "Return to vendor",
    "ARITHMETIC_MISMATCH": "Return to vendor",
    "DOCUMENT_UNREADABLE": "Return to vendor",
    "BANK_CHANGE": "Verify bank change out-of-band (dual control)",
    "TAX_ID_CONFLICT": "Escalate to vendor-master owner",
    "VENDOR_INACTIVE": "Escalate to vendor-master owner",
    "UNKNOWN_VENDOR": "Start vendor onboarding",
    "PO_MISSING": "Route to budget owner (non-PO)",
    "PO_AMBIGUOUS": "Pick the correct PO",
    "PO_CLOSED": "Reject: PO is closed",
    "VENDOR_PO_MISMATCH": "Reject: PO belongs to another vendor",
    "CURRENCY_MISMATCH": "Reject: currency differs from PO",
    "OVER_PO_REMAINING": "Reject or request a PO change order",
    "QTY_VARIANCE": "Hold until goods receipt is posted",
    "LINE_UNVERIFIED": "Map the unmatched line or request a credit",
    "TAX_VARIANCE": "Request a corrected invoice",
    "APPROVAL_REQUIRED": "Send to approver",
    "DUE_DATE_INVALID": "Confirm dates with vendor",
    "INCOMPLETE_EVIDENCE": "Review manually",
    "RULE_ERROR": "Review manually",
}


@dataclass
class Decision:
    outcome: str
    reasons: list[str]
    verdict: str
    recommended_action: str
    results: list[RuleResult]
    vendor_id: str | None
    po_number: str | None
    allocations: list[Allocation]
    policy_version: str = P.POLICY_VERSION
    packet: dict = field(default_factory=dict)
    packet_hash: str = ""
    work: Invoice | None = None  # the invoice the PO checks saw (net of tax when prices include it)
    tax: dict = field(default_factory=dict)

    def result(self, rule: str) -> RuleResult:
        return next(r for r in self.results if r.rule == rule)


def _run_rules(a: Assessment) -> list[RuleResult]:
    out = []
    for fn in RULES:
        try:
            out.append(fn(a))
        except Exception as e:  # a broken rule fails closed, never passes silently
            rid = fn.__name__.upper()
            out.append(RuleResult(rid, rid, "error", f"Rule raised {type(e).__name__}: {e}",
                                  0, "needs_review", "RULE_ERROR"))
    if not a.inv.readable:
        out = [r if r.rule == "R01" else RuleResult(r.rule, r.title, "cannot_run", "Document unreadable.")
               for r in out]
    return out


def adjudicate(results: list[RuleResult]) -> tuple[str, list[str], list[RuleResult]]:
    fails = [r for r in results if r.status in ("fail", "error")]
    if fails:
        # The most severe outcome wins, so a tier-1 review can never soften a tier-2 reject.
        # Tiers order the reasons: the highest-precedence control is named first.
        outcome = max((r.outcome for r in fails), key=_SEVERITY.__getitem__)
        deciding = sorted((r for r in fails if r.outcome == outcome), key=lambda r: (r.tier, r.rule))
        return outcome, list(dict.fromkeys(r.reason for r in deciding)), deciding
    gaps = [r for r in results if r.rule in MANDATORY and r.status == "cannot_run"]
    if gaps:
        return "needs_review", ["INCOMPLETE_EVIDENCE"], gaps
    return "auto_approved", ["STP_CLEAN"], []


def _jsonable(o):
    if dataclasses.is_dataclass(o):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(o).items()}
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, date):
        return o.isoformat()
    return o


def packet_hash(packet: dict) -> str:
    return hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def to_prior(inv: Invoice, d: Decision, run_id: str) -> PriorInvoice:
    """What the ledger and duplicate checks remember about a processed invoice."""
    return PriorInvoice(run_id, d.vendor_id or "", inv.invoice_number, inv.invoice_date, inv.total_cents or 0,
                        inv.currency or "", d.outcome, inv.file_hash, d.po_number, list(d.allocations),
                        [l.description for l in inv.lines])


def tax_basis(inv: Invoice) -> str:
    """exclusive | inclusive (the invoice says prices include tax) | inclusive_inferred (a tax amount is printed,
    the total does not add it, and the invoice reconciles only on the tax-included reading)."""
    if inv.tax_inclusive:
        return "inclusive"
    if inv.tax_cents and inv.total_cents is not None and inv.lines:
        excl = sum(l.amount_cents for l in inv.lines) - inv.discount_cents + inv.tax_cents + inv.freight_cents
        if abs(inv.total_cents - (excl - inv.tax_cents)) <= P.ARITH_TOL_CENTS < abs(inv.total_cents - excl):
            return "inclusive_inferred"
    return "exclusive"


def _share(amount: int, num: int, den: int) -> int:
    return (2 * amount * num + den) // (2 * den)  # amount * num / den, rounded half up


def tax_view(inv: Invoice, po) -> tuple[Invoice, dict]:
    """POs are priced net of tax. When the invoice prices include tax, the PO checks see a net view: every line
    scaled by (gross - tax) / gross. With no tax printed, the PO rate gives it. The printed document is unchanged."""
    basis = tax_basis(inv)
    info = {"basis": basis, "derived": False, "tax_cents": inv.tax_cents}
    if basis == "exclusive" or not inv.lines:
        return inv, info
    gross = sum(l.amount_cents for l in inv.lines) - inv.discount_cents
    tax = inv.tax_cents
    if not tax and po is not None and po.tax_rate_bp:
        tax = gross - _share(gross, 10_000, 10_000 + po.tax_rate_bp)
        info["derived"] = True
    info["tax_cents"] = tax
    if gross <= 0 or tax <= 0 or tax >= gross:
        return inv, info
    net = gross - tax
    lines = [dataclasses.replace(l, amount_cents=_share(l.amount_cents, net, gross)) for l in inv.lines]
    discount = _share(inv.discount_cents, net, gross)
    residual = net + discount - sum(l.amount_cents for l in lines)
    if residual:
        big = max(lines, key=lambda l: abs(l.amount_cents))
        big.amount_cents += residual
    for l in lines:
        l.unit_price_cents = _share(l.amount_cents, 1000, l.qty_milli) if l.qty_milli else l.amount_cents
    info.update(gross_cents=gross, net_cents=net)
    return dataclasses.replace(inv, lines=lines, tax_cents=tax, discount_cents=discount,
                               subtotal_cents=sum(l.amount_cents for l in lines)), info


def prepare(inv: Invoice, po, history: list[PriorInvoice]):
    """What the PO checks compare: (net view, tax basis, line matches, ledger). Shared with the live pipeline."""
    work, tax = tax_view(inv, po)
    ledger = ledger_for(po, history) if po else None
    lines = match_lines(work.lines, po.lines, ledger.billed_by_line) if po else []
    return work, tax, lines, ledger


def _allocations(work: Invoice, lines) -> list[Allocation]:
    out = []
    for m in lines:
        il = work.lines[m.inv_index]
        if m.po_line_no is not None:
            out.append(Allocation(m.po_line_no, il.qty_milli, il.amount_cents))
        elif m.bundle:  # split the billed amount across the bundled lines in proportion to their open value
            expected, left = sum(p["expected_cents"] for p in m.parts), il.amount_cents
            for i, p in enumerate(m.parts):
                amt = left if i == len(m.parts) - 1 else p["expected_cents"] * il.amount_cents // max(expected, 1)
                left -= amt
                out.append(Allocation(p["po_line_no"], p["qty_milli"], amt))
    return out


def evaluate(inv: Invoice, ctx: Context) -> Decision:
    vm = resolve_vendor(inv, ctx.vendors)
    vendor = vm.vendor if vm.status == "matched" else None
    pm = resolve_po(inv, vendor, ctx.pos, ctx.history)
    po = pm.po if pm.status in ("exact", "fuzzy") else None
    work, tax, lines, ledger = prepare(inv, po, ctx.history)
    dup = check_duplicates(inv, vendor, ctx.history)

    results = _run_rules(Assessment(work, ctx, vm, pm, lines, ledger, dup, inv if work is not inv else None, tax))
    outcome, reasons, deciding = adjudicate(results)

    if outcome == "auto_approved":
        r12 = next(r for r in results if r.rule == "R12").evidence
        verdict = (f"Auto-approved: all 16 checks passed. {money(r12['remaining_after'])} remains on "
                   f"{po.po_number}." if po else "Auto-approved: all 16 checks passed.")
        if tax["basis"] != "exclusive":
            verdict += f" Prices include {money(tax['tax_cents'])} tax; the PO was compared net of it."
        for m in lines:
            if m.bundle:
                verdict += f" Line {m.inv_index + 1} bundles PO lines {', '.join(map(str, m.bundle))}."
    else:
        verdict = " ".join(r.message for r in deciding)

    allocations = _allocations(work, lines) if po else []

    d = Decision(outcome, reasons, verdict, _ACTIONS.get(reasons[0], "Review manually"), results,
                 vendor.id if vendor else None, po.po_number if po else None, allocations, work=work, tax=tax)
    d.packet = _jsonable({
        "outcome": outcome, "reasons": reasons, "verdict": verdict,
        "recommended_action": d.recommended_action,
        "invoice": inv, "vendor_match": vm.__dict__ | {"vendor": vm.vendor.id if vm.vendor else None},
        "po_match": pm.__dict__ | {"po": pm.po.po_number if pm.po else None},
        "line_matches": lines, "allocations": allocations, "tax_basis": tax,
        **({"invoice_net": work} if work is not inv else {}),
        "duplicates": dup, "rules": results,
        "policy_version": P.POLICY_VERSION, "policy": P.snapshot(), "as_of": ctx.today,
    })
    d.packet_hash = packet_hash(d.packet)
    return d
