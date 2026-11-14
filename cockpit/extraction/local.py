"""Local extractor: pdfplumber text layer (digital) or Tesseract OCR (scans), then a label/table parser.

Produces the same raw shape as the Claude route: printed strings, no arithmetic, no guessing.
"""
from __future__ import annotations

import io
import re

from .parse import DATE, MONEY

FIELDS = ["invoice_number", "invoice_date", "due_date", "vendor_name", "vendor_tax_id", "po_reference",
          "currency", "bank_account", "subtotal", "discount", "tax", "freight", "total"]

_ID = r"(?=[A-Za-z0-9/\-_.]*\d)[A-Za-z0-9](?:[A-Za-z0-9/\-_.]*[A-Za-z0-9])?"
_SEP = r"[ \t]*(?:\([^)\n]*\))?[ \t]*[:#]?[ \t]*(?:(?:USD|EUR|GBP|CAD|AUD|INR|JPY|CHF)[ \t]+)?"
_LEGAL = re.compile(r"\b(inc|llc|ltd|limited|co|corp|corporation|company|gmbh|plc|llp|ag|sa|pvt|pbc|pty|bv|nv|srl|sas)\b\.?", re.I)
_PARTY = re.compile(r"^\s*(bill|ship|sold|deliver|invoice)\s*to\b", re.I)
_SUSPICIOUS = re.compile(
    r"\b(ignore|disregard|override)\b.{0,60}\b(instruction|rule|check|polic|previous|above)"
    r"|\b(auto-?approve|approve)\b.{0,40}\b(invoice|immediately|automatically|payment)"
    r"|system prompt|\bas an ai\b|you are (an?|the) (ai|assistant|model)", re.I)


def _label(labels: str, value: str) -> re.Pattern:
    return re.compile(rf"(?:{labels}){_SEP}({value})", re.I)


_PATTERNS = {
    "invoice_number": [_label(r"\bInvoice\s*(?:Number|No\.?|Num\.?|#|ID)|\bInv\.?\s*(?:No\.?|#)|\bInvoice(?=\s*:)|\bBill\s*(?:No\.?|Number|#)", _ID)],
    "invoice_date": [_label(r"\b(?:Invoice|Bill|Billing|Issue)\s*Date|\bDate\s+of\s+(?:Invoice|issue)|\bIssued(?:\s+on)?|\bDated", DATE),
                     _label(r"(?<!due )(?<!ship )(?<!order )(?<!delivery )(?<!payment )(?<!service )(?<!expiry )\bDate\b", DATE)],
    "due_date": [_label(r"\bDue\s*Date|\bPayment\s*Due(?:\s*Date)?|\bDue\s*(?:on|by)?", DATE)],
    "po_reference": [_label(r"\b(?:Your|Customer|Buyer)\s*PO(?:\s*(?:Number|No\.?|#))?|\bPurchase\s*Order"
                            r"(?:\s*(?:Number|No\.?|#|Ref(?:erence)?))?|\bP\.?O\.?(?:\s*(?:Number|No\.?|#|Ref))?",
                            _ID),
                     re.compile(r"\b(PO[-\s]?\d{3,}[A-Za-z0-9-]*)\b")],
    "vendor_tax_id": [_label(r"\bTax\s*(?:ID|Identification\s*Number|No\.?)|\bEIN\b|\bTIN\b|\bVAT\s*(?:ID|No\.?|Number)",
                             r"[A-Za-z]{0,2}\d[\d\-]*\d")],
    "bank_account": [_label(r"\bRemit(?:tance)?\s*to\s*account|\bBank\s*Account(?:\s*(?:Number|No\.?|#))?"
                            r"|\bAccount\s*(?:Number|No\.?|#)|\bAcct\.?\s*(?:Number|No\.?|#)|\bIBAN",
                            r"(?:[A-Z]{2}\d{2})?\d[\d\- ]*\d")],
    "subtotal": [_label(r"\bSub[\s-]?total|\bNet(?:\s*(?:Amount|Total))?\b", MONEY)],
    "tax": [_label(r"(?m:^)\s*(?:Sales\s*)?Tax\b(?!\s*(?:ID|No|Number|Identification|Exempt|payer|Invoice))"
                   r"(?:\s*@?\s*\d+(?:[.,]\d+)?\s*%)?", MONEY),  # "Sales Tax 8.25% 165.00": the amount, not the rate
            re.compile(rf"(?m)^\s*(?:GST|HST|PST|QST|VAT)\b(?!\s*(?:ID|No|Number|Reg))[^\n]*?\s((?:{MONEY}))\s*$", re.I)],
    "discount": [_label(r"\b(?:Discount|Rebate)(?:\s*\([^)\n]*\))?", rf"-?\s*(?:{MONEY})")],
    "freight": [_label(r"\bFreight|\bShipping(?:\s*(?:&|and)\s*Handling)?|\bDelivery\s*(?:charge|fee)", MONEY)],
    "total": [_label(r"\b(?:Total\s*(?:Amount\s*)?Due|Amount\s*Due|Balance\s*Due|Grand\s*Total|Invoice\s*Total|Amount\s*Payable|Total\s*Payable)", MONEY),
              _label(r"(?<!Sub)(?<!Sub-)(?<!Sub )\bTotal\b", MONEY)],
    "currency": [_label(r"\bCurrency", r"[A-Za-z]{3}\b")],
}
_LAST = {"total", "subtotal", "discount", "tax", "freight"}  # totals sit at the bottom; take the last occurrence

