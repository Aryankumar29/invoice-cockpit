"""Comparison keys. Originals are never overwritten; these are derived values."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "co", "corp", "corporation",
    "company", "gmbh", "plc", "llp", "lp", "sa", "ag", "pvt",
}
_OCR_MAP = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"})


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(t for t in s.split() if t not in _LEGAL_SUFFIXES)


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def alnum_upper(s: str | None) -> str | None:
    if not s:
        return None
    return re.sub(r"[^0-9A-Za-z]", "", s).upper() or None


norm_tax_id = alnum_upper
norm_po_ref = alnum_upper


def ocr_key(s: str | None) -> str | None:
    """PO reference key tolerant of O/0, I/l/1 style OCR confusions."""
    k = alnum_upper(s)
    return k.translate(_OCR_MAP) if k else None


def bank_fingerprint(account: str | None) -> str | None:
    """Stable key for a remittance account: digits only, so '0000-0077 81' == '0000007781'."""
    digits = re.sub(r"\D", "", account or "")
    return f"acct-{hashlib.sha256(digits.encode()).hexdigest()[:16]}" if digits else None


def invoice_key_loose(n: str | None) -> str | None:
    """Punctuation-insensitive, leading zeros preserved: INV-0042 -> INV0042."""
    return alnum_upper(n)


def invoice_key_numeric(n: str | None) -> str | None:
    """Digits only, leading zeros dropped: INV-0042, INV/42 and 42 all -> 42."""
    if not n:
        return None
    digits = "".join(re.findall(r"\d", n))
    return (digits.lstrip("0") or "0") if digits else None
