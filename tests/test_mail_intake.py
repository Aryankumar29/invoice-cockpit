"""Mail intake: triage of the demo inbox, the matching keys, the connectors, and the full email -> decision path."""
import json
import sys
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cockpit.api import create_app
from cockpit.mail import connectors
from cockpit.mail.message import parse_eml
from cockpit.mail.triage import po_numbers, triage
from cockpit.pipeline import Pipeline, Worker
from cockpit.sources import seed_records
from cockpit.store import Store

ROOT = Path(__file__).resolve().parents[1]
MAILDIR = ROOT / "fixtures" / "mail"
sys.path.insert(0, str(ROOT / "fixtures"))
from generate_mail import EXPECTED  # noqa: E402

BY_KEY = {c["file"].split("/")[-1]: c for c in json.loads((ROOT / "fixtures" / "manifest.json").read_text())}


def msg(name):
    return parse_eml((MAILDIR / name).read_bytes())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_demo_inbox_triage(name):
    t = triage(msg(name), seed_records())
    assert t.label == EXPECTED[name][0], t.reasons
    if t.label == "invoice":
        assert len(t.invoice_docs) == EXPECTED[name][1]


def test_keys_confirm_vendor_po_and_buyer():
    k = triage(msg("01_northwind_invoice.eml"), seed_records()).keys
    assert (k["po_number"]["value"], k["vendor"]["value"], k["buyer"]["value"]) == ("PO-1042", "V-NW", "Demo Buyer Corp")
    assert k["matched"] == 3 and k["confirmed"]


def test_po_number_and_buyer_without_vendor_do_not_confirm():
    k = triage(msg("12_contoso_unknown_vendor.eml"), seed_records()).keys
    assert k["po_number"]["ok"] and k["buyer"]["ok"] and not k["vendor"]["ok"] and not k["confirmed"]


def test_lookalike_sender_is_suspicious_and_never_counts_as_vendor():
    t = triage(msg("08_lookalike_bank_change.eml"), seed_records())
    assert t.label == "suspicious" and t.signals["lookalike_of"] == "V-NW" and t.signals["bank_change"]
    assert not t.keys["vendor"]["ok"] and not t.keys["confirmed"]
    assert any("freemail" in r for r in t.reasons)


def test_po_on_another_vendor_fails_the_vendor_key():
    m = msg("01_northwind_invoice.eml")
    m.subject, m.text = "Invoice for PO-1047", "See PO-1047."
    m.attachments = []
    k = triage(m, seed_records()).keys
    assert k["po_number"]["value"] == "PO-1047" and not k["vendor"]["ok"] and "issued to V-GX" in k["vendor"]["note"]


@pytest.mark.parametrize("text,expected", [("PO-1042", ["PO-1042"]), ("P.O. #1051 and PO 1042", ["PO-1051", "PO-1042"]),
                                           ("PO Number: 1090", ["PO-1090"]), ("PO Box 4410", []), ("SPO-1042", [])])
def test_po_number_patterns(text, expected):
    assert po_numbers(text) == expected


def test_risky_attachment_is_suspicious():
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "billing@northwind-supply.example", "ap@demo-buyer.example", "Invoice"
    m.set_content("Invoice attached.")
    m.add_attachment(b"MZ...", maintype="application", subtype="octet-stream", filename="invoice.pdf.exe")
    assert triage(parse_eml(bytes(m)), seed_records()).label == "suspicious"


def test_inline_logo_is_not_a_document():
    m = EmailMessage()
    m["From"], m["Subject"] = "maya@demo-buyer.example", "Hello"
    m.set_content("Hi")
    m.add_attachment(b"\x89PNG" + b"0" * 500, maintype="image", subtype="png", filename="logo.png",
                     disposition="inline", cid="<logo>")
    assert triage(parse_eml(bytes(m)), seed_records()).label == "not_invoice"


# connectors ------------------------------------------------------------------
class FakeIMAP:
    def __init__(self, raws):
        self.raws, self.calls = raws, []

    def login(self, u, p):
        self.calls.append(("login", u))

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"2"]

    def search(self, charset, criteria):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raws)))]

    def fetch(self, i, what):
        assert "PEEK" in what  # never marks mail as read
        return "OK", [(b"1 (BODY[] {n}", self.raws[int(i) - 1]), b")"]

    def logout(self):
        pass


def test_gmail_connector_reads_without_marking(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "ap@demo-buyer.example")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pass")
    raws = [(MAILDIR / n).read_bytes() for n in ("01_northwind_invoice.eml", "10_internal_lunch.eml")]
    box = FakeIMAP(raws)
    out = connectors.GmailConnector(imap_factory=lambda: box).fetch()
    assert [m.subject for m in out] == ["Invoice INV-H-001 for PO-1042", "Team lunch on Friday?"]
    assert ("select", '"INBOX"', True) in box.calls and all(m.connector == "gmail" for m in out)


def test_gmail_not_configured_explains_what_to_set():
    with pytest.raises(connectors.MailboxError, match="GMAIL_APP_PASSWORD"):
        connectors.GmailConnector().fetch()