_HEADER = re.compile(r"\bdescription\b|\bitem\b|\bservice\b", re.I)
_HEADER_QTY = re.compile(r"\b([qoa]t[yv]|quantity|hours|days|units)\b", re.I)  # OCR: "aty", "qtv"
_HEADER_AMT = re.compile(r"\b(amount|total|line total|ext\.?)\b", re.I)
_HEADER_SKU = re.compile(r"\b(sku|item\s*(?:code|#|no)|part\s*(?:no|#)?|code)\b", re.I)
_END = re.compile(r"^\s*(sub[\s-]?total|net|total|(sales\s*)?tax|freight|shipping|discount|amount\s*due|balance|vat)\b", re.I)
_PCT = r"(?:\s+-?\d+(?:[.,]\d+)?\s?%)?"
_ROW = re.compile(rf"^(?P<head>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>{MONEY}){_PCT}\s+(?P<amt>{MONEY})\s*$")
_ROW_QTY_FIRST = re.compile(rf"^(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<head>(?:[^\d$€£\s][^\n]*?)?)\s*(?P<unit>{MONEY}){_PCT}"
                            rf"\s+(?P<amt>{MONEY})\s*$")


def _find(field: str, text: str) -> str | None:
    for pat in _PATTERNS[field]:
        hits = [m.group(1).strip() for m in pat.finditer(text)]
        if hits:
            return hits[-1] if field in _LAST else hits[0]
    return None


_NOT_NAME = re.compile(r"\b(registration|number|date|ref\b|reference|page|customer|account|summary|total)", re.I)
_MERGED = re.compile(r"\s+(?:(?:bill|ship|sold)\s+to\b|(?:order|invoice|customer|account|po)\s*(?:number|no\.?|#|id|date)\b).*$"
                     r"|\s+@\w+.*$", re.I)


def _clean_vendor(s: str) -> str:
    """Drop what a merged right-hand column appends to the vendor line ("... Bill to", "... Order Number 12345")."""
    return _MERGED.sub("", s or "").strip(" -:|,")


def _vendor(lines: list[str]) -> str | None:
    lab = re.compile(r"^\s*(?:Vendor|Supplier|From|Seller|Sold\s*By)\s*:\s*(.*)$", re.I)
    for i, l in enumerate(lines):
        m = lab.match(l)
        if m and (m.group(1).strip() or i + 1 < len(lines)):
            return _clean_vendor(m.group(1).strip() or lines[i + 1])
    head = []
    for l in lines[:12]:
        if _PARTY.match(l):  # everything after the bill-to block belongs to the buyer
            break
        name = _clean_vendor(re.sub(r"\b(tax\s+)?invoice\b", "", l, flags=re.I))
        if name and not re.search(r"\d{3}|:", name) and not _NOT_NAME.search(name):
            head.append(name)
    return next((h for h in head if _LEGAL.search(h)), head[0] if head else None)


