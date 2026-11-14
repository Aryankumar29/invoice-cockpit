"""Plain-language issues for one run: what is wrong, and where to look. Each issue carries a link target the
review page understands (#line-2, #rule-R12, #field-invoice_number) or another run (the original of a duplicate)."""
from __future__ import annotations

_FIELD = {"invoice_number": "invoice number", "invoice_date": "invoice date", "vendor_name": "vendor name",
          "total": "total amount", "currency": "currency"}


def _rule_issue(r: dict) -> dict:
    ev = r.get("evidence") or {}
    rid, reason = r["rule"], r.get("reason")
    item = {"rule": rid, "reason": reason, "text": r["message"], "severity": "reject" if r.get("outcome") == "rejected"
            else "review", "anchor": f"rule-{rid}", "link": f"See check {rid}", "run": None}
    if rid == "R02" and ev.get("missing"):
        names = [_FIELD.get(m, m.replace("_", " ")) for m in ev["missing"]]
        item |= {"text": f"Missing on the invoice: {', '.join(names)}.", "anchor": f"field-{ev['missing'][0]}",
                 "link": "See what was read"}
    elif rid == "R10" and isinstance(ev.get("worst"), dict):
        n = ev["worst"]["invoice_line"]
        item |= {"anchor": f"line-{n}", "link": f"See line {n}"}
    elif rid == "R09":
        bad = [m["invoice_line"] for m in ev.get("mapping") or [] if m.get("po_line") is None and not m.get("bundle")]
        if bad:
            item |= {"anchor": f"line-{bad[0]}", "link": f"See line {bad[0]}"}
    elif rid == "R11":
        item |= {"link": "See quantities"}
    elif rid == "R12":
        item |= {"anchor": "po-balance", "link": "See PO balance"}
    elif rid in ("R06", "R07", "R08"):
        item |= {"anchor": "vendor-po", "link": "See vendor and PO"}
    elif reason in ("DUPLICATE_INVOICE", "DUPLICATE_SUSPECTED"):
        hits = ev.get("hits") or []
        orig = hits[0].get("run_id") if hits and isinstance(hits[0], dict) else None
        item |= {"anchor": "duplicates", "link": "See the match"}
        if orig:
            item |= {"run": orig, "link": "Open the original"}
    return item


def for_run(run: dict) -> list[dict]:
    """run: a row with status, outcome, error and detail (as stored)."""
    d = run.get("detail") or {}
    if run.get("status") == "failed" or run.get("outcome") == "failed":
        return [{"rule": None, "reason": (run.get("reasons") or ["FAILED"])[0], "severity": "fail",
                 "text": run.get("error") or d.get("verdict") or "Processing failed.", "anchor": "timeline",
                 "link": "See where it stopped", "run": None}]
    if run.get("status") != "done":
        return []
    rules = [r for r in d.get("rules") or [] if r.get("status") in ("fail", "error")]
    order = {"reject": 0, "review": 1}
    primary = list(run.get("reasons") or [])  # the decision's own order: its main reason first
    rank = lambda i: primary.index(i["reason"]) if i["reason"] in primary else len(primary)
    out = sorted((_rule_issue(r) for r in rules), key=lambda i: (order.get(i["severity"], 2), rank(i)))
    if not out and run.get("outcome") in ("auto_approved", "approved"):
        n = len(d.get("rules") or [])
        out = [{"rule": None, "reason": None, "severity": "ok", "text": f"All {n} checks passed." if n else
                "Approved.", "anchor": "rules", "link": "See the checks", "run": None}]
    return out
