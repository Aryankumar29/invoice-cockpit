"""Explanation agent A9: plain-English summary and printable decision memo, built only from the packet.

The template is the source of truth. An optional Claude rewrite is used only when it keeps the outcome, cites
every reason code, and introduces no number that is not already in the packet.
"""
from __future__ import annotations

import html
import os
import re
from decimal import Decimal, InvalidOperation

from cockpit.core.engine import packet_hash
from cockpit.core.rules import money

OUTCOME_WORDS = {"auto_approved": "Auto-approved", "approved": "Approved", "needs_review": "Needs review",
                 "rejected": "Rejected", "returned": "Returned to vendor", "failed": "Failed"}

WHY = {
    "STP_CLEAN": "No exception needs a person; the invoice can be released for payment.",
    "PRICE_VARIANCE": "Paying above the agreed PO price needs a credit note or a PO change first.",
    "DUPLICATE_INVOICE": "Paying the same obligation twice is a permanent loss, so this is rejected outright.",
    "DUPLICATE_SUSPECTED": "It may be the same obligation billed twice; a person must compare it with the earlier invoice.",
    "MISSING_INVOICE_NUMBER": "Without an invoice number the duplicate check by reference cannot run, so a person must confirm it is not a resubmission.",
    "MISSING_FIELD": "A required header field is missing; the system does not fill in values.",
    "ARITHMETIC_MISMATCH": "The document does not add up, so its figures cannot be trusted as printed.",
    "DOCUMENT_UNREADABLE": "No extraction route produced trustworthy values, so nothing was checked automatically.",
    "BANK_CHANGE": "A changed remittance account is the classic payment-fraud pattern; confirm it with the vendor by a known phone number before paying.",
    "TAX_ID_CONFLICT": "The name looks like a known vendor but the tax ID does not, which can indicate impersonation.",
    "VENDOR_INACTIVE": "Payments may only go to active vendors in the vendor master.",
    "UNKNOWN_VENDOR": "The supplier is not in the approved vendor master, so there is nobody verified to pay.",
    "PO_MISSING": "No purchase order could be linked safely, so there is no approved commitment to match against.",
    "PO_AMBIGUOUS": "More than one purchase order fits; a person must pick the right one rather than the system guessing.",
    "PO_CLOSED": "A closed PO has no remaining commitment to bill against.",
    "VENDOR_PO_MISMATCH": "The PO was issued to a different vendor, so this invoice cannot draw on it.",
    "CURRENCY_MISMATCH": "The invoice currency differs from the PO currency, so amounts are not comparable.",
    "OVER_PO_REMAINING": "Paying it would spend more than the PO has left after earlier invoices.",
    "QTY_VARIANCE": "More units are billed than have been received; hold until the goods receipt is posted.",
    "LINE_UNVERIFIED": "A billed line has no matching PO line, so nobody has approved buying it.",
    "TAX_VARIANCE": "The stated tax does not match the rate on the PO.",
    "APPROVAL_REQUIRED": "The amount is above the auto-approval limit, so a named approver must sign off.",
    "DUE_DATE_INVALID": "The invoice dates are inconsistent or too old to pay without checking.",
    "INCOMPLETE_EVIDENCE": "Some mandatory checks could not run, and approval needs evidence, not just the absence of a failure.",
    "RULE_ERROR": "A check failed to run correctly, so the system stopped rather than guess.",
}


def _failing(packet: dict) -> list[dict]:
    return [r for r in packet.get("rules", []) if r.get("status") in ("fail", "error")]


def _headline(packet: dict) -> str:
    inv = packet.get("invoice") or {}
    po = (packet.get("po_match") or {}).get("po")
    parts = [f"invoice {inv['invoice_number']}" if inv.get("invoice_number") else "an invoice with no number"]
    if inv.get("vendor_name"):
        parts.append(f"from {inv['vendor_name']}")
    if inv.get("total_cents") is not None:
        parts.append(f"for {money(inv['total_cents'])}")
    if po:
        parts.append(f"against {po}")
    return f"{OUTCOME_WORDS.get(packet.get('outcome'), packet.get('outcome'))}: {' '.join(parts)}."


