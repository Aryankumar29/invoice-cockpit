"""Every threshold at its boundary (docs/DESIGN.md §3)."""
from datetime import timedelta

import pytest

from cockpit.core.domain import InvoiceLine, POLine, PurchaseOrder
from cockpit.core.engine import evaluate
from cockpit.core.seed import Q, TODAY, context, fixture


def _single_line_ctx(unit_cents: int, qty: int, tax_bp: int = 0):
    ctx = context()
    ctx.pos["PO-T"] = PurchaseOrder("PO-T", "V-NW", "USD", [POLine(1, "Test part", qty * Q, unit_cents, qty * Q, "TP-1")],
                                    tax_rate_bp=tax_bp)
    return ctx


def _single_line_inv(amount_cents: int, qty: int, tax: int = 0, total_delta: int = 0):
    inv = fixture("H1")
    inv.po_reference, inv.file_hash, inv.invoice_number = "PO-T", "sha-boundary", "INV-T-1"
    inv.lines = [InvoiceLine("Test part", qty * Q, amount_cents // qty, amount_cents, "TP-1")]
    inv.subtotal_cents, inv.tax_cents = amount_cents, tax
    inv.total_cents = amount_cents + tax + total_delta
    return inv


@pytest.mark.parametrize("over,status", [(4_000, "pass"), (4_001, "fail")])
def test_r10_line_tolerance_2pct(over, status):
    # Expected $2,000.00 -> allowance min($40.00, $50.00) = $40.00
    ctx = _single_line_ctx(200_000, 1)
    inv = _single_line_inv(200_000 + over, 1)
    assert evaluate(inv, ctx).result("R10").status == status


@pytest.mark.parametrize("over,status", [(5_000, "pass"), (5_001, "fail")])
def test_r10_line_tolerance_50_cap(over, status):
    # Expected $10,000.00 -> 2% is $200, capped at $50.00
    ctx = _single_line_ctx(1_000_000, 1)
    inv = _single_line_inv(1_000_000 + over, 1)
    assert evaluate(inv, ctx).result("R10").status == status


@pytest.mark.parametrize("over,status", [(12_000, "pass"), (12_001, "fail")])
def test_r12_po_allowance(over, status):
    # PO $6,000.00 -> allowance max($120.00, $50.00) = $120.00
    ctx = _single_line_ctx(600_000, 1)
    inv = _single_line_inv(600_000 + over, 1)
    r = evaluate(inv, ctx).result("R12")
    assert r.status == status
    if status == "fail":
        assert r.outcome == "rejected"


@pytest.mark.parametrize("delta,status", [(2, "pass"), (-2, "pass"), (3, "fail")])
def test_r03_arithmetic_two_cents(delta, status):
    inv = fixture("H1")
    inv.total_cents += delta
    assert evaluate(inv, context()).result("R03").status == status


@pytest.mark.parametrize("tax_off,status", [(100, "pass"), (101, "fail")])
def test_r14_tax_tolerance(tax_off, status):
    # $100.00 at 8.25% = $8.25; allowance max($1.00, 0.1% of ~$110) = $1.00
    ctx = _single_line_ctx(10_000, 1, tax_bp=825)
    inv = _single_line_inv(10_000, 1, tax=825 + tax_off)
    assert evaluate(inv, ctx).result("R14").status == status


@pytest.mark.parametrize("total,outcome,approvers", [
    (1_000_000, "auto_approved", None),
    (1_000_001, "needs_review", 1),
    (5_000_001, "needs_review", 2),
])
def test_r15_approval_authority(total, outcome, approvers):
    ctx = _single_line_ctx(total, 1)
    inv = _single_line_inv(total, 1)
    d = evaluate(inv, ctx)
    assert d.outcome == outcome
    if approvers:
        assert d.reasons == ["APPROVAL_REQUIRED"]
        assert d.result("R15").evidence["approvers"] == approvers


@pytest.mark.parametrize("age,status", [(120, "pass"), (121, "fail")])
def test_r16_stale_invoice(age, status):
    inv = fixture("H1")
    inv.invoice_date = TODAY - timedelta(days=age)
    inv.due_date = inv.invoice_date + timedelta(days=30)
    assert evaluate(inv, context()).result("R16").status == status
