"""Node 0 - Mail triage: is this email an invoice, something else, suspicious, or unclear?
Deterministic and local (no model call): sender against the vendor master, wording, and the text of each attached
PDF. The matching keys from the PO book tie an email to a PO when at least two of three agree, one of them the
vendor: PO number, vendor (sender domain or name, and the PO's own vendor), buyer company (bill-to on the invoice)."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..core.normalize import norm_text
from ..sources import Records
from .message import Attachment, MailMessage

LABELS = ("invoice", "not_invoice", "suspicious", "unsure")

_PO = re.compile(r"\bP\.?\s?O\.?\s*(?:number|no\.?|#|:)?\s*[-#:]?\s*(\d{3,8})\b", re.I)
_INVOICE_HINT = re.compile(r"\b(invoice|inv[-#\s]?\d|bill(?:ing)? for|amount due|payment due|balance due|"
                           r"tax invoice|remit)\b", re.I)
_BANK_CHANGE = re.compile(r"(new|updated|changed?|change of|different)\s+(bank|banking|account|remittance|"
                          r"payment)\s*(details|information|info|account)?|(bank|account)\s+(details\s+)?"
                          r"(has|have)\s+changed|pay\s+(future|all)\s+invoices\s+to|update\s+(your|our)\s+"
                          r"(records|payment details)", re.I)
_URGENT = re.compile(r"\b(urgent|immediately|today only|final notice|wire)\b", re.I)
_LINK_INVOICE = re.compile(r"(view|download|access)\s+(your\s+)?invoice|invoice\s+is\s+(ready|available)|"
                           r"https?://\S+", re.I)

# What a document says it is, from its own words. Head = first ~400 characters, where the title sits.
_KINDS = {
    "statement": re.compile(r"statement of account|account statement|statement date|opening balance|aging summary",
                            re.I),
    "quote": re.compile(r"\bquotation\b|\bquote\s*(no|number|#)|\bestimate\s*(no|number|#)|\bproposal\b|"
                        r"valid until", re.I),
    "order_confirmation": re.compile(r"order confirmation|sales order\s*(no|number|#)|acknowledg(e)?ment of order",
                                     re.I),
    "remittance": re.compile(r"remittance advice|payment receipt|paid in full|receipt\s*(no|number|#)", re.I),
    "credit_note": re.compile(r"credit (note|memo)", re.I),
    "invoice": re.compile(r"\binvoice\b|\btax invoice\b", re.I),
}
_INVOICE_BODY = re.compile(r"invoice\s*(no|number|#|date)|amount due|balance due|total due|bill to|due date", re.I)
KIND_LABEL = {"invoice": "invoice", "credit_note": "credit note", "statement": "statement of account",
              "quote": "quotation", "order_confirmation": "order confirmation", "remittance": "payment receipt",
              "scan": "scanned document", "unreadable": "unreadable file", "other": "other file",
              "risky": "risky file type"}


@dataclass
class DocCheck:
    filename: str
    kind: str
    text: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass
class Triage:
    label: str
    reasons: list[str]
    keys: dict
    vendor_id: str | None
    docs: list[DocCheck]
    signals: dict

    @property
    def invoice_docs(self) -> list[DocCheck]:
        return [d for d in self.docs if d.kind in ("invoice", "credit_note") or d.kind == "scan" and
                self.signals.get("invoice_wording")]


def pdf_text(data: bytes, pages: int = 3) -> str | None:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:pages])
    except Exception:
        return None


def classify_doc(a: Attachment) -> DocCheck:
    if a.is_risky:
        return DocCheck(a.filename, "risky", reasons=[f"{a.filename}: file type is not accepted for invoices"])
    if not a.is_document:
        return DocCheck(a.filename, "other")
    if not (a.content_type == "application/pdf" or a.filename.lower().endswith(".pdf")):
        return DocCheck(a.filename, "scan", reasons=[f"{a.filename}: image, read later by OCR"])
    text = pdf_text(a.data)
    if text is None:
        return DocCheck(a.filename, "unreadable", reasons=[f"{a.filename}: PDF could not be opened"])
    if len(text.strip()) < 40:
        return DocCheck(a.filename, "scan", text, [f"{a.filename}: scanned PDF, read later by OCR"])
    head = text[:400]
    for kind in ("credit_note", "statement", "quote", "order_confirmation", "remittance", "invoice"):
        if _KINDS[kind].search(head):
            if kind == "invoice" or kind == "credit_note":
                return DocCheck(a.filename, kind, text, [f"{a.filename}: titled as {KIND_LABEL[kind]}"])
            return DocCheck(a.filename, kind, text, [f"{a.filename}: {KIND_LABEL[kind]}, not an invoice"])
    if len(_INVOICE_BODY.findall(text)) >= 2:
        return DocCheck(a.filename, "invoice", text, [f"{a.filename}: has invoice number, due date and totals"])
    return DocCheck(a.filename, "other", text, [f"{a.filename}: does not read as an invoice"])


def po_numbers(text: str) -> list[str]:
    seen = []
    for m in _PO.finditer(text or ""):
        n = f"PO-{m.group(1)}"
        if n not in seen:
            seen.append(n)
    return seen


def _sender_vendor(msg: MailMessage, rec: Records) -> tuple[str | None, str | None]:
    """(vendor_id by exact domain, vendor_id the sender imitates). The second is set for look-alike domains or a
    vendor's display name on an address outside its domain."""
    dom = msg.domain
    for vid, c in rec.contacts.items():
        if c.get("domain") and (dom == c["domain"] or dom.endswith("." + c["domain"])):
            return vid, None
    for vid, c in rec.contacts.items():
        d = c.get("domain") or ""
        if d and dom and fuzz.ratio(dom, d) >= 85:
            return None, vid
    name = norm_text(msg.sender_name)
    for v in rec.vendors:
        if name and fuzz.token_set_ratio(name, norm_text(v.name)) >= 90:
            return None, v.id
    return None, None


