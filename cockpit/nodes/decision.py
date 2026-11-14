"""Node 8 - Decision: publish the outcome (auto_approved / needs_review / rejected), reasons and next action."""
from __future__ import annotations

from ..core import policy as P
from ..core.engine import Decision

STAGE, TITLE = "decision", "Decision"
_STATUS = {"auto_approved": "ok", "needs_review": "warn", "rejected": "stopped"}


def run(d: Decision):
    data = {"outcome": d.outcome, "reasons": d.reasons, "recommended_action": d.recommended_action,
            "packet_hash": d.packet_hash}
    return _STATUS.get(d.outcome, "warn"), d.verdict, data, None


def dual_control_required(detail: dict, total_cents: int | None) -> bool:
    if (total_cents or 0) > P.DUAL_APPROVAL_CENTS:
        return True
    return any(r.get("evidence", {}).get("dual_control") or (r.get("evidence", {}).get("approvers") or 0) >= 2
               for r in detail.get("rules", []) if r["status"] == "fail")
