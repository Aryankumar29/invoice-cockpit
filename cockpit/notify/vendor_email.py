"""Draft the email back to a vendor when an invoice has a problem only the vendor can fix. A person reviews and
sends it; the cockpit never emails vendors itself. The address comes from the vendor master, never from the
incoming email, so a spoofed sender cannot receive the reply."""
from __future__ import annotations

from urllib.parse import quote

from ..core.rules import money

# Reason -> what we ask the vendor for. Reasons missing here are internal (fraud, onboarding, approvals):
# no vendor email is drafted for them.
ASK = {
    "PRICE_VARIANCE": "Please re-issue the invoice at the agreed PO price, or send a credit note for the difference.",
    "QTY_VARIANCE": "We can only pay for quantities received so far. Please re-invoice for the delivered quantity, "
                    "or let us know when the rest ships.",
    "OVER_PO_REMAINING": "The invoice is more than what is left on the purchase order. Please correct the amount, "
                         "or confirm with your buyer contact that a PO change has been agreed.",
    "LINE_UNVERIFIED": "Some lines do not correspond to items on the purchase order. Please itemise them against "
                       "the PO lines, or remove items that were not ordered.",
    "MISSING_INVOICE_NUMBER": "The invoice has no invoice number. Please re-issue it with a unique number.",
    "MISSING_FIELD": "Required details are missing. Please re-issue the invoice with all fields completed.",
    "ARITHMETIC_MISMATCH": "The line totals, tax and freight do not add up to the invoice total. Please send a "
                           "corrected invoice.",
    "DOCUMENT_UNREADABLE": "We could not read the document. Please send a clear PDF copy (not a photo or scan).",
    "PO_MISSING": "The invoice does not quote a purchase order number. Please re-issue it with the PO number.",
    "PO_AMBIGUOUS": "We could not tell which purchase order this invoice is for. Please state the PO number.",
    "PO_CLOSED": "The purchase order quoted is closed. Please check the PO number with your buyer contact.",
    "VENDOR_PO_MISMATCH": "The PO number quoted was issued to a different supplier. Please check the PO number.",
    "CURRENCY_MISMATCH": "The invoice currency differs from the purchase order. Please re-issue it in the PO "
                         "currency.",
    "TAX_VARIANCE": "The tax charged does not match the rate on the purchase order. Please send a corrected "
                    "invoice or confirm the rate that applies.",
    "DUE_DATE_INVALID": "The due date is earlier than the invoice date or outside the agreed terms. Please confirm "
                        "the dates.",
    "DUPLICATE_SUSPECTED": "This looks very similar to an invoice we already have. Please confirm whether it is a "
                           "new charge or a copy of an earlier invoice.",
    "DUPLICATE_INVOICE": "This invoice has already been received and paid. No action is needed unless it is a new "
                         "charge, in which case please issue it with a new invoice number.",
}
INTERNAL = {"BANK_CHANGE": "Bank details differ from the vendor master: verify by phone on the number on file, "
                           "do not ask by email.",
            "TAX_ID_CONFLICT": "Tax ID conflict: check with the vendor-master owner first.",
            "UNKNOWN_VENDOR": "Unknown vendor: onboarding comes first.",
            "VENDOR_INACTIVE": "Vendor is inactive in the master: check with the vendor-master owner.",
            "APPROVAL_REQUIRED": "Needs an internal approver; nothing to ask the vendor."}