def _lines(lines: list[str]) -> list[dict]:
    out, in_table, has_sku, pending = [], False, False, ""
    for l in lines:
        if not in_table:
            if _HEADER.search(l) and _HEADER_QTY.search(l) and _HEADER_AMT.search(l):
                in_table, has_sku = True, bool(_HEADER_SKU.search(l))
            continue
        if _END.match(l):
            break
        m = _ROW.match(l.strip())
        if not m and (q := _ROW_QTY_FIRST.match(l.strip())):
            m = q
            if not q.group("head").strip() and len(pending) >= 3:  # description printed on the line above
                if out and out[-1]["description"].endswith(" " + pending):
                    out[-1]["description"] = out[-1]["description"][:-len(pending) - 1]
                out.append({"sku": None, "description": pending, "quantity": q.group("qty"),
                            "unit_price": q.group("unit"), "amount": q.group("amt")})
                pending = ""
                continue
        if not m:
            if l.strip() and not re.search(MONEY, l):
                pending = l.strip()
                if out:
                    out[-1]["description"] += " " + pending
            continue
        pending = ""
        head, sku = m.group("head").strip(), None
        if has_sku:
            first, _, rest = head.partition(" ")
            before, _, last = head.rpartition(" ")
            if rest and _is_sku(first):
                sku, head = first, rest.strip()
            elif before and _is_sku(last):  # some layouts print the item code after the description
                sku, head = last, before.strip()
        out.append({"sku": sku, "description": head, "quantity": m.group("qty"),
                    "unit_price": m.group("unit"), "amount": m.group("amt")})
    return out or _headerless_lines(lines)


def _headerless_lines(lines: list[str]) -> list[dict]:
    """OCR often garbles the table header ("ty" for "Qty"). Without a header, accept only rows that prove
    themselves: description, quantity, unit price, amount, with quantity x unit price = amount (2-cent tolerance)."""
    from .parse import to_cents, to_milli
    out = []
    for l in lines:
        if _END.match(l):
            if out:  # "Tax ID:" above the table must not end a table that has not started
                break
            continue
        m = _ROW.match(l.strip())
        if not m:
            continue
        q, u, a = to_milli(m.group("qty")), to_cents(m.group("unit")), to_cents(m.group("amt"))
        if None in (q, u, a) or not q or abs(q * u // 1000 - a) > 2:
            continue
        head, sku = m.group("head").strip(), None
        first, _, rest = head.partition(" ")
        if rest and _is_sku(first):
            sku, head = first, rest.strip()
        out.append({"sku": sku, "description": head, "quantity": m.group("qty"),
                    "unit_price": m.group("unit"), "amount": m.group("amt")})
    return out


def _is_sku(tok: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9\-_/.]*[A-Z0-9]", tok) and re.search(r"[\d-]", tok)
                and re.search(r"[A-Z]", tok))


def _fuzzy_invoice_date(lines: list[str]) -> str | None:
    """OCR garbles labels ("Invokes Dats: Oct 21, 2026"). The value must still be a printed date; only the two
    words before it may be misspelled, and a due-date line never qualifies."""
    from rapidfuzz import fuzz
    for l in lines:
        for m in re.finditer(DATE, l):
            words = re.findall(r"[A-Za-z]+", l[:m.start()])[-2:]
            if len(words) < 2 or fuzz.ratio(words[0].lower(), "due") >= 67:
                continue
            if fuzz.ratio(" ".join(words).lower(), "invoice date") >= 70 and fuzz.ratio(words[0].lower(), "invoice") >= 70:
                return m.group(0)
    return None


_KV = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z.#/&'()-]*(?:\s+[A-Za-z#/&'()-][A-Za-z.#/&'()-]*){0,3})\s*:\s*"
                 r"(?P<value>\S.{0,150}?)\s*$")
