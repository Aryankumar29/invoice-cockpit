"""Demo scenarios (docs/DESIGN.md §3): exact outcome and reason code."""
import pytest

from cockpit.core.engine import evaluate, to_prior
from cockpit.core.seed import EXPECTED, context, fixture


@pytest.mark.parametrize("key", ["H1", "S1", "D1", "E1", "E2"])
def test_demo_fixture_outcome(key):
    d = evaluate(fixture(key), context())
    assert (d.outcome, d.reasons[0]) == EXPECTED[key], d.verdict


def test_happy_path_all_rules_pass():
    d = evaluate(fixture("H1"), context())
    assert {r.status for r in d.results} <= {"pass", "na"}
    assert d.result("R12").evidence["remaining_after"] == 0
    assert d.result("R09").evidence["mapping"] == [
        {"invoice_line": 1, "po_line": 1, "score": 1.0},
        {"invoice_line": 2, "po_line": 2, "score": 1.0},
        {"invoice_line": 3, "po_line": 3, "score": 1.0},
    ]


def test_split_po_second_invoice_knows_what_first_consumed():
    ctx = context()
    s1 = fixture("S1")
    d1 = evaluate(s1, ctx)
    assert d1.result("R12").evidence["remaining_after"] == 400_000
    ctx.history.append(to_prior(s1, d1, "run-s1"))
    d2 = evaluate(fixture("S2"), ctx)
    assert (d2.outcome, d2.reasons) == ("auto_approved", ["STP_CLEAN"])
    ev = d2.result("R12").evidence
    assert ev["remaining_before"] == 400_000 and ev["remaining_after"] == 0
    assert "final amount" in d2.result("R12").message
    assert ev["prior_runs"] == ["run-s1"]


def test_split_po_third_invoice_rejected_over_remaining():
    ctx = context()
    for key, rid in (("S1", "run-s1"), ("S2", "run-s2")):
        inv = fixture(key)
        ctx.history.append(to_prior(inv, evaluate(inv, ctx), rid))
    s3 = fixture("S2")
    s3.invoice_number, s3.file_hash = "INV-S-003", "sha-s3"
    s3.lines[0].description = "Implementation consulting - phase 3 (4 days)"
    d = evaluate(s3, ctx)
    assert d.outcome == "rejected" and "OVER_PO_REMAINING" in d.reasons
    assert d.result("R11").reason == "QTY_VARIANCE"


def test_split_po_rejected_invoice_does_not_consume_balance():
    ctx = context()
    s1 = fixture("S1")
    d1 = evaluate(s1, ctx)
    d1.outcome = "rejected"
    ctx.history.append(to_prior(s1, d1, "run-s1"))
    d = evaluate(fixture("S2"), ctx)
    assert d.result("R12").evidence["remaining_before"] == 1_000_000


def test_price_variance_detail():
    d = evaluate(fixture("D1"), context())
    r10 = d.result("R10")
    assert r10.evidence["worst"]["variance"] == 6_000
    assert r10.evidence["worst"]["allowed"] == 4_000
    assert r10.message == "Line 2 is $60.00 above the PO line; the allowed variance is $40.00."
    assert d.result("R12").status == "pass"   # header-level allowance $120 would have let it through
    assert d.result("R03").status == "pass"
    assert d.recommended_action == "Request a credit note"


def test_duplicate_links_original_and_still_shows_other_findings():
    d = evaluate(fixture("E1"), context())
    hit = d.result("R13").evidence["hits"][0]
    assert hit["run_id"] == "run-0007" and hit["invoice_number"] == "NW-2026-001"
    assert d.result("R12").status == "fail"   # PO-1038 is fully billed; lower tier, still recorded


def test_scan_missing_number_does_not_guess():
    d = evaluate(fixture("E2"), context())
    assert d.result("R02").evidence["missing"] == ["invoice_number"]
    assert d.result("R13").status == "cannot_run"
    assert d.recommended_action == "Return to vendor"
    for rule in ("R04", "R06", "R09", "R10", "R12"):
        assert d.result(rule).status == "pass", rule