def template(packet: dict) -> str:
    outcome, reasons = packet.get("outcome"), packet.get("reasons") or []
    failing = _failing(packet)
    out = [_headline(packet)]
    if reasons == ["STP_CLEAN"]:
        out.append(f"[STP_CLEAN] {packet.get('verdict') or 'All checks passed.'}")
        out.append(WHY["STP_CLEAN"])
    for code in reasons:
        if code == "STP_CLEAN":
            continue
        if code == "INCOMPLETE_EVIDENCE":
            gaps = [r for r in packet.get("rules", []) if r.get("status") == "cannot_run"]
            msgs = "; ".join(f"{r['rule']} {r['title']}: {r['message']}" for r in gaps) or "mandatory checks did not run"
            out.append(f"[{code}] {msgs}. {WHY[code]}")
            continue
        rules = [r for r in failing if r.get("reason") == code]
        msg = " ".join(f"{r['rule']}: {r['message']}" for r in rules) or packet.get("verdict", "")
        out.append(f"[{code}] {msg} {WHY.get(code, '')}".strip())
    also = [r for r in failing if r.get("reason") not in reasons]
    if also:
        out.append("Also noted: " + " ".join(f"{r['rule']} ({r['reason']}): {r['message']}" for r in also))
    if packet.get("recommended_action"):
        out.append(f"Recommended action: {packet['recommended_action']}.")
    return "\n".join(out)


# ---- optional Claude rewrite --------------------------------------------------------------------

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _dec(tok: str) -> Decimal | None:
    try:
        return Decimal(tok.replace(",", "")).normalize()
    except InvalidOperation:
        return None


def _packet_numbers(o, acc: set) -> set:
    if isinstance(o, bool) or o is None:
        return acc
    if isinstance(o, int):
        for v in (Decimal(o), Decimal(o) / 100, Decimal(o) / 1000, abs(Decimal(o)), abs(Decimal(o)) / 100):
            acc.add(v.normalize())
    elif isinstance(o, float):
        d = Decimal(str(o))
        acc |= {d.normalize(), round(d, 2).normalize(), (d * 100).normalize()}
    elif isinstance(o, str):
        acc |= {d for d in map(_dec, _NUM.findall(o)) if d is not None}
    elif isinstance(o, dict):
        for k, v in o.items():
            _packet_numbers(k, acc), _packet_numbers(v, acc)
    elif isinstance(o, (list, tuple)):
        for v in o:
            _packet_numbers(v, acc)
    return acc


def check_invariants(text: str, packet: dict) -> list[str]:
    """Problems with a candidate explanation; empty means it may be shown."""
    problems = []
    outcome = packet.get("outcome")
    word = OUTCOME_WORDS.get(outcome, outcome or "").lower()
    lead = re.split(r"[.:\n]", text.lower().strip(), maxsplit=1)[0]  # the verdict sentence
    if word not in lead:
        problems.append(f"outcome '{word}' not stated first")
    for other, w in OUTCOME_WORDS.items():
        if other != outcome and w.lower() not in word and re.search(rf"\b{re.escape(w.lower())}\b", lead):
            problems.append(f"mentions a different outcome '{w}'")
    allowed = _packet_numbers(packet, _packet_numbers(template(packet), set()))
    invented = [t for t in _NUM.findall(text) if _dec(t) not in allowed]
    if invented:
        problems.append(f"numbers not in the packet: {invented}")
    missing = [c for c in packet.get("reasons") or [] if c not in text]
    if missing:
        problems.append(f"reason codes not addressed: {missing}")
    return problems


_SYSTEM = """You rewrite accounts-payable decision explanations for a reviewer. The decision is final and was made \
by deterministic rules; you only make the wording clearer. Rules:
- Start with the outcome phrase exactly as given.
- Mention every reason code exactly as written, in square brackets.
- Use only numbers, amounts, dates and identifiers that appear in the facts. Do not calculate new numbers.
- Do not speculate, soften, or change the outcome. Plain prose, at most 120 words."""


