"""Tax-inclusive invoices and bundled line items: two common messy-invoice cases."""
import copy
import json
from pathlib import Path

import pytest

from cockpit.core.domain import POLine
from cockpit.core.engine import evaluate, tax_basis, tax_view, to_prior
from cockpit.core.seed import Q, context
from cockpit.extraction import TAX_INCLUDED
from fixtures.generate import from_extraction

MANIFEST = {e["key"]: e for e in json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "manifest.json").read_text(encoding="utf-8"))}


def fx(key):
    return from_extraction(copy.deepcopy(MANIFEST[key]["extraction"]))


@pytest.mark.parametrize("text", ["All prices include 8.25% sales tax.", "Total incl. VAT", "Sales Tax 8.25% (included)",
                                  "VAT inclusive", "Prices are inclusive of GST", "Amounts include tax",
                                  "including 20% VAT"])
def test_inclusive_wording_detected(text):
    assert TAX_INCLUDED.search(text)


@pytest.mark.parametrize("text", ["Prices exclude VAT", "Total excl. VAT", "Tax exempt: resale certificate on file",
                                  "Tax ID: 94-1234567", "Sales Tax 8.25%", "Shipping included"])
def test_exclusive_wording_not_detected(text):
    assert not TAX_INCLUDED.search(text)


def test_tax_inclusive_invoice_is_compared_net_and_approves():
    d = evaluate(fx("R-INCL"), context())
    assert (d.outcome, d.reasons) == ("auto_approved", ["STP_CLEAN"]), d.verdict
    assert d.tax["basis"] == "inclusive" and d.tax["tax_cents"] == 19_800
    assert d.work.lines[0].unit_price_cents == 20_000 and d.work.lines[0].amount_cents == 240_000
    assert [(a.po_line_no, a.amount_cents) for a in d.allocations] == [(1, 240_000)]  # the ledger holds net value
    assert "compared net" in d.verdict


def test_same_invoice_read_as_tax_exclusive_would_fail():
    """Without the tax basis the old engine added tax again and compared gross prices to a net PO."""
    inv = fx("R-INCL")
    inv.tax_inclusive = False
    inv.tax_cents = 19_800
    inv.total_cents = 259_800 + 19_800  # a vendor that really adds tax on top of gross prices
    d = evaluate(inv, context())
    assert d.outcome == "rejected" and d.reasons[0] == "OVER_PO_REMAINING"
    assert d.result("R10").reason == "PRICE_VARIANCE"


def test_inclusive_inferred_from_arithmetic_when_not_worded():
    inv = fx("R-INCL")
    inv.tax_inclusive = False
    assert tax_basis(inv) == "inclusive_inferred"
    d = evaluate(inv, context())
    assert d.outcome == "auto_approved" and "inside the prices" in d.result("R03").message


def test_included_tax_not_printed_is_derived_from_po_rate():
    inv = fx("R-INCL")
    inv.tax_cents = 0
    work, info = tax_view(inv, context().pos["PO-1090"])
    assert info == {"basis": "inclusive", "derived": True, "tax_cents": 19_800, "gross_cents": 259_800,
                    "net_cents": 240_000}
    assert work.tax_cents == 19_800 and sum(l.amount_cents for l in work.lines) == 240_000


def test_inclusive_tax_on_exempt_po_is_flagged():
    inv = fx("R-INCL")
    ctx = context()
    ctx.pos["PO-1090"].tax_rate_bp = 0
    d = evaluate(inv, ctx)
    assert "TAX_VARIANCE" in d.reasons


def test_net_view_lines_sum_exactly_with_discount():
    inv = fx("R-INCL")
    inv.lines.append(copy.deepcopy(inv.lines[0]))
    inv.lines[1].qty_milli, inv.lines[1].amount_cents = 3 * Q, 64_950
    inv.discount_cents = 1_001
    gross = sum(l.amount_cents for l in inv.lines) - inv.discount_cents
    inv.tax_cents = gross - round(gross / 1.0825)
    work, info = tax_view(inv, None)
    assert sum(l.amount_cents for l in work.lines) - work.discount_cents == gross - inv.tax_cents


def test_bundled_line_matches_the_po_lines_it_covers():
    d = evaluate(fx("R-BUNDLE"), context())
    assert (d.outcome, d.reasons) == ("auto_approved", ["STP_CLEAN"]), d.verdict
    assert d.result("R09").evidence["mapping"][0]["bundle"] == [1, 2, 3]
    assert [(a.po_line_no, a.qty_milli, a.amount_cents) for a in d.allocations] == [
        (1, 10 * Q, 100_000), (2, 20 * Q, 90_000), (3, 5 * Q, 50_000), (4, 8 * Q, 60_000)]
    assert "bundles PO lines 1, 2, 3" in d.verdict


def test_bundle_consumes_the_po_so_a_rebill_is_over_remaining():
    ctx = context()
    first = fx("R-BUNDLE")
    ctx.history.append(to_prior(first, evaluate(first, ctx), "run-b1"))
    again = fx("R-BUNDLE")
    again.invoice_number = "INV-B-002"
    again.file_hash = "other"
    d = evaluate(again, ctx)
    assert d.outcome in ("rejected", "needs_review") and d.outcome != "auto_approved"


def test_ambiguous_bundle_goes_to_review_with_the_options():
    ctx = context()
    ctx.pos["PO-1095"].lines.append(POLine(5, "Spare bracket set", 1 * Q, 240_000, 1 * Q, "SP-1"))
    d = evaluate(fx("R-BUNDLE"), ctx)
    assert (d.outcome, d.reasons) == ("needs_review", ["LINE_UNVERIFIED"])
    assert "more than one set" in d.result("R09").message and "1+2+3" in d.result("R09").message


def test_bundle_with_unreceived_goods_is_held():
    ctx = context()
    ctx.pos["PO-1095"].lines[1].received_qty_milli = 10 * Q
    d = evaluate(fx("R-BUNDLE"), ctx)
    assert "QTY_VARIANCE" in d.reasons


def test_itemised_line_with_quantity_is_never_treated_as_bundle():
    inv = fx("R-BUNDLE")
    inv.lines[0].qty_milli, inv.lines[0].unit_price_cents = 2 * Q, 120_000
    d = evaluate(inv, context())
    assert d.reasons[0] == "LINE_UNVERIFIED"
