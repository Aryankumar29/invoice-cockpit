"""Node 7 - Policy: evaluate rules R01-R16 (cockpit/core/rules.py) against the invoice and context."""
from __future__ import annotations

from ..core.domain import Context, Invoice
from ..core.engine import evaluate
from ._shared import rule_row

STAGE, TITLE = "policy", "Policy R01-R16"


def run(inv: Invoice, ctx: Context):
    d = evaluate(inv, ctx)
    counts: dict[str, int] = {}
    for r in d.results:
        counts[r.status] = counts.get(r.status, 0) + 1
    failed = [f"{r.rule} {r.reason}" for r in d.results if r.status in ("fail", "error")]
    msg = f"16 rules evaluated: {counts.get('pass', 0)} pass, {len(failed)} fail" + (
        f" ({', '.join(failed)})." if failed else ".")
    return ("ok" if not failed else "warn"), msg, {"counts": counts, "rules": [rule_row(r) for r in d.results],
                                                   "policy_version": d.policy_version}, d