def _llm_rewrite(packet: dict, base: str) -> str | None:
    if not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("COCKPIT_LLM_EXPLAIN") == "1"):
        return None
    from cockpit.extraction import claude
    client = claude.make_client()
    resp = client.beta.messages.create(
        model=claude.MODEL, max_tokens=4000, betas=claude.BETAS, fallbacks="default", system=_SYSTEM,
        output_config={"effort": os.environ.get("COCKPIT_EFFORT", "low")},
        messages=[{"role": "user", "content": f"Outcome phrase: {OUTCOME_WORDS.get(packet.get('outcome'))}\n"
                                              f"Reason codes: {', '.join(packet.get('reasons') or [])}\n\nFacts:\n{base}"}],
        timeout=claude.TIMEOUT_S)
    from cockpit import usage
    usage.record(resp, "explanation")
    if resp.stop_reason != "end_turn":
        return None
    return next((b.text for b in resp.content if b.type == "text"), None)


def explain_source(packet: dict) -> tuple[str, str]:
    """(text, source) where source is 'claude' (rewrite passed every invariant) or 'template'."""
    base = template(packet)
    try:
        text = _llm_rewrite(packet, base)
    except Exception:  # explanation must never break a run
        text = None
    if text and not check_invariants(text, packet):
        return text.strip(), "claude"
    return base, "template"


def explain(packet: dict) -> str:
    return explain_source(packet)[0]


# ---- decision memo ------------------------------------------------------------------------------