def test_outlook_connector_uses_graph(monkeypatch):
    for k, v in {"OUTLOOK_TENANT_ID": "t", "OUTLOOK_CLIENT_ID": "c", "OUTLOOK_CLIENT_SECRET": "s",
                 "OUTLOOK_MAILBOX": "ap@demo-buyer.example"}.items():
        monkeypatch.setenv(k, v)
    raw = (MAILDIR / "02_globex_two_invoices.eml").read_bytes()

    def handler(req: httpx.Request):
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok"})
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path.endswith("/$value"):
            return httpx.Response(200, content=raw)
        return httpx.Response(200, json={"value": [{"id": "AAA"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        out = connectors.OutlookConnector(client=c).fetch()
    assert len(out) == 1 and len(out[0].attachments) == 2 and out[0].connector == "outlook"


# email -> pipeline -> decision, through the API ----------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_REPLAY_DIR", str(tmp_path / "replay"))
    monkeypatch.setenv("COCKPIT_MAILBOX_DIR", str(MAILDIR))
    pipe = Pipeline(Store(tmp_path), stage_delay=0)
    c = TestClient(create_app(pipeline=pipe, start_worker=False))
    c.drain = Worker(pipe).drain
    return c


def outcome_for(client, mail_row, filename):
    rid = next(a["run_id"] for a in mail_row["attachments"] if a["filename"] == filename)
    return client.get(f"/api/runs/{rid}").json()


def test_sync_demo_mailbox_end_to_end(client):
    r = client.post("/api/mail/sync", json={"connector": "folder"}).json()
    assert r["fetched"] == r["new"] == len(EXPECTED)
    assert r["by_label"] == {"invoice": 7, "not_invoice": 4, "suspicious": 1, "unsure": 1}
    assert r["runs_started"] == 8
    client.drain()
    mails = {m["subject"]: m for m in client.get("/api/mail").json()["mails"]}
    d = outcome_for(client, mails["Invoice INV-H-001 for PO-1042"], "INV-H-001.pdf")
    assert d["outcome"] == "auto_approved" and d["mail"]["keys"]["confirmed"]
    d = outcome_for(client, mails["Invoice INV-D-001 / PO-1051"], "INV-D-001.pdf")
    assert (d["outcome"], d["reasons"][0]) == ("needs_review", "PRICE_VARIANCE")
    draft = client.get(f"/api/runs/{d['id']}/vendor-draft").json()
    assert draft["available"] and draft["to"] == "billing@northwind-supply.example"
    assert "agreed PO price" in draft["body"] and draft["mailto"].startswith("mailto:billing%40northwind")
    d = outcome_for(client, mails["Re: Invoice NW-2026-001 (PO-1038)"], "NW-2026-001.pdf")
    assert (d["outcome"], d["reasons"][0]) == ("rejected", "DUPLICATE_INVOICE")
    for subj, fname in (("Invoices INV-S-001 and INV-S-002 (PO-1047)", "INV-S-001.pdf"),
                        ("Invoice IN-5610 - office chairs", "IN-5610.pdf")):
        assert outcome_for(client, mails[subj], fname)["outcome"] == "auto_approved"
    # held and archived emails started nothing
    for m in mails.values():
        if m["label"] != "invoice":
            assert m["run_ids"] == [] and m["status"] in ("held", "archived")
    # Slack previews: one alert per held email and per invoice that needs a person
    kinds = [n["kind"] for n in client.get("/api/notifications").json()["items"]]
    assert kinds.count("mail_suspicious") == 1 and kinds.count("mail_unsure") == 1
    assert "run_needs_review" in kinds and "run_rejected" in kinds
    # a second sync is a no-op: Message-IDs are remembered
    again = client.post("/api/mail/sync", json={"connector": "folder"}).json()
    assert again["new"] == 0 and again["runs_started"] == 0


def test_release_held_email_runs_the_rules(client):
    row = client.post("/api/mail/upload", files={"file": ("m.eml", (MAILDIR / "08_lookalike_bank_change.eml")
                                                          .read_bytes(), "message/rfc822")}).json()
    assert row["status"] == "held" and row["run_ids"] == []
    assert client.post(f"/api/mail/{row['id']}/release", json={"reviewer": "", "note": ""}).status_code == 400
    rel = client.post(f"/api/mail/{row['id']}/release", json={"reviewer": "Aryan", "note": "Checking the PDF"}).json()
    assert rel["status"] == "released" and len(rel["run_ids"]) == 1
    client.drain()
    d = client.get(f"/api/runs/{rel['run_ids'][0]}").json()
    assert d["outcome"] == "needs_review" and "BANK_CHANGE" in d["reasons"]
    draft = client.get(f"/api/runs/{d['id']}/vendor-draft").json()
    assert any("verify by phone" in n for n in draft["internal_notes"])
    assert client.post(f"/api/mail/{row['id']}/dismiss", json={"reviewer": "A", "note": "n"}).status_code == 409


def test_upload_rejects_non_eml(client):
    assert client.post("/api/mail/upload", files={"file": ("x.pdf", b"%PDF", "application/pdf")}).status_code == 400


def test_dismiss_unsure_email(client):
    row = client.post("/api/mail/upload", files={"file": ("m.eml", (MAILDIR / "09_acme_link_only.eml").read_bytes(),
                                                          "message/rfc822")}).json()
    assert row["label"] == "unsure"
    out = client.post(f"/api/mail/{row['id']}/dismiss", json={"reviewer": "Aryan", "note": "Asked for a PDF"}).json()
    assert out["status"] == "dismissed" and "Asked for a PDF" in out["note"]


def test_unknown_mailbox_is_400(client):
    assert client.post("/api/mail/sync", json={"connector": "yahoo"}).status_code == 400
    assert "GMAIL" in client.post("/api/mail/sync", json={"connector": "gmail"}).json()["detail"]