_CORE_LABEL = re.compile(r"^(?:invoice(?:\s*(?:number|no\.?|#|date|total))?|bill\s*(?:no\.?|number|#)|(?:issue\s*)?date"
                         r"|dated|issued|due(?:\s*date)?|payment\s*due|p\.?o\.?(?:\s*(?:number|no\.?|#))?|purchase\s*order"
                         r"|tax\s*id|ein|tin|vat(?:\s*(?:id|no\.?|number))?|sub[\s-]?total|total(?:\s*due)?|(?:sales\s*)?tax"
                         r"|gst|hst|freight|shipping|discount(?:\s*\(.*\))?|currency|bank\s*account|account\s*(?:number|no\.?|#)"
                         r"|vendor|supplier|from|amount\s*(?:due|payable)|balance\s*due)$", re.I)


def _key_values(lines: list[str], core_values=()) -> list[dict]:
    """Generic capture of "Label: value" lines the core fields do not cover (company-specific fields). A label on a
    line of its own followed by a value line ("Ship Mode:" / "Standard Class") is joined. Local reading is
    conservative; templates from new vendors get the full model read, which captures unlabelled blocks too."""
    out, seen = [], set()
    core = {str(v).strip().lower() for v in core_values if v}
    for i, l in enumerate(lines):
        m = _KV.match(l)
        if not m:
            continue
        label, value = m.group("label").strip(), m.group("value").strip()
        if (_CORE_LABEL.match(label.rstrip(" .")) or (label.lower(), value.lower()) in seen or _KV.match(value)
                or value.endswith(":") or value.lower() in core or _CORE_LABEL.match(label.split()[-1])):
            continue
        seen.add((label.lower(), value.lower()))
        out.append({"label": label, "value": value, "section": "other"})
    for i, l in enumerate(lines[:-1]):  # "Ship Mode:" alone, value on the next line
        lab = l.strip()
        if (lab.endswith(":") and ":" not in lab[:-1] and 1 <= len(lab[:-1].split()) <= 4
                and not _CORE_LABEL.match(lab[:-1].strip())):
            nxt = lines[i + 1].strip()
            if (nxt and ":" not in nxt and nxt.lower() not in core
                    and (lab[:-1].lower(), nxt.lower()) not in seen):
                seen.add((lab[:-1].lower(), nxt.lower()))
                out.append({"label": lab[:-1].strip(), "value": nxt, "section": "other"})
    return out


def parse_text(text: str, ocr: bool = False) -> dict:
    lines = [l for l in text.splitlines()]
    raw = {f: _find(f, text) for f in FIELDS if f != "vendor_name"}
    if ocr and not raw["invoice_date"]:
        raw["invoice_date"] = _fuzzy_invoice_date(lines)
    raw["vendor_name"] = _vendor([l for l in lines if l.strip()])
    if raw["bank_account"]:
        raw["bank_account"] = raw["bank_account"].strip()
    raw["lines"] = _lines(lines)
    raw["suspicious_text"] = [l.strip() for l in lines if _SUSPICIOUS.search(l)]
    raw["other_fields"] = _key_values(lines, [v for k, v in raw.items() if isinstance(v, str)])
    return raw


def text_layer(pdf_bytes: bytes) -> list[str]:
    return [p["text"] for p in page_texts(pdf_bytes)]


# ---- per-page routing ----------------------------------------------------------------------------

_CID = re.compile(r"\(cid:\d+\)")


def classify(text: str) -> tuple[str, dict]:
    """digital | garbled | image, with the measured signals. Garbled = enough characters but not real text
    (fonts without a Unicode map, which pdfplumber renders as "(cid:N)" or replacement characters)."""
    cids = len(_CID.findall(text))
    body = _CID.sub("", text)
    chars = re.sub(r"\s", "", body)
    n = len(chars) + cids
    alnum = sum(c.isalnum() for c in chars) / n if n else 0.0
    bad = (cids + sum(c == "�" or (ord(c) < 32) or 0xE000 <= ord(c) <= 0xF8FF for c in chars)) / n if n else 0.0
    m = {"chars": n, "alnum_ratio": round(alnum, 3), "bad_ratio": round(bad, 3)}
    if n < 50:
        return "image", m
    return ("digital" if alnum >= 0.5 and bad < 0.02 else "garbled"), m


