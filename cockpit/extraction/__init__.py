"""Extraction agent A2. The model reads; nothing here decides (docs/DESIGN.md D2-D4).

Chain: [forced replay] -> Claude (retry once, one grounding repair) -> local text/OCR -> replay cache -> unreadable.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cockpit.core.domain import Invoice, InvoiceLine
from cockpit.core.normalize import bank_fingerprint

from . import claude, local, openai_llm
from .parse import currency_of, parse_date, text_amounts, to_cents, to_milli

ARITH_TOL_CENTS = 2
_MIN_TEXT_CHARS = 30
PROVIDER_LABEL = {"claude": "Claude", "openai": "OpenAI"}


def provider():
    """(name, module) of the LLM extractor to use, or (None, None). COCKPIT_LLM=claude|openai forces one;
    otherwise Claude if its key is set, then OpenAI."""
    want = os.environ.get("COCKPIT_LLM", "").lower()
    mods = {"claude": claude, "openai": openai_llm}
    order = [want] if want in mods else ["claude", "openai"]
    for name in order:
        if mods[name].api_key_present():
            return name, mods[name]
    return None, None


@dataclass
class ExtractionResult:
    invoice: Invoice
    source: str  # claude | openai | local_text | local_ocr | replay | none
    grounding: dict
    warnings: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # informational, unlike warnings: never flags the stage


def _s(v) -> str | None:
    v = (str(v).strip() if v is not None else "") or None
    return None if v and v.lower() in ("null", "none", "n/a") else v


def _alnum(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def verify_text(value: str, text: str) -> bool | None:
    """Is a captured value printed in the document text? None when there is no text layer to check against.
    Address blocks arrive joined with ", "; each part must be present (punctuation and spacing ignored)."""
    if len(re.sub(r"\s", "", text or "")) < _MIN_TEXT_CHARS:
        return None
    if len(_alnum(value)) < 4:  # "0%", "4": a substring test would match almost any page; require the token
        v = re.escape(re.sub(r"\s+", " ", (value or "").strip()))
        return bool(v) and re.search(rf"(?<![\w.]){v}(?![\w])", text, re.I) is not None
    hay = _alnum(text)
    parts = [p for p in (_alnum(x) for x in re.split(r",\s*|\n", value or "")) if p]
    return bool(parts) and all(p in hay for p in parts)


def _pair(f: dict, text: str) -> dict | None:
    label, value = _s(f.get("label")), _s(f.get("value"))
    if not value:
        return None
    return {"label": label or "", "value": value, "verified": verify_text(value, text)}


def _pairs(items, text: str) -> list[dict]:
    return [p for p in (_pair(f, text) for f in items or [] if isinstance(f, dict)) if p]


def _section(s) -> str:
    return s if s in claude.SECTIONS else "other"


_RATE = r"(?:\d+(?:[.,]\d+)?\s*%\s*)?"
_TAXES = r"(?:sales\s+)?(?:tax(?:es)?|vat|gst|hst|iva|mwst)"
# "Prices include 8.25% sales tax", "Total incl. VAT", "Sales Tax 8.25% (included)", "VAT inclusive".
# Never "excl. VAT", "Tax exempt" or "Tax ID".
TAX_INCLUDED = re.compile(
    rf"\b(?:prices?|amounts?|rates?|totals?|all\s+prices)\s+(?:are\s+|shown\s+)*(?:inclusive\s+of|incl(?:\.|udes?|uding|usive)?)"
    rf"\s+(?:of\s+)?(?:all\s+)?(?:applicable\s+)?{_RATE}{_TAXES}\b"
    rf"|\b{_TAXES}\s*{_RATE}\(?\s*(?:included|incl\.|inclusive)"
    rf"|\bincl(?:\.|uding|usive)\s+(?:of\s+)?{_RATE}{_TAXES}\b", re.I)


def says_tax_inclusive(text: str, inv: Invoice) -> bool:
    if TAX_INCLUDED.search(text or ""):
        return True
    pairs = list(inv.other_fields) + [e for l in inv.lines for e in l.extra]
    return any(TAX_INCLUDED.search(f"{p.get('label', '')} {p.get('value', '')}") for p in pairs)


def to_invoice(raw: dict, text: str, file_hash: str) -> tuple[Invoice, list[str], set[str]]:
    """Raw printed strings -> domain Invoice. Returns (invoice, warnings, derived field paths)."""
    warnings, derived, lines = [], set(), []
    for i, r in enumerate(raw.get("lines") or []):
        qty, unit, amt = to_milli(_s(r.get("quantity"))), to_cents(_s(r.get("unit_price"))), to_cents(_s(r.get("amount")))
        if amt is None and qty is not None and unit is not None:
            amt = qty * unit // 1000
            derived.add(f"lines[{i}].amount")
            warnings.append(f"Line {i + 1}: amount not printed; computed as quantity x unit price.")
        if qty is None and amt is not None:
            qty, unit = 1000, unit if unit is not None else amt
            derived.add(f"lines[{i}].unit_price")
            warnings.append(f"Line {i + 1}: no quantity printed; treated as 1.")
        if amt is None:
            warnings.append(f"Line {i + 1}: no amount could be read; line dropped.")
            continue
        lines.append(InvoiceLine(_s(r.get("description")) or "", qty, unit if unit is not None else 0, amt,
                                 _s(r.get("sku")), _pairs(r.get("extra"), text)))
    dates = {}
    for k in ("invoice_date", "due_date"):
        dates[k] = parse_date(_s(raw.get(k)))
        if _s(raw.get(k)) and dates[k] is None:
            warnings.append(f"{k.replace('_', ' ').capitalize()} '{raw.get(k)}' could not be parsed.")
    total = to_cents(_s(raw.get("total")))
    inv = Invoice(
        invoice_number=_s(raw.get("invoice_number")), invoice_date=dates["invoice_date"],
        vendor_name=_s(raw.get("vendor_name")), currency=currency_of(_s(raw.get("currency")), text),
        total_cents=total, lines=lines, subtotal_cents=to_cents(_s(raw.get("subtotal"))),
        tax_cents=to_cents(_s(raw.get("tax"))) or 0, freight_cents=to_cents(_s(raw.get("freight"))) or 0,
        discount_cents=abs(to_cents(_s(raw.get("discount"))) or 0),
        due_date=dates["due_date"], vendor_tax_id=_s(raw.get("vendor_tax_id")),
        po_reference=_s(raw.get("po_reference")), bank_hash=bank_fingerprint(_s(raw.get("bank_account"))),
        file_hash=file_hash,
    )
    inv.other_fields = [p | {"section": _section(f.get("section"))}
                        for f, p in ((f, _pair(f, text)) for f in raw.get("other_fields") or [] if isinstance(f, dict))
                        if p]
    inv.tax_inclusive = says_tax_inclusive(text, inv)
    if inv.tax_inclusive:
        warnings.append("The invoice states its prices include tax; the PO checks compare them net of tax.")
    inv.readable = bool(inv.invoice_number or inv.total_cents is not None or inv.vendor_name or inv.lines)
    for k, label in (("invoice_number", "invoice number"), ("total_cents", "total"), ("currency", "currency")):
        if inv.readable and getattr(inv, k) is None:
            warnings.append(f"No {label} found on the document; left blank (never inferred).")
    if raw.get("suspicious_text"):
        warnings.append(f"Document contains instruction-like text, ignored: {raw['suspicious_text'][0][:120]!r}")
    return inv, warnings, derived


def _amount_fields(inv: Invoice, derived: set[str]) -> list[tuple[str, int]]:
    fields = [("subtotal", inv.subtotal_cents), ("discount", inv.discount_cents), ("tax", inv.tax_cents),
              ("freight", inv.freight_cents),
              ("total", inv.total_cents)]
    for i, l in enumerate(inv.lines):
        fields += [(f"lines[{i}].unit_price", l.unit_price_cents), (f"lines[{i}].amount", l.amount_cents)]
    return [(p, v) for p, v in fields if v and p not in derived]


def ground(inv: Invoice, text: str, derived: set[str] = frozenset(), corroborate: str = "") -> dict:
    """D4: every printed amount must exist in the text layer; arithmetic must reconcile within 2 cents.

    `text` is the digital text layer only. On a mixed document, an amount that is not in the text layer but is in
    the OCR of the scanned pages (`corroborate`) counts as grounded: two independent readers agree."""
    has_text = len(re.sub(r"\s", "", text or "")) >= _MIN_TEXT_CHARS
    amounts = text_amounts(text) if has_text else set()
    extra = text_amounts(corroborate) if has_text and corroborate else set()
    checked = _amount_fields(inv, derived) if has_text else []
    corroborated = [p for p, v in checked if not {v, -v} & amounts and {v, -v} & extra]
    ungrounded = [p for p, v in checked if not {v, -v} & (amounts | extra)]
    problems = []
    if inv.total_cents is None or not inv.lines:
        problems.append("no total or no line items to reconcile")
    else:
        for i, l in enumerate(inv.lines):
            if abs(l.qty_milli * l.unit_price_cents // 1000 - l.amount_cents) > ARITH_TOL_CENTS:
                problems.append(f"line {i + 1}: quantity x unit price != amount")
        s = sum(l.amount_cents for l in inv.lines)
        if inv.subtotal_cents is not None and abs(inv.subtotal_cents - s) > ARITH_TOL_CENTS:
            problems.append("subtotal != sum of lines")
        if abs(s - inv.discount_cents + inv.tax_cents + inv.freight_cents - inv.total_cents) > ARITH_TOL_CENTS:
            problems.append("lines - discount + tax + freight != total")
    g = {"checked": len(checked), "grounded": len(checked) - len(ungrounded), "ungrounded": ungrounded,
         "arithmetic_ok": not problems, "arithmetic_problems": problems, "text_layer": has_text}
    if corroborated:
        g["ocr_corroborated"] = corroborated
    return g


def crosscheck(inv: Invoice, ocr_text: str, derived: set[str] = frozenset()) -> dict:
    """Advisory on scans: which model-read amounts Tesseract also read. OCR misreads digits, so a disagreement
    is shown to the reviewer, never used to reject a model reading on its own."""
    amounts = text_amounts(ocr_text)
    fields = _amount_fields(inv, derived)
    return {"checked": len(fields), "agreed": sum(1 for _, v in fields if {v, -v} & amounts),
            "disagreed": [p for p, v in fields if not {v, -v} & amounts]}


def _problems(g: dict) -> list[str]:
    return ([f"{p} does not appear anywhere in the document text" for p in g["ungrounded"]]
            + g["arithmetic_problems"])


def _cache_path(file_hash: str) -> Path:
    key = re.sub(r"[^A-Za-z0-9_-]", "", file_hash or "") or "nohash"
    return Path(os.environ.get("COCKPIT_REPLAY_DIR", "data/replay")) / f"{key}.json"


def _cache_read(file_hash: str) -> dict | None:
    try:
        return json.loads(_cache_path(file_hash).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prompt_version() -> str:
    """Changes whenever the prompt or the schema changes, so a cached reading is never reused across them."""
    import hashlib
    return hashlib.sha256((claude.SYSTEM + json.dumps(claude.SCHEMA, sort_keys=True)).encode()).hexdigest()[:12]


def _cache_write(file_hash: str, raw: dict, source: str = "claude", model: str = claude.MODEL) -> str | None:
    p = _cache_path(file_hash)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"raw": raw, "source": source, "model": model, "prompt_version": prompt_version(),
                                 "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                indent=1, sort_keys=True), encoding="utf-8")
        return None
    except OSError as e:
        return f"Replay cache not written: {e}"


@dataclass
class _Candidate:
    source: str
    raw: dict
    invoice: Invoice
    grounding: dict
    warnings: list[str]
    derived: set[str] = field(default_factory=set)

    @property
    def clean(self) -> bool:
        return not self.grounding["ungrounded"] and self.grounding["arithmetic_ok"]


def _verify_by_ocr(inv: Invoice, ocr: str) -> None:
    """Extra fields on scanned pages: verified when Tesseract read (nearly) the same text. Unverified ones stay,
    flagged, because OCR misreads are common; they never affect a decision."""
    from rapidfuzz import fuzz
    hay = _alnum(ocr)
    for f in [*inv.other_fields, *(x for l in inv.lines for x in l.extra)]:
        if f.get("verified") is None and hay:
            v = _alnum(f["value"])
            f["verified"] = bool(v) and (v in hay or fuzz.partial_ratio(v, hay) >= 90)


_NOT_A_VENDOR = {"from", "bill to", "invoice", "vendor", "supplier", "seller", "sold by", "remit to", "ship to", "to"}


def _complete(inv: Invoice) -> bool:
    """A local reading good enough to skip the model: every header field the rules need, at least one line,
    and a vendor name that is a name rather than a layout label."""
    v = (inv.vendor_name or "").strip().rstrip(":").lower()
    return bool(inv.invoice_number and inv.invoice_date and inv.total_cents is not None and inv.currency
                and inv.lines and sum(c.isalpha() for c in v) >= 3 and v not in _NOT_A_VENDOR)


_VOTED = (("po_reference", "PO reference"), ("invoice_number", "invoice number"), ("vendor_tax_id", "tax ID"))


def _vote_identifiers(best: _Candidate, readings: list[_Candidate]) -> None:
    """OCR misreads one digit at a time (PO-1046 -> PO-1048), and an identifier that is wrong but valid points at
    the wrong obligation. Each identifier must win a majority of the independent OCR readings; a split vote
    blanks it, so the PO/duplicate checks run without it and route to a human instead of to the wrong record."""
    from collections import Counter

    def key(v):  # OCR look-alikes vote together: "P0-1046" and "PO-1046" are one reading, "PO-1048" another
        return re.sub(r"\s", "", v or "").upper().translate(str.maketrans("OIL", "011"))

    votes = {}
    for f, label in _VOTED:
        vals = [key(getattr(c.invoice, f)) for c in readings]
        counts = Counter(v for v in vals if v)
        votes[f] = dict(counts)
        if not counts:
            continue
        (top, n), *rest = counts.most_common()
        mine = key(getattr(best.invoice, f))
        if rest and rest[0][1] == n:
            setattr(best.invoice, f, None)
            best.warnings.append(f"OCR readings disagree on the {label} ({', '.join(counts)}); left blank, "
                                 "not guessed.")
        elif mine != top:
            winner = next(getattr(c.invoice, f) for c, v in zip(readings, vals) if v == top)
            best.warnings.append(f"OCR {label}: {winner!r} read by {n} of {len(readings)} passes; "
                                 f"{getattr(best.invoice, f)!r} discarded.")
            setattr(best.invoice, f, winner)
    best.grounding["ocr_votes"] = votes


def _fill_dates(best: _Candidate, readings: list[_Candidate]) -> None:
    """A date one OCR pass could not read may be legible in another. Only dates are filled this way: they parse
    or they don't. Amounts come from the reading that reconciles; identifiers go through the vote."""
    from collections import Counter
    for f, label in (("invoice_date", "invoice date"), ("due_date", "due date")):
        if getattr(best.invoice, f) is not None:
            continue
        counts = Counter(getattr(c.invoice, f) for c in readings if getattr(c.invoice, f) is not None)
        if not counts:
            continue
        (top, n), *rest = counts.most_common()
        if rest and rest[0][1] == n:
            continue  # disagreeing readings: leave it blank
        setattr(best.invoice, f, top)
        best.warnings.append(f"OCR {label} {top.isoformat()} taken from another OCR pass ({n} of {len(readings)}).")


