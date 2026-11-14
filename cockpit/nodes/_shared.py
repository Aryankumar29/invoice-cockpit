"""Helpers shared by several nodes: the stop signal and the row shapes the UI reads."""
from __future__ import annotations

from ..core import policy as P
from ..core.domain import Invoice
from ..core.engine import _jsonable

_RULE_STATUS = {"na": "not_applicable"}


class StageStop(Exception):
    def __init__(self, stage: str, title: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.stage, self.title, self.message, self.data = stage, title, message, data or {}


def rule_row(r) -> dict:
    return {"rule": r.rule, "title": r.title, "status": _RULE_STATUS.get(r.status, r.status), "message": r.message,
            "tier": r.tier, "outcome": r.outcome, "reason": r.reason, "evidence": _jsonable(r.evidence)}


def line_rows(inv: Invoice, po, matches) -> list[dict]:
    by_no = {l.line_no: l for l in po.lines}
    rows = []
    for m in matches:
        il = inv.lines[m.inv_index]
        pl = by_no.get(m.po_line_no)
        expected = il.qty_milli * pl.unit_price_cents // 1000 if pl else None
        po_desc = pl.description if pl else None
        if m.bundle:
            expected = sum(p["expected_cents"] for p in m.parts)
            po_desc = " + ".join(by_no[n].description for n in m.bundle if n in by_no)
        rows.append({
            "inv_index": m.inv_index, "description": il.description, "sku": il.sku, "qty_milli": il.qty_milli,
            "unit_price_cents": il.unit_price_cents, "amount_cents": il.amount_cents, "po_line_no": m.po_line_no,
            "po_description": po_desc, "po_unit_price_cents": pl.unit_price_cents if pl else None,
            "expected_cents": expected, "variance_cents": il.amount_cents - expected if expected is not None else None,
            "allowance_cents": P.line_allowance(expected) if expected is not None else None, "score": round(m.score, 3),
            "bundle": m.bundle, "bundle_options": m.options})
    return rows