def _number_list(c) -> list[float] | None:
    if c is None or isinstance(c, str):
        return None
    c = (c,) if isinstance(c, (int, float)) else c
    try:
        return [float(x) for x in c]
    except (TypeError, ValueError):
        return None


def _is_white(c) -> bool:
    v = _number_list(c)
    if not v:
        return False
    if len(v) == 4:  # CMYK
        return max(v) <= 0.05
    return min(v) >= 0.95


def _hidden_reason(ch: dict, page, dark_fills: list) -> str | None:
    if ch.get("size", 10) < 1.5:
        return "tiny"
    if ch["x1"] < 0 or ch["x0"] > page.width or ch["bottom"] < 0 or ch["top"] > page.height:
        return "off-page"
    if _is_white(ch.get("non_stroking_color")):
        cx, cy = (ch["x0"] + ch["x1"]) / 2, (ch["top"] + ch["bottom"]) / 2
        if not any(r["x0"] <= cx <= r["x1"] and r["top"] <= cy <= r["bottom"] for r in dark_fills):
            return "white-on-white"  # white text on a dark header bar is normal and stays visible
    return None


_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—―−"})


def _unmapped(text: str) -> str:
    """Some fonts map punctuation to NUL ("JB5LUBTC\x000003", "GST - India \x0018%"). Between two letters/digits it
    was a hyphen; elsewhere it is read as a space. Unicode dashes become "-" so identifiers compare as printed."""
    text = re.sub(r"(?<=[A-Za-z0-9])\x00(?=[A-Za-z0-9])", "-", text).replace("\x00", " ")
    return text.translate(_DASHES)


def page_texts(pdf_bytes: bytes) -> list[dict]:
    """Per page: visible text, hidden text runs, and the digital/garbled/image class.

    Hidden text (white on white, off-page, sub-1.5pt) is invisible to a person but readable by any parser or
    model, so it is removed before parsing and grounding and reported instead.
    """
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            fills = [r for r in page.rects if r.get("fill") and not _is_white(r.get("non_stroking_color"))]
            hidden: dict[str, list[str]] = {}
            for ch in page.chars:
                why = _hidden_reason(ch, page, fills)
                if why:
                    hidden.setdefault(why, []).append(ch["text"])
            visible = page.filter(lambda o: o.get("object_type") != "char" or not _hidden_reason(o, page, fills))
            text = _unmapped(visible.extract_text() or "")
            kind, metrics = classify(text)
            out.append({"page": i + 1, "text": text if kind != "garbled" else "", "class": kind,
                        "metrics": metrics, "hidden": {k: "".join(v).strip() for k, v in hidden.items()
                                                       if "".join(v).strip()}})
    return out


