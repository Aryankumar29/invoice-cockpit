"""Connections made from the dashboard, plain-language issues with links, and sending the vendor email."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cockpit.api import create_app
from cockpit.issues import for_run
from cockpit.mail import send as mail_send
from cockpit.mail.connectors import GmailConnector, MailboxError
from cockpit.pipeline import Pipeline, Worker
from cockpit.settings import Settings
from cockpit.store import Store

from test_api import pdf_for, stub_extract


@pytest.fixture
def api(tmp_path, monkeypatch):
    for k in ("GOOGLE_SERVICE_ACCOUNT_FILE",):
        monkeypatch.delenv(k, raising=False)
    pipe = Pipeline(Store(tmp_path), extractor=stub_extract, explainer=lambda p: p["verdict"], stage_delay=0)
    c = TestClient(create_app(pipeline=pipe, start_worker=False))
    c.drain = Worker(pipe).drain
    c.tmp = tmp_path
    return c


def upload(api, key):
    rid = api.post("/api/runs", files={"file": (f"{key}.pdf", pdf_for(key), "application/pdf")}).json()["run_id"]
    api.drain()
    return api.get(f"/api/runs/{rid}").json()


class FakeSMTP:
    sent = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        if p != "app-pass":
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


# issues ------------------------------------------------------------------------
def test_price_variance_points_at_the_line(api):
    d = upload(api, "D1")
    i = d["issues"][0]
    assert i["reason"] == "PRICE_VARIANCE" and i["anchor"] == "line-2" and i["link"] == "See line 2"
    assert "Line 2 is $60.00 above the PO line" in i["text"]
    row = next(r for r in api.get("/api/runs").json() if r["id"] == d["id"])
    assert row["issues"][0]["anchor"] == "line-2"


def test_missing_field_says_what_is_missing(api):
    d = upload(api, "E2")
    i = next(x for x in d["issues"] if x["rule"] == "R02")
    assert i["text"] == "Missing on the invoice: invoice number." and i["anchor"] == "field-invoice_number"


def test_duplicate_links_to_the_original(api):
    d = upload(api, "E1")
    i = d["issues"][0]
    assert i["reason"] == "DUPLICATE_INVOICE" and i["severity"] == "reject"
    assert i["run"] == "run-0007" and i["link"] == "Open the original"


def test_clean_invoice_says_all_checks_passed(api):
    assert upload(api, "H1")["issues"] == [{"rule": None, "reason": None, "severity": "ok", "anchor": "rules",
                                            "text": "All 16 checks passed.", "link": "See the checks", "run": None}]


def test_failed_run_points_at_the_timeline():
    out = for_run({"status": "failed", "outcome": "failed", "reasons": ["PDF_ENCRYPTED"], "error": "PDF is password-protected."})
    assert out[0]["text"] == "PDF is password-protected." and out[0]["anchor"] == "timeline"


# settings ------------------------------------------------------------------------
def test_settings_save_hide_secrets_and_disconnect(tmp_path, monkeypatch):
    s = Settings(tmp_path)
    s.save("gmail", {"GMAIL_ADDRESS": "ap@demo-buyer.example", "GMAIL_APP_PASSWORD": "app-pass"})
    import os
    assert os.environ["GMAIL_APP_PASSWORD"] == "app-pass"
    rows = {r["key"]: r for r in s.describe()["gmail"]}
    assert rows["GMAIL_APP_PASSWORD"] == rows["GMAIL_APP_PASSWORD"] | {"set": True, "value": None, "from": "dashboard"}
    assert rows["GMAIL_ADDRESS"]["value"] == "ap@demo-buyer.example"
    s.save("gmail", {"GMAIL_ADDRESS": "ap2@demo-buyer.example", "GMAIL_APP_PASSWORD": ""})  # blank secret: keep it
    assert os.environ["GMAIL_APP_PASSWORD"] == "app-pass"
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    Settings(tmp_path)  # a restart re-applies what was saved
    assert os.environ["GMAIL_APP_PASSWORD"] == "app-pass"
    s.disconnect("gmail")
    assert "GMAIL_APP_PASSWORD" not in os.environ and json.loads((tmp_path / "settings.json").read_text()) == {}


def test_sheet_link_is_reduced_to_its_id(tmp_path):
    import os
    Settings(tmp_path).save("sheets", {"GOOGLE_SHEET_ID": "https://docs.google.com/spreadsheets/d/1AbC-xyz_9/edit#gid=0"})
    assert os.environ["GOOGLE_SHEET_ID"] == "1AbC-xyz_9"


def test_bad_service_account_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="not a valid Google JSON key"):
        Settings(tmp_path).save("sheets", {"GOOGLE_SHEET_ID": "abc", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"})


# connect from the dashboard ---------------------------------------------------------
def test_connect_gmail_runs_a_check(api, monkeypatch):
    monkeypatch.setattr(GmailConnector, "check", lambda self: f"Signed in to {self.address}; INBOX has 3 emails.")
    r = api.post("/api/connections/gmail", json={"values": {"GMAIL_ADDRESS": "ap@demo-buyer.example",
                                                           "GMAIL_APP_PASSWORD": "app-pass"}}).json()
    assert r["ok"] and "Signed in" in r["message"]
    c = r["connections"]
    assert c["send_via"] == "gmail" and next(m for m in c["mailboxes"] if m["name"] == "gmail")["configured"]
    assert "app-pass" not in json.dumps(c)


def test_connect_reports_a_failed_check_but_keeps_the_values(api, monkeypatch):
    def boom(self):
        raise MailboxError("Gmail refused the login.")
    monkeypatch.setattr(GmailConnector, "check", boom)
    r = api.post("/api/connections/gmail", json={"values": {"GMAIL_ADDRESS": "a@b.example",
                                                           "GMAIL_APP_PASSWORD": "x"}}).json()
    assert r["ok"] is False and "refused" in r["message"]


def test_connect_slack_posts_a_test_message(api, monkeypatch):
    seen = []
    real = httpx.Client

    def client(*a, **k):
        return real(transport=httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, text="ok")))

    monkeypatch.setattr("cockpit.notify.slack.httpx.Client", client)
    r = api.post("/api/connections/slack", json={"values": {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/X"}}).json()
    assert r["ok"] and r["message"] == "Test message posted to Slack." and len(seen) == 1
    assert api.delete("/api/connections/slack").json()["connections"]["slack"]["configured"] is False


def test_unknown_connection_is_404(api):
    assert api.post("/api/connections/yahoo", json={"values": {}}).status_code == 404


def test_spreadsheet_download(api, monkeypatch):
    from pathlib import Path
    book = Path(__file__).resolve().parents[1] / "fixtures" / "po_book.xlsx"
    monkeypatch.setenv("COCKPIT_PO_SOURCE", "file")
    monkeypatch.setenv("COCKPIT_PO_BOOK", str(book))
    r = api.get("/api/records/book")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert api.get("/api/connections").json()["po_source"]["open_url"] == "/api/records/book"


def test_google_sheet_open_link(api, monkeypatch):
    monkeypatch.setenv("COCKPIT_PO_SOURCE", "sheets")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "SHEET123")
    r = api.get("/api/records/book", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].endswith("/SHEET123/edit")


# send the vendor email --------------------------------------------------------------
def test_send_vendor_email_through_gmail(api, monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "ap@demo-buyer.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pass")
    monkeypatch.setattr("cockpit.mail.send.smtplib.SMTP", FakeSMTP)
    FakeSMTP.sent = []
    d = upload(api, "D1")
    draft = api.get(f"/api/runs/{d['id']}/vendor-draft").json()
    assert draft["send_via"] == "gmail" and draft["sent"] == []
    assert api.post(f"/api/runs/{d['id']}/vendor-email", json={"reviewer": ""}).status_code == 400
    r = api.post(f"/api/runs/{d['id']}/vendor-email",
                 json={"reviewer": "Aryan", "subject": draft["subject"], "body": draft["body"] + "\nThanks"}).json()
    assert r["sent"] and r["to"] == "billing@northwind-supply.example" and r["via"] == "gmail"
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "billing@northwind-supply.example" and msg["From"] == "ap@demo-buyer.example"
    assert "Thanks" in msg.get_content()
    assert r["run"]["outcome"] == "returned"
    assert r["run"]["actions"][-1]["action"] == "email_vendor" and r["run"]["actions"][-1]["reviewer"] == "Aryan"
    assert len(r["draft"]["sent"]) == 1 and r["draft"]["sent"][0]["status"] == "sent"


def test_send_without_a_mailbox_explains(api):
    d = upload(api, "D1")
    r = api.post(f"/api/runs/{d['id']}/vendor-email", json={"reviewer": "Aryan"})
    assert r.status_code == 502 and "Connect Gmail or Outlook" in r.json()["detail"]


def test_nothing_to_send_for_an_approved_invoice(api):
    d = upload(api, "H1")
    assert api.post(f"/api/runs/{d['id']}/vendor-email", json={"reviewer": "Aryan"}).status_code == 409


def test_send_through_outlook_graph(monkeypatch):
    for k, v in {"OUTLOOK_TENANT_ID": "t", "OUTLOOK_CLIENT_ID": "c", "OUTLOOK_CLIENT_SECRET": "s",
                 "OUTLOOK_MAILBOX": "ap@demo-buyer.example"}.items():
        monkeypatch.setenv(k, v)
    got = []

    def handler(req: httpx.Request):
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok"})
        got.append(json.loads(req.content))
        return httpx.Response(202)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        res = mail_send.send("billing@x.example", "Subj", "Body", client=c)
    assert res["via"] == "outlook"
    assert got[0]["message"]["toRecipients"][0]["emailAddress"]["address"] == "billing@x.example"


# PO book: upload, template, choose the source ------------------------------------------
def _book_bytes(tmp_path, mutate=None):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from make_po_book import build
    from cockpit.sources.files import write_xlsx
    t = build()
    if mutate:
        mutate(t)
    out = tmp_path / "upload.xlsx"
    write_xlsx(out, t)
    return out.read_bytes()


def test_upload_po_spreadsheet_switches_the_source(api, tmp_path):
    def cheaper(t):
        next(l for l in t["POLines"] if l["po_number"] == "PO-1051" and l["line_no"] == 2)["unit_price"] = "103.00"
    r = api.post("/api/records/upload", files={"file": ("my-pos.xlsx", _book_bytes(tmp_path, cheaper),
                                                        "application/octet-stream")}).json()
    assert r["ok"] and "11 purchase orders from my-pos.xlsx" in r["message"]
    opts = r["connections"]["po_source"]["options"]
    assert opts["in_use"] == "uploaded" and opts["uploaded"]["filename"] == "my-pos.xlsx"
    assert upload(api, "D1")["outcome"] == "auto_approved"  # the new price list is what the rules use
    back = api.post("/api/records/source", json={"kind": "sample"}).json()
    assert back["ok"] and back["connections"]["po_source"]["options"]["in_use"] == "sample"


def test_upload_rejects_a_wrong_file(api, tmp_path):
    r = api.post("/api/records/upload", files={"file": ("pos.csv", b"a,b", "text/csv")})
    assert r.status_code == 400 and ".xlsx" in r.json()["detail"]
    def no_currency(t):
        for row in t["PurchaseOrders"]:
            row.pop("currency")
    from cockpit.sources import book
    old = book.TABS["PurchaseOrders"]
    book.TABS["PurchaseOrders"] = [c for c in old if c != "currency"]
    try:
        data = _book_bytes(tmp_path, no_currency)
    finally:
        book.TABS["PurchaseOrders"] = old
    r = api.post("/api/records/upload", files={"file": ("pos.xlsx", data, "application/octet-stream")})
    assert r.status_code == 400 and "missing the column(s): currency" in r.json()["detail"]


def test_blank_template_downloads_and_loads(api, tmp_path):
    r = api.get("/api/records/template")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    (tmp_path / "t.xlsx").write_bytes(r.content)
    from cockpit.sources import book, files
    rec = book.parse(files.read_xlsx(tmp_path / "t.xlsx"))
    assert list(rec.pos) == ["PO-0001"] and rec.source["issues"] == []


def test_choose_sheets_without_one_is_refused(api):
    assert api.post("/api/records/source", json={"kind": "sheets"}).status_code == 409
    assert api.post("/api/records/source", json={"kind": "uploaded"}).status_code == 409


def test_sheet_view_shows_every_column_and_flags_bad_rows(api, tmp_path):
    def broken(t):
        t["POLines"].append({"po_number": "PO-1042", "line_no": "9", "description": "Bad", "qty_ordered": "ten",
                             "unit_price": "1"})
    api.post("/api/records/upload", files={"file": ("pos.xlsx", _book_bytes(tmp_path, broken), "application/octet-stream")})
    d = api.get("/api/records/sheet").json()
    tabs = {t["name"]: t for t in d["tabs"]}
    assert tabs["PurchaseOrders"]["columns"][-1] == "requester" and len(tabs["PurchaseOrders"]["rows"]) == 11
    po = tabs["PurchaseOrders"]; i = po["columns"].index("po_total")
    assert po["computed"] == ["po_total"] and po["rows"][0]["cells"][i] == "4,800.00"
    pl = tabs["POLines"]; j = pl["columns"].index("line_amount")
    assert pl["rows"][0]["cells"][j] == "2,000.00"
    vendor = tabs["Vendors"]["rows"][0]
    assert vendor["row"] == 2 and vendor["cells"][-1] == "****7781"  # bank account masked
    bad = [r for r in tabs["POLines"]["rows"] if r["problem"]]
    assert len(bad) == 1 and bad[0]["row"] == 20 and "'ten' is not a number" in bad[0]["problem"]
    api.post("/api/records/source", json={"kind": "sample"})


def test_policy_endpoint_serves_the_live_limits(api):
    from cockpit.core import policy as P
    d = api.get("/api/policy").json()
    assert d["version"] == P.POLICY_VERSION
    assert d["values"]["AUTO_APPROVE_CAP_CENTS"] == P.AUTO_APPROVE_CAP_CENTS and "needs_review" in d["consuming_outcomes"]
