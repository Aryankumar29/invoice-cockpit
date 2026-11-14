"""Node 2 - Extraction: runs the extraction ladder (cache, local, model, OCR, replay) and reports grounding."""
from __future__ import annotations

import inspect

from .. import sources
from ..core.resolve import resolve_vendor
from ..core.rules import money

STAGE, TITLE = "extraction", "Extraction"
_LABELS = {"claude": "Claude", "openai": "OpenAI", "local_text": "local text parser", "local_ocr": "local OCR",
           "replay": "replay cache", "cache": "cached model reading"}


def run(pdf: bytes, run: dict, extractor):
    vendors = sources.records().vendors
    kw = {}
    if "known_vendor" in inspect.signature(extractor).parameters:
        kw["known_vendor"] = lambda i: resolve_vendor(i, vendors).status == "matched"
    res = extractor(pdf, run["file_hash"], run["filename"], **kw)
    inv = res.invoice
    g = res.grounding or {}
    extras = len(getattr(inv, "other_fields", []) or []) + sum(len(getattr(l, "extra", []) or []) for l in inv.lines)
    data = {"source": res.source, "grounding": g, "warnings": res.warnings,
            "notes": getattr(res, "notes", []), "attempts": res.attempts,
            "fields": {"invoice_number": inv.invoice_number, "vendor": inv.vendor_name,
                       "po_reference": inv.po_reference, "total": money(inv.total_cents),
                       "lines": len(inv.lines), "other_fields": extras}}
    if not inv.readable:
        return "warn", "Document could not be read; it will go to a reviewer.", data, res
    msg = (f"Read {len(inv.lines)} line(s), total {money(inv.total_cents)}"
           + (f" and {extras} other field(s)" if extras else "") + f" via {_LABELS.get(res.source, res.source)}; "
           f"{g.get('grounded', 0)}/{g.get('checked', 0)} amounts grounded in the PDF text.")
    return ("warn" if res.warnings or g.get("ungrounded") else "ok"), msg, data, res
