"""Versioned policy thresholds. Starting policy, not industry law (docs/DESIGN.md D6)."""

POLICY_VERSION = "2026.10-demo.2"

ARITH_TOL_CENTS = 2                 # R03: tax rounding can differ by a cent or two
LINE_TOL_PCT = 2                    # R10: min(2% of expected line amount, $50)
LINE_TOL_CAP_CENTS = 5_000
PO_TOL_PCT = 2                      # R12: max(2% of PO total, $50)
PO_TOL_FLOOR_CENTS = 5_000
RECEIPT_TOL_MILLI = 0               # R11
TAX_TOL_FLOOR_CENTS = 100           # R14: max($1, 0.1% of total)
TAX_TOL_PERMILLE = 1
AUTO_APPROVE_CAP_CENTS = 1_000_000  # R15: auto-approve at or below $10,000
DUAL_APPROVAL_CENTS = 5_000_000     # R15: two approvers above $50,000
STALE_INVOICE_DAYS = 120            # R16

VENDOR_LINK_SCORE = 93
VENDOR_LINK_MARGIN = 5
VENDOR_REVIEW_SCORE = 85

PO_LINK_SCORE = 0.90
PO_LINK_MARGIN = 0.05
PO_REVIEW_SCORE = 0.70
PO_MISSING_REF_PENALTY = 0.05

LINE_UNMATCHED_COST = 0.45          # a pair must score > 0.55 to beat "unmatched"
BUNDLE_MAX_PO_LINES = 16            # R09: subset search when one invoice line bills several whole PO lines

DUP_NEAR_SCORE = 0.85
DUP_REVIEW_SCORE = 0.70

# Outcomes that consume PO balance. Pending reviews reserve it so two reviewers
# cannot spend the same remaining amount.
CONSUMING_OUTCOMES = {"auto_approved", "approved", "needs_review"}


def line_allowance(expected_cents: int) -> int:
    return min(abs(expected_cents) * LINE_TOL_PCT // 100, LINE_TOL_CAP_CENTS)


def po_allowance(po_total_cents: int) -> int:
    return max(po_total_cents * PO_TOL_PCT // 100, PO_TOL_FLOOR_CENTS)


def tax_allowance(total_cents: int) -> int:
    return max(TAX_TOL_FLOOR_CENTS, abs(total_cents) * TAX_TOL_PERMILLE // 1000)


def snapshot() -> dict:
    return {k: v for k, v in globals().items() if k.isupper() and k != "CONSUMING_OUTCOMES"}