def draft(run: dict, contact: dict | None, buyer: str) -> dict | None:
    """run: the run detail the API returns. Returns None when there is nothing to ask the vendor."""
    if run.get("outcome") not in ("needs_review", "rejected", "returned"):
        return None
    reasons = list(dict.fromkeys(run.get("reasons") or []))
    asks = [r for r in reasons if r in ASK]
    if "DUPLICATE_INVOICE" in asks:  # a copy of a paid invoice: its other findings are moot
        asks = ["DUPLICATE_INVOICE"]
    internal = [INTERNAL[r] for r in reasons if r in INTERNAL]
    if not asks:
        return {"available": False, "internal_notes": internal,
                "why": "Nothing on this invoice is for the vendor to fix." if not internal else
                       "Only internal checks failed; do not contact the vendor about them by email."}
    inv = run.get("invoice") or {}
    vendor = run.get("vendor_name") or inv.get("vendor_name") or "supplier"
    number = run.get("invoice_number") or "without number"
    po = run.get("po_number") or inv.get("po_reference")
    failing = {r["reason"]: r for r in run.get("rules") or [] if r.get("status") == "fail" and r.get("reason")}
    cur = run.get("currency") or inv.get("currency") or "USD"
    points = []
    for code in asks:
        detail = failing.get(code, {}).get("message")
        lines = _price_lines(run, cur) if code == "PRICE_VARIANCE" else (
            _unmatched_lines(run, cur) if code == "LINE_UNVERIFIED" else [])
        if lines:
            points.append(ASK[code] + "\n" + "\n".join(lines))
        else:
            points.append(ASK[code] + (f"\n   What we found: {detail}" if detail else ""))
    subject = (f"Invoice {number} already paid" if asks == ["DUPLICATE_INVOICE"] else
               f"Invoice {number}" + (f" ({po})" if po else "") + ": correction needed")
    total = money(run.get("total_cents")) if run.get("total_cents") is not None else "the amount stated"
    body = "\n".join([
        f"Hello {vendor} accounts team,",
        "",
        f"Thank you for invoice {number} for {total}" + (f" against purchase order {po}." if po else "."),
        "We have checked it against our records:" if asks == ["DUPLICATE_INVOICE"]
        else "We are unable to process it for payment yet:",
        "",
        *[f"{i}. {p}" for i, p in enumerate(points, 1)],
        "",
        "Please reply with a corrected invoice (or the credit note) as a PDF, quoting the same PO number.",
        "",
        "Kind regards,",
        f"Accounts Payable, {buyer}",
    ])
    to = (contact or {}).get("email")
    mailto = f"mailto:{quote(to or '')}?subject={quote(subject)}&body={quote(body)}"
    return {"available": True, "to": to, "to_source": "vendor master" if to else None,
            "missing_address": not to, "subject": subject, "body": body, "mailto": mailto,
            "reasons": asks, "internal_notes": internal}


def _amt(cents: int | None, cur: str) -> str:
    if cents is None:
        return "n/a"
    return money(cents) if cur == "USD" else f"{'-' if cents < 0 else ''}{cur} {abs(cents) / 100:,.2f}"


def _qty(milli: int) -> str:
    return f"{milli / 1000:,.3f}".rstrip("0").rstrip(".")


def _item(m: dict) -> str:
    code = f", item code {m['sku']}" if m.get("sku") else ""
    po_line = (f", PO lines {' + '.join(map(str, m['bundle']))}" if m.get("bundle") else
               f", PO line {m['po_line_no']}" if m.get("po_line_no") else "")
    return f"Line {m['inv_index'] + 1}: {m['description']}{code}{po_line}"


def _price_lines(run: dict, cur: str) -> list[str]:
    """Every line billed above the PO beyond the tolerance: what was charged, the PO price and the difference."""
    out, over = [], 0
    for m in run.get("line_matches") or []:
        var, allow = m.get("variance_cents"), m.get("allowance_cents")
        if var is None or allow is None or abs(var) <= allow:
            continue
        over += var
        if m.get("po_unit_price_cents") is not None and not m.get("bundle"):
            out.append(f"   - {_item(m)}\n     Billed {_qty(m['qty_milli'])} x {_amt(m['unit_price_cents'], cur)} = "
                       f"{_amt(m['amount_cents'], cur)}; PO price {_amt(m['po_unit_price_cents'], cur)} gives "
                       f"{_amt(m['expected_cents'], cur)}. Difference {_amt(var, cur)}.")
        else:
            out.append(f"   - {_item(m)}\n     Billed {_amt(m['amount_cents'], cur)}; the PO gives "
                       f"{_amt(m['expected_cents'], cur)}. Difference {_amt(var, cur)}.")
    if out and over > 0:
        total = run.get("total_cents")
        taxed = bool((run.get("invoice") or {}).get("tax_cents"))
        note = " before tax" if taxed else ""
        out.append(f"   Credit note due: {_amt(over, cur)}{note}"
                   + (f"; corrected invoice total {_amt(total - over, cur)}{' plus the tax on it' if taxed else ''}."
                      if total else "."))
    return out


def _unmatched_lines(run: dict, cur: str) -> list[str]:
    return [f"   - Line {m['inv_index'] + 1}: {m['description']}"
            + (f" (item code {m['sku']})" if m.get("sku") else "")
            + f", {_qty(m['qty_milli'])} x {_amt(m['unit_price_cents'], cur)} = {_amt(m['amount_cents'], cur)}: "
              f"not on the purchase order."
            for m in run.get("line_matches") or [] if m.get("po_line_no") is None and not m.get("bundle")]
