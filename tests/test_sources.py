"""PO book connector: the spreadsheet, CSV folder and Google Sheets readers all load the same master data."""
import csv
import dataclasses
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from cockpit import sources
from cockpit.core.seed import BANK_ACCOUNTS
from cockpit.sources import book, files, seed_records, sheets, to_tables

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _same(a, b):
    assert [dataclasses.asdict(v) for v in a.vendors] == [dataclasses.asdict(v) for v in b.vendors]
    assert {k: dataclasses.asdict(v) for k, v in a.pos.items()} == {k: dataclasses.asdict(v) for k, v in b.pos.items()}
    assert a.contacts == b.contacts and a.buyers == b.buyers


@pytest.fixture
def tables():
    from make_po_book import build
    return build()


def test_xlsx_round_trip_equals_seed(tmp_path, tables):
    files.write_xlsx(tmp_path / "book.xlsx", tables)
    rec = book.parse(files.read_xlsx(tmp_path / "book.xlsx"))
    _same(rec, seed_records())
    assert rec.source["issues"] == []


def test_csv_round_trip_equals_seed(tmp_path, tables):
    files.write_csv_dir(tmp_path / "csv", tables)
    _same(book.parse(files.read_csv_dir(tmp_path / "csv")), seed_records())


def test_shipped_sample_book_matches_seed():
    """fixtures/po_book.xlsx is what the demo loads; regenerate it with scripts/make_po_book.py."""
    _same(book.parse(files.read_xlsx(ROOT / "fixtures" / "po_book.xlsx")), seed_records())


def test_bad_rows_are_reported_not_guessed(tables):
    t = json.loads(json.dumps(tables))
    t["Vendors"].append({"vendor_id": "", "vendor_name": "No ID Ltd", "tax_id": "1"})
    t["PurchaseOrders"].append({"po_number": "PO-9999", "vendor_id": "V-XX", "currency": "USD"})
    t["PurchaseOrders"][0]["vendor_name"] = "Someone Else Inc."
    t["POLines"].append({"po_number": "PO-1042", "line_no": "9", "description": "Bad", "qty_ordered": "ten",
                         "unit_price": "1"})
    t["POLines"].append({"po_number": "PO-0000", "line_no": "1", "description": "Orphan", "qty_ordered": "1",
                         "unit_price": "1"})
    rec = book.parse(t)
    text = " | ".join(rec.source["issues"])
    assert "missing vendor_id" in text and "V-XX, which is not on the Vendors tab" in text
    assert "'ten' is not a number" in text and "PO-0000 is not on the PurchaseOrders tab" in text
    assert "the Vendors tab wins" in text
    assert "PO-9999" not in rec.pos and len(rec.pos["PO-1042"].lines) == 3


def test_money_quantity_tax_and_text_formats(tables):
    t = json.loads(json.dumps(tables))
    po = next(r for r in t["PurchaseOrders"] if r["po_number"] == "PO-1060")
    po["tax_rate_pct"] = "8.25%"
    line = next(r for r in t["POLines"] if r["po_number"] == "PO-1060")
    line["unit_price"], line["qty_ordered"] = "$1,040.005", "2.5"
    rec = book.parse(t)
    l = rec.pos["PO-1060"].lines[0]
    assert rec.pos["PO-1060"].tax_rate_bp == 825 and l.unit_price_cents == 104_001 and l.qty_milli == 2_500


def test_auto_source_prefers_sheet_then_file_then_seed(monkeypatch, tmp_path):
    monkeypatch.setenv("COCKPIT_PO_SOURCE", "auto")
    monkeypatch.setenv("COCKPIT_PO_BOOK", str(tmp_path / "missing.xlsx"))
    assert sources.configured()[0] == "seed"
    monkeypatch.setenv("COCKPIT_PO_BOOK", str(ROOT / "fixtures" / "po_book.xlsx"))
    assert sources.configured()[0] == "file"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "abc")
    assert sources.configured() == ("sheets", "abc")


def test_file_source_reloads_on_change_and_keeps_last_good(monkeypatch, tmp_path, tables):
    path = tmp_path / "book.xlsx"
    files.write_xlsx(path, tables)
    monkeypatch.setenv("COCKPIT_PO_SOURCE", "file")
    monkeypatch.setenv("COCKPIT_PO_BOOK", str(path))
    assert sources.records(force=True).pos["PO-1042"].lines[0].received_qty_milli == 20_000
    t = json.loads(json.dumps(tables))
    t["POLines"][0]["qty_received"] = "5"
    import os
    import time
    files.write_xlsx(path, t)
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    assert sources.records().pos["PO-1042"].lines[0].received_qty_milli == 5_000
    path.write_bytes(b"not a workbook")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 20_000_000))
    rec = sources.records()
    assert rec.pos["PO-1042"].lines[0].received_qty_milli == 5_000 and "Reload failed" in rec.source["error"]


def test_no_good_copy_fails_instead_of_guessing(monkeypatch, tmp_path):
    monkeypatch.setenv("COCKPIT_PO_SOURCE", "file")
    monkeypatch.setenv("COCKPIT_PO_BOOK", str(tmp_path / "nope.xlsx"))
    with pytest.raises(sources.BookError, match="unavailable"):
        sources.records(force=True)


def _csv(rows: list[dict], cols: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def test_google_sheet_shared_by_link(tables):
    seen = []

    def handler(req: httpx.Request):
        tab = req.url.params["sheet"]
        seen.append(tab)
        return httpx.Response(200, text=_csv(tables[tab], book.TABS[tab]), headers={"content-type": "text/csv"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        _same(book.parse(sheets.read_sheet("SHEET123", client=c)), seed_records())
    assert seen == list(book.TABS)


def test_google_sheet_not_shared_gives_clear_error():
    def handler(req):
        return httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c, pytest.raises(book.BookError, match="anyone with"):
        sheets.read_sheet("SHEET123", client=c)


def test_google_sheet_with_service_account(tmp_path, tables):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    kf = tmp_path / "sa.json"
    kf.write_text(json.dumps({"client_email": "cockpit@proj.iam.gserviceaccount.com", "private_key": pem,
                              "token_uri": "https://oauth2.googleapis.com/token"}))

    def handler(req: httpx.Request):
        if req.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in req.content
            return httpx.Response(200, json={"access_token": "tok"})
        assert req.headers["authorization"] == "Bearer tok"
        vr = [{"values": [book.TABS[t]] + [[str(r.get(c, "")) for c in book.TABS[t]] for r in tables[t]]}
              for t in book.TABS]
        return httpx.Response(200, json={"valueRanges": vr})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        _same(book.parse(sheets.read_sheet("SHEET123", str(kf), client=c)), seed_records())


def test_to_tables_keeps_bank_accounts_as_text():
    t = to_tables(seed_records(), BANK_ACCOUNTS)
    assert t["Vendors"][0]["bank_account"] == "0000007781"