_WINDOWS_TESSERACT = (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")


def _configure_tesseract(pytesseract) -> None:
    """Use TESSERACT_CMD, else PATH, else the standard Windows install location (the installer skips PATH)."""
    import os
    import shutil
    cmd = os.environ.get("TESSERACT_CMD")
    if not cmd and not shutil.which("tesseract"):
        cmd = next((p for p in _WINDOWS_TESSERACT if os.path.isfile(p)), None)
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd


def tesseract_available() -> bool:
    try:
        import pytesseract
        _configure_tesseract(pytesseract)
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# Tried in order; the first whose reading reconciles wins. psm 6 = one uniform block, psm 4 = one column of
# variable-size text; "hires" re-reads an upscaled, sharpened copy (low-resolution or heavily compressed scans).
OCR_MODES = ("--psm 6", "--psm 4", "--psm 6 hires", "--psm 11")
# psm 11 (sparse text) splits two-column headers that the block modes merge ("Invoice Date:" sharing a baseline
# with the left column's e-mail). It prints label and value on separate lines; _join_labels rejoins them.


def skew_angle(gray, max_deg: float = 8.0, step: float = 0.5) -> float:
    """Text-line skew by projection profile: rows of text are sharpest (highest row-sum variance) when level."""
    import numpy as np
    small = gray.copy()
    small.thumbnail((900, 900))
    ink = (np.asarray(small) < 128)
    if ink.mean() < 0.002:  # blank page
        return 0.0
    from PIL import Image
    im = Image.fromarray((ink * 255).astype("uint8"))

    def score(a: float) -> float:
        return float(np.var(np.asarray(im.rotate(a, expand=False, fillcolor=0, resample=2)).sum(axis=1,
                                                                                           dtype=np.float64)))

    coarse = max(np.arange(-max_deg, max_deg + step / 2, step), key=lambda a: score(float(a)))
    fine = max(np.arange(coarse - step, coarse + step + 0.05, 0.1), key=lambda a: score(float(a)))
    return round(float(fine), 1)


def _prep(img):
    """Grayscale, stretch contrast, upright via Tesseract's orientation detection (0/90/180/270), then level
    small skew. Returns (image, rotation degrees, skew degrees)."""
    import pytesseract
    from PIL import ImageOps
    g = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
    rotation = 0
    try:
        osd = pytesseract.image_to_osd(g, output_type=pytesseract.Output.DICT)
        if osd.get("orientation_conf", 0) >= 2 and osd.get("rotate", 0):
            rotation = int(osd["rotate"])
            g = g.rotate(-rotation, expand=True, fillcolor=255)
    except Exception:  # OSD needs osd.traineddata and enough text; without it, OCR the page as is
        pass
    skew = 0.0
    try:
        skew = skew_angle(g)
        if abs(skew) >= 0.5:
            g = g.rotate(skew, expand=True, fillcolor=255, resample=3)  # 3 = bicubic
    except Exception:
        skew = 0.0
    return g, rotation, skew


def _hires(g):
    from PIL import Image, ImageFilter
    big = g.resize((int(g.width * 1.6), int(g.height * 1.6)), Image.LANCZOS)
    return big.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=3))


def ocr_pages(pdf_bytes: bytes, pages: list[int] | None = None, dpi: int = 300) -> list[dict]:
    """OCR the given 1-based pages (default all). Per page: text per OCR mode, rotation and skew corrected.
    Modes are computed lazily: `texts` is filled for the first two now; the hi-res pass only on request."""
    import pdfplumber
    import pytesseract
    _configure_tesseract(pytesseract)
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, p in enumerate(pdf.pages):
            if pages is not None and i + 1 not in pages:
                continue
            img, rot, skew = _prep(p.to_image(resolution=dpi).original)
            texts = _LazyTexts(img)
            out.append({"page": i + 1, "rotation": rot, "skew": round(skew, 1), "texts": texts})
    return out


def _join_labels(text: str) -> str:
    """'Invoice Date:' followed by 'Oct 21, 2026' on the next line becomes one 'Invoice Date: Oct 21, 2026' line."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out, i = [], 0
    while i < len(lines):
        l = lines[i]
        if l.endswith(":") and len(l) <= 40 and i + 1 < len(lines) and not lines[i + 1].endswith(":"):
            out.append(f"{l} {lines[i + 1]}")
            i += 2
        else:
            out.append(l)
            i += 1
    return "\n".join(out)


class _LazyTexts(dict):
    """OCR text per mode, computed on first access, so the slow hi-res pass runs only when it is needed."""
    def __init__(self, img):
        super().__init__()
        self._img = img

    def _get(self, mode):
        if not dict.__contains__(self, mode):
            import pytesseract
            img = _hires(self._img) if mode.endswith("hires") else self._img
            text = pytesseract.image_to_string(img, config=mode.replace(" hires", ""))
            dict.__setitem__(self, mode, _join_labels(text) if mode == "--psm 11" else text)
        return dict.__getitem__(self, mode)

    def __getitem__(self, mode):
        return self._get(mode)

    def items(self):
        return [(m, self._get(m)) for m in OCR_MODES[:2]] + [(m, dict.__getitem__(self, m))
                                                             for m in OCR_MODES[2:] if dict.__contains__(self, m)]


def ocr(pdf_bytes: bytes, dpi: int = 300) -> list[str]:
    return [p["texts"][OCR_MODES[0]] for p in ocr_pages(pdf_bytes, dpi=dpi)]
