"""Number and date parsing shared by every extraction route. Values are read, never corrected."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# A printed money amount: two decimals (US or EU separators), or a currency symbol with whole units.
MONEY = (r"(?:[$€£]\s?)?-?(?<![\d.,])(?:\d{1,3}(?:[,.]\d{3})+|\d+)[.,]\d{2}(?![\d])"
         r"|[$€£]\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?![\d.,]*\d)")
MONEY_RE = re.compile(MONEY)
DATE = (r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}"
        r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}")
DATE_RE = re.compile(DATE)
_NUM_TOKEN = re.compile(r"(?<![\d])\d+(?:[.,]\d+)*|(?<![\d.,])[.,]\d{2}(?!\d)")  # also "$.50"
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


def parse_number(s: str | None, money: bool = True) -> Decimal | None:
    """'4,800.00', '4800', '$4,800', '1.234,56', '(12.50)' -> Decimal. None when not a number."""
    if s is None:
        return None
    s = str(s).strip()
    neg = s.startswith("(") and s.endswith(")") or "-" in s[:3]
    t = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", t):
        return None
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in t:
        parts = t.split(",")
        t = t.replace(",", ".") if len(parts) == 2 and len(parts[1]) != 3 else t.replace(",", "")
    elif t.count(".") > 1 or (money and re.fullmatch(r"\d{1,3}\.\d{3}", t)):
        t = t.replace(".", "")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    return -d if neg else d


_MAX_DIGITS = 15  # longer digit runs are barcodes, account or reference numbers, never invoice amounts


def _scaled(d: Decimal | None, factor: int) -> int | None:
    if d is None or len(d.as_tuple().digits) > _MAX_DIGITS:
        return None
    try:
        return int((d * factor).quantize(Decimal(1), ROUND_HALF_UP))
    except InvalidOperation:
        return None


def to_cents(s) -> int | None:
    return _scaled(parse_number(s) if not isinstance(s, (int, float)) else Decimal(str(s)), 100)


def to_milli(s) -> int | None:
    return _scaled(parse_number(s, money=False) if not isinstance(s, (int, float)) else Decimal(str(s)), 1000)


def text_amounts(text: str) -> set[int]:
    """Every value (in cents) that a numeric token on the page could denote, under US and EU reading."""
    out: set[int] = set()
    text = text or ""
    # Some PDFs space right-aligned digits apart ("$ 4 62.00"); also read such runs joined. Grounding only:
    # a parser must not join "10 100.00" (qty 10 at 100.00), but an extra candidate value here is harmless.
    joined = _SPLIT_AMOUNT.sub("", text)
    for tok in _NUM_TOKEN.findall(text) + (_NUM_TOKEN.findall(joined) if joined != text else []):
        if sum(c.isdigit() for c in tok) > _MAX_DIGITS:
            continue
        for d in (parse_number(tok), parse_number(tok, money=False),
                  _strict(tok, ",", "."), _strict(tok, ".", ",")):
            v = _scaled(d, 100)
            if v is not None:
                out.add(v)
    return out


_SPLIT_AMOUNT = re.compile(r"(?<=\d) (?=\d[\d,]*[.,]\d{2}(?!\d))")


def _strict(tok: str, thou: str, dec: str) -> Decimal | None:
    try:
        return Decimal(tok.replace(thou, "").replace(dec, "."))
    except InvalidOperation:
        return None


_FORMATS = ["%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%m/%d/%y",
            "%d/%m/%Y", "%d.%m.%Y", "%d.%m.%y"]


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    if isinstance(s, date):
        return s
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(s).strip())
    t = " ".join(re.sub(r"(?<=[A-Za-z])\.", "", t.replace(",", " ")).split())
    if "/" in t:
        a = t.split("/")
        fmts = ["%d/%m/%Y", "%d/%m/%y"] if int(a[0]) > 12 else ["%m/%d/%Y", "%m/%d/%y"]
    else:
        fmts = _FORMATS
    for f in fmts:
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            continue
    return None


ISO_CURRENCIES = frozenset(
    "USD EUR GBP CAD AUD NZD INR JPY CNY CHF SEK NOK DKK PLN CZK HUF SGD HKD KRW MXN BRL ZAR AED SAR ILS TRY "
    "THB MYR IDR PHP VND TWD CLP COP PEN ARS EGP NGN KES PKR BDT RON BGN".split())


_STATED = [  # a currency stated in words or by a prefixed symbol beats the bare "$" (Canada Post: "$" + "Canadian currency")
    ("USD", r"\bUS\$|\bU\.?S\.? dollars?\b"),
    ("CAD", r"\bC\$|\bCA\$|\bCanadian (?:dollars?|currency|funds)\b"),
    ("AUD", r"\bA\$|\bAU\$|\bAustralian (?:dollars?|currency)\b"),
    ("NZD", r"\bNZ\$|\bNew Zealand dollars?\b"),
    ("SGD", r"\bS\$|\bSingapore dollars?\b"),
    ("HKD", r"\bHK\$|\bHong Kong dollars?\b"),
    ("MXN", r"\bMX\$|\bMexican pesos?\b"),
    ("EUR", r"\beuros?\b"),
    ("GBP", r"\bpounds? sterling\b"),
    ("INR", r"₹|\bIndian rupees?\b"),
]


def stated_currency(text: str) -> str | None:
    hits = {iso for iso, pat in _STATED if re.search(pat, text or "", re.I)}
    return hits.pop() if len(hits) == 1 else None  # two different statements: let the code/symbol decide


def currency_of(code: str | None, text: str) -> str | None:
    """ISO code if given; else a currency stated in words; else a printed symbol; else None.

    "$" alone is ambiguous (USD, CAD, AUD, ...). A bare-"$" USD reading is replaced when the document states a
    different dollar currency in words and never prints USD itself."""
    stated = stated_currency(text)
    if code and code.strip().upper() in ISO_CURRENCIES:  # "uso" (OCR for USD) is not a currency: fall through
        c = code.strip().upper()
        if c == "USD" and stated and stated != "USD" and not re.search(r"\bUSD\b", text or ""):
            return stated
        return c
    m = re.search(r"\b(USD|EUR|GBP|CAD|AUD|INR|JPY|CHF)\b", text or "")
    if m:
        return m.group(1)
    if stated:
        return stated
    for sym, iso in _SYMBOLS.items():
        if (code and sym in code) or sym in (text or ""):
            return iso
    return None
