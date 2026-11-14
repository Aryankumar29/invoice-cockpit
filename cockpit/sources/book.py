"""The PO book: the spreadsheet AP keeps of vendors, purchase orders and goods received.
Three tabs (Vendors, PurchaseOrders, POLines), one header row each. Every reader (xlsx, CSV folder, Google Sheets)
produces the same {tab: [row dicts]} and parse() turns it into master data. A bad row is skipped and reported,
never guessed."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..core.domain import POLine, PurchaseOrder, Vendor
from ..core.normalize import bank_fingerprint

TABS = {
    "Vendors": ["vendor_id", "vendor_name", "tax_id", "status", "billing_email", "email_domain", "bank_account"],
    "PurchaseOrders": ["po_number", "vendor_id", "vendor_name", "buyer_company", "currency", "status", "issued_on",
                       "tax_rate_pct", "requester"],
    "POLines": ["po_number", "line_no", "sku", "description", "qty_ordered", "unit_price", "qty_received"],
}
REQUIRED = {
    "Vendors": ["vendor_id", "vendor_name", "tax_id"],
    "PurchaseOrders": ["po_number", "vendor_id", "currency"],
    "POLines": ["po_number", "line_no", "description", "qty_ordered", "unit_price"],
}


@dataclass
class Records:
    """Master data the pipeline and the mail triage read. contacts and buyers are the matching keys."""
    vendors: list[Vendor]
    pos: dict[str, PurchaseOrder]
    contacts: dict[str, dict] = field(default_factory=dict)  # vendor_id -> {email, domain}
    buyers: dict[str, str] = field(default_factory=dict)  # po_number -> buyer company on the PO
    source: dict = field(default_factory=dict)  # kind, location, loaded_at, counts, issues
    tables: dict[str, list[dict]] = field(default_factory=dict)  # the rows as read, for the sheet view

    def vendor(self, vid: str | None) -> Vendor | None:
        return next((v for v in self.vendors if v.id == vid), None)


class BookError(Exception):
    pass


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _dec(v, what: str) -> Decimal:
    s = _s(v).replace(",", "").replace("$", "").replace("%", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{what} '{_s(v)}' is not a number")


def _cents(v, what: str) -> int:
    d = _dec(v, what)
    if d < 0:
        raise ValueError(f"{what} is negative")
    return int((d * 100).quantize(Decimal(1), ROUND_HALF_UP))


def _milli(v, what: str) -> int:
    d = _dec(v, what)
    if d < 0:
        raise ValueError(f"{what} is negative")
    return int((d * 1000).quantize(Decimal(1), ROUND_HALF_UP))


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _s(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError(f"date '{s}' is not YYYY-MM-DD")


def _cell(v) -> str:
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return _s(v)


def parse(tables: dict[str, list[dict]]) -> Records:
    issues: list[str] = []
    for tab in TABS:
        if tab not in tables:
            raise BookError(f"The PO book has no '{tab}' tab.")

    def rows(tab):
        for i, raw in enumerate(tables[tab], start=2):  # row 1 is the header
            row = {k.strip().lower(): v for k, v in raw.items() if k}
            if not any(_s(v) for v in row.values()):
                continue
            missing = [c for c in REQUIRED[tab] if not _s(row.get(c))]
            if missing:
                issues.append(f"{tab} row {i}: missing {', '.join(missing)}; row skipped.")
                continue
            yield i, row

    vendors, contacts = [], {}
    for i, r in rows("Vendors"):
        vid = _s(r["vendor_id"])
        if any(v.id == vid for v in vendors):
            issues.append(f"Vendors row {i}: vendor_id {vid} appears twice; row skipped.")
            continue
        bank = _s(r.get("bank_account"))
        vendors.append(Vendor(vid, _s(r["vendor_name"]), _s(r["tax_id"]), _s(r.get("status")).lower() or "active",
                              bank_hash=bank_fingerprint(bank) if bank else None))
        email = _s(r.get("billing_email")).lower()
        domain = _s(r.get("email_domain")).lower().lstrip("@") or (email.split("@", 1)[1] if "@" in email else "")
        contacts[vid] = {"email": email or None, "domain": domain or None}
    by_id = {v.id: v for v in vendors}

    pos, buyers = {}, {}
    for i, r in rows("PurchaseOrders"):
        no, vid = _s(r["po_number"]), _s(r["vendor_id"])
        if no in pos:
            issues.append(f"PurchaseOrders row {i}: {no} appears twice; row skipped.")
            continue
        if vid not in by_id:
            issues.append(f"PurchaseOrders row {i}: {no} names vendor {vid}, which is not on the Vendors tab; "
                          f"PO skipped.")
            continue
        name = _s(r.get("vendor_name"))
        if name and name.casefold() != by_id[vid].name.casefold():
            issues.append(f"PurchaseOrders row {i}: {no} says vendor '{name}' but {vid} is "
                          f"'{by_id[vid].name}'; the Vendors tab wins.")
        try:
            rate = _dec(r.get("tax_rate_pct") or 0, "tax_rate_pct")
            pos[no] = PurchaseOrder(no, vid, _s(r["currency"]).upper(), [],
                                    status=_s(r.get("status")).lower() or "open",
                                    tax_rate_bp=int((rate * 100).quantize(Decimal(1), ROUND_HALF_UP)),
                                    issued=_date(r.get("issued_on")))
        except ValueError as e:
            issues.append(f"PurchaseOrders row {i}: {no}: {e}; PO skipped.")
            continue
        if _s(r.get("buyer_company")):
            buyers[no] = _s(r["buyer_company"])

    for i, r in rows("POLines"):
        no = _s(r["po_number"])
        if no not in pos:
            issues.append(f"POLines row {i}: {no} is not on the PurchaseOrders tab; line skipped.")
            continue
        try:
            line_no = int(_dec(r["line_no"], "line_no"))
            qty = _milli(r["qty_ordered"], "qty_ordered")
            received = _milli(r["qty_received"], "qty_received") if _s(r.get("qty_received")) else 0
            price = _cents(r["unit_price"], "unit_price")
        except ValueError as e:
            issues.append(f"POLines row {i}: {no}: {e}; line skipped.")
            continue
        if any(l.line_no == line_no for l in pos[no].lines):
            issues.append(f"POLines row {i}: {no} line {line_no} appears twice; line skipped.")
            continue
        if received > qty:
            issues.append(f"POLines row {i}: {no} line {line_no} received more than ordered; kept as entered.")
        pos[no].lines.append(POLine(line_no, _s(r["description"]), qty, price, received, _s(r.get("sku")) or None))

    for no, po in list(pos.items()):
        po.lines.sort(key=lambda l: l.line_no)
        if not po.lines:
            issues.append(f"{no} has no lines on the POLines tab; PO skipped.")
            del pos[no]
            buyers.pop(no, None)
    return Records(vendors, pos, contacts, buyers, {"issues": issues},
                   {tab: [{k.strip().lower(): _cell(v) for k, v in r.items() if k} for r in tables[tab]] for tab in TABS})


def to_tables(rec: Records, bank_accounts: dict[str, str] | None = None) -> dict[str, list[dict]]:
    """The inverse of parse(), used to write the sample book from the demo master data."""
    bank_accounts = bank_accounts or {}
    vendors = [{"vendor_id": v.id, "vendor_name": v.name, "tax_id": v.tax_id, "status": v.status,
                "billing_email": (rec.contacts.get(v.id) or {}).get("email") or "",
                "email_domain": (rec.contacts.get(v.id) or {}).get("domain") or "",
                "bank_account": bank_accounts.get(v.id, "")} for v in rec.vendors]
    names = {v.id: v.name for v in rec.vendors}
    pos, lines = [], []
    for po in rec.pos.values():
        pos.append({"po_number": po.po_number, "vendor_id": po.vendor_id, "vendor_name": names.get(po.vendor_id, ""),
                    "buyer_company": rec.buyers.get(po.po_number, ""), "currency": po.currency,
                    "status": po.status, "issued_on": po.issued.isoformat() if po.issued else "",
                    "tax_rate_pct": _num(Decimal(po.tax_rate_bp) / 100), "requester": ""})
        for l in po.lines:
            lines.append({"po_number": po.po_number, "line_no": l.line_no, "sku": l.sku or "",
                          "description": l.description, "qty_ordered": _num(Decimal(l.qty_milli) / 1000),
                          "unit_price": f"{Decimal(l.unit_price_cents) / 100:.2f}",
                          "qty_received": _num(Decimal(l.received_qty_milli) / 1000)})
    return {"Vendors": vendors, "PurchaseOrders": pos, "POLines": lines}


def _num(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s