_CSS = """body{font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1b1f24;max-width:960px;
margin:24px auto;padding:0 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px;
border-bottom:1px solid #d0d7de;padding-bottom:4px}table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid #d0d7de;padding:4px 6px;text-align:left;vertical-align:top}th{background:#f6f8fa}
.num{text-align:right;font-variant-numeric:tabular-nums}.meta{color:#57606a;font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-weight:600;font-size:12px}
.auto_approved,.approved,.pass{background:#dafbe1;color:#116329}.needs_review{background:#fff8c5;color:#7d4e00}
.rejected,.failed,.fail,.error{background:#ffebe9;color:#a40e26}.cannot_run,.na,.not_applicable{background:#eaeef2;color:#57606a}
pre{white-space:pre-wrap;background:#f6f8fa;padding:8px;border-radius:6px}code{font-size:12px;word-break:break-all}
@media print{body{margin:0;max-width:none}h2{break-after:avoid}tr{break-inside:avoid}}"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _table(headers: list[str], rows: list[list], num_cols=()) -> str:
    if not rows:
        return '<p class="meta">None.</p>'
    th = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f'<td class="num">{c}</td>' if i in num_cols else f"<td>{c}</td>"
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _badge(s) -> str:
    return f'<span class="badge {_e(s)}">{_e(s)}</span>'


def memo(packet: dict, run: dict) -> str:
    run = run or {}
    inv = packet.get("invoice") or {}
    outcome = run.get("outcome") or packet.get("outcome")
    phash = run.get("packet_hash") or packet_hash(packet)
    rules = packet.get("rules") or []
    r10 = next((r for r in rules if r.get("rule") == "R10"), {}).get("evidence") or {}
    by_line = {row.get("invoice_line"): row for row in r10.get("lines", [])}
    lines = inv.get("lines") or []

    header = _table(["Field", "Value"], [[_e(k), _e(v)] for k, v in [
        ("Vendor", inv.get("vendor_name")), ("Invoice number", inv.get("invoice_number") or "(none printed)"),
        ("Invoice date", inv.get("invoice_date")), ("Due date", inv.get("due_date")),
        ("PO reference", inv.get("po_reference")), ("Total", f"{money(inv.get('total_cents'))} {inv.get('currency') or ''}"),
        ("Vendor match", f"{(packet.get('vendor_match') or {}).get('vendor')} ({(packet.get('vendor_match') or {}).get('method')})"),
        ("PO match", f"{(packet.get('po_match') or {}).get('po')} ({(packet.get('po_match') or {}).get('method')})"),
        ("Extraction source", run.get("extraction_source")), ("File", run.get("filename")),
        ("File SHA-256", inv.get("file_hash")),
    ] if v not in (None, "")])

    rule_rows = [[_e(r.get("rule")), _e(r.get("title")), _badge(r.get("status")), _e(r.get("message")),
                  _e(r.get("reason") or ""), _e("" if r.get("tier") is None else r.get("tier"))] for r in rules]

    match_rows = []
    for m in packet.get("line_matches") or []:
        i = m.get("inv_index", 0)
        il = lines[i] if i < len(lines) else {}
        v = by_line.get(i + 1, {})
        match_rows.append([_e(i + 1), _e(il.get("description")), _e(m.get("po_line_no") or "unmatched"),
                           _e(m.get("score")), _e(money(v.get("expected")) if v else ""),
                           _e(money(il.get("amount_cents"))), _e(money(v.get("variance")) if v else ""),
                           _e(money(v.get("allowed")) if v else ""), _badge("pass" if v.get("ok") else "fail") if v else ""])

    dup = packet.get("duplicates") or {}
    dup_rows = [[_e(h.get("run_id")), _e(h.get("invoice_number")), _e(h.get("score")), _e(", ".join(h.get("signals", [])))]
                for h in dup.get("hits", [])]
    alloc_rows = [[_e(a.get("po_line_no")), _e(a.get("qty_milli", 0) / 1000), _e(money(a.get("amount_cents")))]
                  for a in packet.get("allocations") or []]
    stage_rows = [[_e(s.get("seq")), _e(s.get("ts")), _e(s.get("stage")), _badge(s.get("status")), _e(s.get("title")),
                   _e(s.get("message"))] for s in run.get("stages") or []]
    action_rows = [[_e(a.get("ts")), _e(a.get("reviewer")), _e(a.get("action")),
                    _e(f"{a.get('outcome_before')} -> {a.get('outcome_after')}"), _e(a.get("note"))]
                   for a in run.get("actions") or []]
    warnings = "".join(f"<li>{_e(w)}</li>" for w in run.get("warnings") or [])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decision memo {_e(inv.get('invoice_number') or run.get('id') or '')}</title><style>{_CSS}</style></head><body>
<h1>Invoice decision memo</h1>
<p class="meta">Run {_e(run.get('id'))} &middot; {_e(run.get('created_at'))} &middot; as of {_e(packet.get('as_of'))}
&middot; policy {_e(packet.get('policy_version'))}</p>
<h2>Verdict</h2>
<p>{_badge(outcome)} <strong>{_e(packet.get('verdict'))}</strong></p>
<p>Reasons: {', '.join(f'<code>{_e(r)}</code>' for r in packet.get('reasons') or [])}<br>
Recommended action: {_e(packet.get('recommended_action'))}</p>
<pre>{_e(run.get('explanation') or template(packet))}</pre>
{f'<h2>Extraction warnings</h2><ul>{warnings}</ul>' if warnings else ''}
<h2>Invoice</h2>{header}
<h2>Rules (all {len(rules)} evaluated)</h2>
{_table(['Rule', 'Check', 'Status', 'Evidence', 'Reason', 'Tier'], rule_rows)}
<h2>Line matches</h2>
{_table(['Inv line', 'Description', 'PO line', 'Score', 'Expected', 'Billed', 'Variance', 'Allowed', 'Price'], match_rows, (3, 4, 5, 6, 7))}
<h2>PO allocations</h2>
{_table(['PO line', 'Qty', 'Amount'], alloc_rows, (1, 2))}
<h2>Duplicate check</h2>
<p>Result: <code>{_e(dup.get('kind'))}</code>; reference check {'ran' if dup.get('ref_check_ran') else 'could not run'}.</p>
{_table(['Prior run', 'Invoice number', 'Score', 'Signals'], dup_rows) if dup_rows else ''}
<h2>Audit trail</h2>
{_table(['#', 'Time', 'Stage', 'Status', 'Step', 'Detail'], stage_rows)}
<h2>Reviewer actions</h2>
{_table(['Time', 'Reviewer', 'Action', 'Outcome', 'Note'], action_rows)}
<h2>Integrity</h2>
<p>Decision packet SHA-256: <code>{_e(phash)}</code><br>Policy version: <code>{_e(packet.get('policy_version'))}</code></p>
<p class="meta">The model reads; the rules decide; the human is accountable. This memo is generated from the stored
decision packet; re-evaluating the same inputs under the same policy version reproduces the same hash.</p>
</body></html>"""