def _candidate(source, raw, text, file_hash, gtext: str | None = None, corroborate: str = "") -> _Candidate:
    inv, warns, derived = to_invoice(raw, text, file_hash)
    c = _Candidate(source, raw, inv, ground(inv, text if gtext is None else gtext, derived, corroborate), warns)
    c.derived = derived
    return c


def extract(pdf_bytes: bytes, file_hash: str, filename: str = "", known_vendor=None) -> ExtractionResult:
    """known_vendor(invoice) -> bool, when given, limits the free local-first path to vendors already in the
    vendor master: a new vendor means a template not seen before, which always gets the full model read so that
    every company-specific field is captured."""
    attempts, warnings, candidates = [], [], []
    mode = os.environ.get("COCKPIT_EXTRACTION", "auto").lower()

    def step(name, fn):
        t0 = time.perf_counter()
        try:
            ok, detail, out = fn()
        except Exception as e:  # a broken route is a failed attempt, never a crash
            ok, detail, out = False, f"{type(e).__name__}: {e}", None
        attempts.append({"step": name, "ok": ok, "detail": detail, "ms": int((time.perf_counter() - t0) * 1000)})
        return out

    def done(c: _Candidate, extra=(), notes=()) -> ExtractionResult:
        return ExtractionResult(c.invoice, c.source, c.grounding, warnings + list(extra) + c.warnings, attempts, c.raw,
                                list(notes))

    pinfo = step("text_layer", lambda: (True, "", local.page_texts(pdf_bytes))) or []
    digital = [p for p in pinfo if p["class"] == "digital"]
    scanned = [p["page"] for p in pinfo if p["class"] != "digital"]
    text = "\n".join(p["text"] for p in pinfo)       # every visible, readable character: for parsing
    gtext = "\n".join(p["text"] for p in digital)    # digital pages only: the grounding reference
    attempts[-1]["detail"] = (f"{len(pinfo)} page(s): " + ", ".join(f"p{p['page']} {p['class']}" for p in pinfo)
                              + f"; {len(text.strip())} characters")
    attempts[-1]["pages"] = [{k: p[k] for k in ("page", "class", "metrics", "hidden")} for p in pinfo]
    for p in pinfo:
        for why, s in p["hidden"].items():
            warnings.append(f"Hidden text on page {p['page']} ({why}) was removed before reading: {s[:100]!r}")
    garbled = [p["page"] for p in pinfo if p["class"] == "garbled"]
    if garbled:
        warnings.append(f"Page(s) {', '.join(map(str, garbled))}: the text layer is not real text (unmapped font); "
                        "treated as scanned.")

    ocr_state: dict = {}

    def ocr_pages():
        """OCR of the non-digital pages, run once and only when needed. None when there is nothing to OCR."""
        if "pages" not in ocr_state:
            ocr_state["pages"] = None
            if scanned and local.tesseract_available():
                pages = step("ocr", lambda: (True, "", local.ocr_pages(pdf_bytes, scanned)))
                if pages:
                    rot = [f"p{p['page']} {p['rotation']}deg" for p in pages if p["rotation"]]
                    sk = [f"p{p['page']} {p['skew']:+g}deg" for p in pages if abs(p.get("skew", 0)) >= 0.5]
                    attempts[-1]["detail"] = (f"{len(pages)} page(s) OCR'd" + (f"; rotated {', '.join(rot)}" if rot else "")
                                              + (f"; deskewed {', '.join(sk)}" if sk else ""))
                    ocr_state["pages"] = pages
        return ocr_state["pages"]

    def ocr_text(mode: str | None = None) -> str:
        pages = ocr_pages() or []
        if mode:
            out = []
            for p in pages:
                try:
                    out.append(p["texts"][mode])
                except Exception:  # one failed OCR pass drops out of the vote; it never fails the read
                    pass
            return "\n".join(out)
        return "\n".join(t for p in pages for _, t in p["texts"].items())

    def model_candidate(source, raw):
        corroborate = ocr_text() if digital and scanned else ""
        c = _candidate(source, raw, text, file_hash, gtext, corroborate)
        if scanned and ocr_pages():
            _verify_by_ocr(c.invoice, ocr_text())
        if scanned and not digital and ocr_pages():
            x = crosscheck(c.invoice, ocr_text(), c.derived)
            c.grounding["ocr_crosscheck"] = x
            if x["disagreed"]:
                c.warnings.append(f"OCR cross-check: {x['agreed']}/{x['checked']} amounts also read by Tesseract; "
                                  f"not confirmed: {', '.join(x['disagreed'])}. Compare with the scan.")
        return c

    def replay():
        hit = _cache_read(file_hash)
        if not hit or not isinstance(hit.get("raw"), dict):
            return False, "no cache entry for this file hash", None
        c = model_candidate("replay", hit["raw"])
        c.warnings.insert(0, f"Extraction replayed from cache (recorded {hit.get('saved_at', 'earlier')}); "
                             "live extraction was not used.")
        return True, f"cache hit, recorded {hit.get('saved_at')}", c

    if mode == "replay":
        c = step("replay_forced", replay)
        if c:
            return done(c)

    # Local readers: text layer for digital pages, OCR (preprocessing, several layout modes) for the rest
    has_text = bool(digital)

    def local_text():
        c = _candidate("local_text", local.parse_text(gtext), gtext, file_hash)
        detail = "grounded" if c.clean else "; ".join(_problems(c.grounding))
        if scanned:
            detail += f"; page(s) {', '.join(map(str, scanned))} not read (no OCR engine)"
        return c.invoice.readable and c.clean, detail, c if c.invoice.readable else None

    def local_ocr():
        if not local.tesseract_available():
            return False, "Tesseract not installed", None
        if not ocr_pages():
            return False, "OCR produced no pages", None
        tried = []
        for m in local.OCR_MODES:  # every mode reads; the first whose reading reconciles supplies the amounts
            merged = "\n".join([gtext, ocr_text(m)]).strip()
            c = _candidate("local_ocr", local.parse_text(merged, ocr=True), merged, file_hash)
            c.grounding = ground(c.invoice, "", set())  # OCR text is not an independent text layer
            c.grounding["ocr_mode"] = m
            c.warnings.insert(0, f"Page(s) {', '.join(map(str, scanned))} read by OCR ({m}); amounts checked by "
                                 "arithmetic only.")
            tried.append(c)
        readable = [c for c in tried if c.invoice.readable]
        best = next((c for c in readable if c.grounding["arithmetic_ok"]), readable[0] if readable else None)
        if not best:
            return False, "nothing readable", None
        _vote_identifiers(best, readable)
        _fill_dates(best, readable)
        ok = best.grounding["arithmetic_ok"]
        return ok, (f"arithmetic reconciles ({best.grounding['ocr_mode']})" if ok
                    else "; ".join(best.grounding["arithmetic_problems"])), best

    # API savers (auto mode). 1) The same bytes were already read by the model: reuse that reading, re-checked
    # against this PDF. 2) A fully digital PDF whose local reading is complete, grounded and reconciles needs
    # no model call at all. Both fall through to the model when their checks do not pass.
    auto = mode == "auto"
    if auto and os.environ.get("COCKPIT_CACHE_FIRST", "1") != "0":
        hit = _cache_read(file_hash)
        if hit and hit.get("prompt_version") == prompt_version() and isinstance(hit.get("raw"), dict):
            c = step("cache", lambda: (lambda c: (c.clean, "earlier model reading of identical bytes; "
                                                  + ("re-grounded" if c.clean else "; ".join(_problems(c.grounding))), c))(
                model_candidate("cache", hit["raw"])))
            if c and c.clean:
                return done(c, notes=[f"Same file was read by {hit.get('model')} on {hit.get('saved_at')}; that reading "
                                "was reused and re-checked against this PDF (no API call)."])
    local_first_tried, local_first_result = False, None
    if auto and digital and not scanned and os.environ.get("COCKPIT_LOCAL_FIRST", "1") != "0":
        local_first_tried = True
        local_first_result = step("local_text", local_text)
        if (local_first_result and local_first_result.clean and _complete(local_first_result.invoice)
                and (known_vendor is None or known_vendor(local_first_result.invoice))):
            return done(local_first_result, notes=["Read by the local parser: every required field present, every amount "
                                             "found in the PDF text, arithmetic reconciles. No model call needed."])

    # LLM (Claude or OpenAI, whichever key is configured)
    name, llm = provider()
    if mode == "local":
        attempts.append({"step": name or "llm", "ok": False, "detail": "skipped: COCKPIT_EXTRACTION=local", "ms": 0})
    elif llm is None:
        attempts.append({"step": "llm", "ok": False, "detail": "skipped: no ANTHROPIC_API_KEY or OPENAI_API_KEY",
                         "ms": 0})
        warnings.append("No API key: local extractor used.")
    else:
        client = step(f"{name}_client", lambda: (True, "", llm.make_client()))

        def first():
            raw, note, n = llm.call(client, pdf_bytes)
            c = model_candidate(name, raw)
            return c.clean, f"{note}; {n} call(s); " + ("grounded" if c.clean else "; ".join(_problems(c.grounding))), c

        c = step(name, first) if client else None
        if c and not c.clean:
            def repair():
                raw, note, _ = llm.call(client, pdf_bytes, llm.repair_instruction(c.raw, _problems(c.grounding)), 0)
                r = model_candidate(name, raw)
                return r.clean, f"{note}; " + ("grounded" if r.clean else "; ".join(_problems(r.grounding))), r
            r = step(f"{name}_repair", repair)
            if r and (r.clean or not r.grounding["ungrounded"]):
                c = r
            if not c.clean:
                candidates.append(c)
        if c and c.clean:
            err = _cache_write(file_hash, c.raw, name, llm.model() if name == "openai" else llm.MODEL)
            return done(c, [err] if err else [])
        if client:
            warnings.append(f"{PROVIDER_LABEL[name]} extraction did not pass ({attempts[-1]['detail']}); falling back.")

    # Local fallback (or the first reader, see local-first above)
    if scanned:
        c = step("local_ocr", local_ocr)
        if attempts[-1]["detail"] == "Tesseract not installed":
            warnings.append("Scanned page and no OCR engine available (Tesseract not installed).")
            c = step("local_text", local_text) if has_text else None
    else:
        c = local_first_result if local_first_tried else step("local_text", local_text)
    if c and (c.clean or c.source == "local_ocr" and c.grounding["arithmetic_ok"]):
        return done(c)
    if c:
        candidates.append(c)

    if mode != "replay":
        r = step("replay", replay)
        if r:
            return done(r)

    # Nothing passed cleanly. A candidate whose every amount is in the document but whose arithmetic fails is
    # the document's own math: pass it on and let R03 decide. Ungrounded amounts never reach the rules.
    grounded = [c for c in candidates if not c.grounding["ungrounded"]]
    if grounded:
        return done(grounded[0], ["Totals do not reconcile on the document; the rules will decide."])
    if candidates:
        c = candidates[0]
        c.invoice.readable = False
        return done(c, [f"Extracted amounts not found in the document text ({', '.join(c.grounding['ungrounded'])}); "
                        "treated as unreadable (fail closed)."])
    attempts.append({"step": "none", "ok": False, "detail": "every extraction route failed", "ms": 0})
    inv = Invoice(None, None, None, None, None, file_hash=file_hash, readable=False)
    g = {"checked": 0, "grounded": 0, "ungrounded": [], "arithmetic_ok": False,
         "arithmetic_problems": ["nothing extracted"], "text_layer": has_text}
    return ExtractionResult(inv, "none", g, warnings + ["Document could not be read by any extraction route."],
                            attempts, {})