def _vendor_in_text(text: str, rec: Records) -> str | None:
    low = norm_text(text)
    for v in rec.vendors:
        core = norm_text(re.sub(r"\b(inc|llc|ltd|co|corp|company)\b\.?", "", v.name, flags=re.I))
        if core and core in low:
            return v.id
    return None


def match_keys(msg: MailMessage, docs: list[DocCheck], rec: Records, sender_vid: str | None,
               imitated: str | None = None) -> dict:
    texts = [msg.subject, msg.text] + [d.text for d in docs if d.kind in ("invoice", "credit_note")]
    found = []
    for t in texts:
        found += [p for p in po_numbers(t) if p not in found]
    known = [p for p in found if p in rec.pos]
    po = rec.pos.get(known[0]) if known else None
    named_vid = None
    for t in texts:
        named_vid = named_vid or _vendor_in_text(t, rec)
    vid = None if imitated else sender_vid or named_vid  # a look-alike sender never stands in for the vendor
    buyer = rec.buyers.get(po.po_number) if po else next(iter(set(rec.buyers.values())), None)
    doc_text = norm_text(" ".join(texts))
    keys = {
        "po_number": {"ok": po is not None, "value": po.po_number if po else (found[0] if found else None),
                      "note": "on the PO book" if po else ("not on the PO book" if found else "no PO number")},
        "vendor": {"ok": bool(vid and (po is None or po.vendor_id == vid)), "value": vid,
                   "note": ("sender imitates a vendor" if imitated else "sender domain" if sender_vid else
                            "vendor name in the email" if named_vid else "vendor not recognised")},
        "buyer": {"ok": bool(buyer and norm_text(buyer) in doc_text), "value": buyer,
                  "note": "bill-to matches our company" if buyer and norm_text(buyer) in doc_text
                  else "our company is not named"},
    }
    if po is not None and vid and po.vendor_id != vid:
        keys["vendor"]["note"] = f"{po.po_number} was issued to {po.vendor_id}, not {vid}"
    keys["matched"] = sum(1 for k in ("po_number", "vendor", "buyer") if keys[k]["ok"])
    keys["confirmed"] = keys["matched"] >= 2 and keys["vendor"]["ok"]  # PO number + bill-to alone prove no sender
    keys["all_po_numbers"] = found
    return keys


def triage(msg: MailMessage, rec: Records) -> Triage:
    docs = [classify_doc(a) for a in msg.attachments if a.is_document or a.is_risky]
    words = f"{msg.subject}\n{msg.text}"
    sender_vid, imitated = _sender_vendor(msg, rec)
    keys = match_keys(msg, docs, rec, sender_vid, imitated)
    signals = {"invoice_wording": bool(_INVOICE_HINT.search(words)), "bank_change": bool(_BANK_CHANGE.search(words)),
               "urgent": bool(_URGENT.search(words)), "lookalike_of": imitated, "sender_vendor": sender_vid,
               "reply_to_differs": bool(msg.reply_to and msg.reply_to.lower() != msg.sender),
               "documents": len(docs)}
    reasons: list[str] = []
    fraud = []
    if imitated:
        v = rec.vendor(imitated)
        fraud.append(f"Sender {msg.sender} looks like {v.name if v else imitated} but is not its address on the "
                     f"vendor master ({(rec.contacts.get(imitated) or {}).get('domain')}).")
    if signals["bank_change"]:
        fraud.append("Asks to change bank or payment details. Verify by phone on the number in the vendor master, "
                     "never by replying to this email.")
    if any(d.kind == "risky" for d in docs):
        fraud.append("Carries a file type invoices never come in (" +
                     ", ".join(d.filename for d in docs if d.kind == "risky") + ").")
    if signals["reply_to_differs"] and (sender_vid or imitated):
        fraud.append(f"Replies would go to {msg.reply_to}, not the sender.")

    inv_docs = [d for d in docs if d.kind in ("invoice", "credit_note") or d.kind == "scan" and
                signals["invoice_wording"]]
    if fraud:
        label = "suspicious"
        reasons += fraud + ["Held for a person; nothing is sent to the pipeline until someone releases it."]
    elif inv_docs:
        label = "invoice"
        reasons.append(f"{len(inv_docs)} invoice document(s) attached.")
        reasons.append(f"Matching keys {keys['matched']}/3: " + ("confirmed against the PO book."
                       if keys["confirmed"] else "not confirmed; the pipeline will check vendor and PO itself."))
        if not sender_vid:
            reasons.append(f"{msg.sender} is not a vendor address on file.")
    elif any(d.kind in ("statement", "quote", "order_confirmation", "remittance") for d in docs):
        label = "not_invoice"
        reasons.append("Attachment is a " + ", ".join(sorted({KIND_LABEL[d.kind] for d in docs if d.kind in
                       ("statement", "quote", "order_confirmation", "remittance")})) + ", not an invoice.")
    elif signals["invoice_wording"] and not docs:
        label = "unsure"
        reasons.append("Talks about an invoice but has no invoice attached" +
                       (" (only a link; links are never followed)." if _LINK_INVOICE.search(msg.text) else "."))
    elif any(d.kind in ("scan", "unreadable", "other") for d in docs):
        label = "unsure"
        reasons.append("Has a document that does not clearly read as an invoice.")
    else:
        label = "not_invoice"
        reasons.append("No invoice attached and no invoice wording.")
    for d in docs:
        reasons += [r for r in d.reasons if r not in reasons]
    return Triage(label, reasons, keys, sender_vid, docs, signals)
