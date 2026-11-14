"""Source of records: where vendors and purchase orders come from (COCKPIT_PO_SOURCE).
  seed    the built-in demo data (cockpit/core/seed.py)
  file    an .xlsx workbook or a folder of CSVs (COCKPIT_PO_BOOK, default fixtures/po_book.xlsx)
  sheets  a Google Sheet (GOOGLE_SHEET_ID, optional GOOGLE_SERVICE_ACCOUNT_FILE)
  auto    sheets if GOOGLE_SHEET_ID is set, else file if the book exists, else seed (the default)
The book is re-read when the file changes, or every COCKPIT_SHEETS_TTL seconds for Sheets. If a reload fails,
the last good copy stays in use and the error is shown; with no good copy, runs fail instead of guessing."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..core import seed
from .book import BookError, Records, parse, to_tables

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOOK = ROOT / "fixtures" / "po_book.xlsx"
SEED_DOMAINS = {"V-NW": "northwind-supply.example", "V-GX": "globex-services.example",
                "V-AC": "acme-industrial.example", "V-IN": "initech-office.example"}
SEED_EMAILS = {"V-NW": "billing@northwind-supply.example", "V-GX": "ar@globex-services.example",
               "V-AC": "invoices@acme-industrial.example", "V-IN": "billing@initech-office.example"}
BUYER = "Demo Buyer Corp"

_lock = threading.Lock()
_cache: dict = {}

__all__ = ["BookError", "Records", "configured", "records", "reload", "seed_records", "to_tables"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed_records() -> Records:
    contacts = {vid: {"email": SEED_EMAILS[vid], "domain": SEED_DOMAINS[vid]} for vid in SEED_DOMAINS}
    pos = seed.purchase_orders()
    rec = Records(seed.vendors(), pos, contacts, {no: BUYER for no in pos},
                  {"kind": "seed", "location": "cockpit/core/seed.py", "issues": []})
    rec.tables = {t: [{k: str(v) for k, v in r.items()} for r in rows]
                  for t, rows in to_tables(rec, seed.BANK_ACCOUNTS).items()}
    return rec


def configured() -> tuple[str, str]:
    kind = (os.environ.get("COCKPIT_PO_SOURCE") or "auto").strip().lower()
    book = os.environ.get("COCKPIT_PO_BOOK") or str(DEFAULT_BOOK)
    sheet = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if kind == "auto":
        kind = "sheets" if sheet else "file" if Path(book).exists() else "seed"
    if kind not in ("seed", "file", "sheets"):
        raise BookError(f"COCKPIT_PO_SOURCE={kind} is not one of seed, file, sheets.")
    return kind, {"seed": "cockpit/core/seed.py", "file": book, "sheets": sheet}[kind]


def _version(kind: str, loc: str):
    if kind == "file":
        p = Path(loc)
        if p.is_dir():
            return tuple(sorted((f.name, f.stat().st_mtime_ns) for f in p.glob("*.csv")))
        return p.stat().st_mtime_ns if p.exists() else None
    if kind == "sheets":
        return int(time.time() // max(5, int(os.environ.get("COCKPIT_SHEETS_TTL", "60"))))
    return 0


def _load(kind: str, loc: str) -> Records:
    if kind == "seed":
        return seed_records()
    if kind == "file":
        from .files import read_book
        p = Path(loc)
        if not p.exists():
            raise BookError(f"PO book not found: {loc}")
        rec = parse(read_book(p))
    else:
        from .sheets import read_sheet
        if not loc:
            raise BookError("GOOGLE_SHEET_ID is not set.")
        rec = parse(read_sheet(loc, os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or None))
    rec.source |= {"kind": kind, "location": loc}
    return rec


def records(force: bool = False) -> Records:
    kind, loc = configured()
    with _lock:
        key = (kind, loc)
        ver = _version(kind, loc)
        hit = _cache.get(key)
        if hit and not force and hit["version"] == ver:
            return hit["records"]
        try:
            rec = _load(kind, loc)
        except Exception as e:
            if hit:  # keep serving the last good copy; surface the failure
                hit["records"].source["error"] = f"Reload failed at {_now()}: {e}"
                hit["version"] = ver
                return hit["records"]
            raise BookError(f"PO source {kind} unavailable: {e}") from e
        rec.source |= {"loaded_at": _now(), "error": None,
                       "counts": {"vendors": len(rec.vendors), "purchase_orders": len(rec.pos),
                                  "po_lines": sum(len(p.lines) for p in rec.pos.values())}}
        _cache[key] = {"version": ver, "records": rec}
        return rec


def reload() -> Records:
    return records(force=True)
