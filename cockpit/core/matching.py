"""Global invoice-line to PO-line assignment (docs/DESIGN.md D8)."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from . import policy as P
from .domain import InvoiceLine, POLine
from .normalize import alnum_upper, norm_text

_BIG = 1e6


@dataclass
class LineMatch:
    inv_index: int
    po_line_no: int | None
    score: float
    # A bundled invoice line ("Workstation kit, $2,400") bills the open balance of several whole PO lines.
    # parts: [{po_line_no, qty_milli, expected_cents}] that the bundle consumes; options: >1 subset fits.
    bundle: list[int] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)
    options: list[list[int]] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.po_line_no is not None or bool(self.bundle)


def pair_score(il: InvoiceLine, pl: POLine) -> float:
    desc = fuzz.token_set_ratio(norm_text(il.description), norm_text(pl.description)) / 100
    price = max(0.0, 1 - abs(il.unit_price_cents - pl.unit_price_cents) / max(pl.unit_price_cents, 1))
    a, b = alnum_upper(il.sku), alnum_upper(pl.sku)
    if a and b:
        if a != b:
            return 0.0  # item codes are exact or alias-only, never fuzzy
        return 0.4 + 0.4 * desc + 0.2 * price
    return 0.75 * desc + 0.25 * price


def match_lines(inv_lines: list[InvoiceLine], po_lines: list[POLine],
                billed_by_line: dict[int, tuple[int, int]] | None = None) -> list[LineMatch]:
    """Square (n+m) cost matrix. Invoice line -> its dummy column costs LINE_UNMATCHED_COST.
    PO line -> dummy row costs 0, because unbilled PO lines are normal in partial billing.
    Lines left unmatched are then tried as bundles of the PO lines nobody else claimed."""
    n, m = len(inv_lines), len(po_lines)
    if n == 0:
        return []
    size = n + m
    cost = np.full((size, size), _BIG)
    scores = np.zeros((n, max(m, 1)))
    for i, il in enumerate(inv_lines):
        for j, pl in enumerate(po_lines):
            scores[i, j] = pair_score(il, pl)
            cost[i, j] = 1 - scores[i, j]
        cost[i, m + i] = P.LINE_UNMATCHED_COST
    for j in range(m):
        cost[n + j, j] = 0.0
    cost[n:, m:] = 0.0
    rows, cols = linear_sum_assignment(cost)
    out = []
    for r, c in zip(rows.tolist(), cols.tolist()):
        if r >= n:
            continue
        if c < m:
            out.append(LineMatch(r, po_lines[c].line_no, round(float(scores[r, c]), 4)))
        else:
            out.append(LineMatch(r, None, 0.0))
    out.sort(key=lambda x: x.inv_index)
    _find_bundles(out, inv_lines, po_lines, billed_by_line or {})
    return out


def _find_bundles(out: list[LineMatch], inv_lines: list[InvoiceLine], po_lines: list[POLine],
                  billed_by_line: dict[int, tuple[int, int]]) -> None:
    """An unmatched line is a bundle when exactly one set of 2+ free PO lines has an open balance that adds up
    to its amount within the line tolerance. Two or more fitting sets: ambiguous, left for a reviewer.
    Only lump-sum lines (quantity 1) qualify: a line with its own quantity and unit price is an item, not a kit."""
    used = {m.po_line_no for m in out if m.po_line_no is not None}
    for m in out:
        il = inv_lines[m.inv_index]
        if m.po_line_no is not None or il.amount_cents <= 0 or il.qty_milli != 1000:
            continue
        free = []
        for pl in po_lines:
            if pl.line_no in used:
                continue
            qty = pl.qty_milli - billed_by_line.get(pl.line_no, (0, 0))[0]
            if qty > 0 and pl.unit_price_cents > 0:
                free.append((pl, qty, qty * pl.unit_price_cents // 1000))
        if len(free) < 2 or len(free) > P.BUNDLE_MAX_PO_LINES:
            continue
        # k = 1 counts toward ambiguity only: a single PO line worth the same amount is a rival reading.
        fits = []
        for k in range(1, len(free) + 1):
            for combo in combinations(free, k):
                total = sum(c[2] for c in combo)
                if abs(il.amount_cents - total) <= P.line_allowance(total):
                    fits.append(combo)
                    if len(fits) > 3:
                        break
            if len(fits) > 3:
                break
        if len(fits) == 1 and len(fits[0]) > 1:
            combo = fits[0]
            total = sum(c[2] for c in combo)
            m.bundle = [c[0].line_no for c in combo]
            m.parts = [{"po_line_no": pl.line_no, "qty_milli": q, "expected_cents": amt} for pl, q, amt in combo]
            m.score = round(1 - abs(il.amount_cents - total) / max(total, 1), 4)
            used.update(m.bundle)
        elif fits:
            m.options = [[c[0].line_no for c in combo] for combo in fits[:3]]
