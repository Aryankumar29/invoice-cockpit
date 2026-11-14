"""Slack alerts, the daily digest and its schedule, and vendor email drafts."""
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from cockpit.api import create_app
from cockpit.notify import DigestScheduler, Notifier
from cockpit.notify.slack import Slack
from cockpit.notify.vendor_email import draft
from cockpit.pipeline import Pipeline, Worker
from cockpit.store import Store

from test_api import pdf_for, stub_extract


@pytest.fixture
def api(tmp_path):
    pipe = Pipeline(Store(tmp_path), extractor=stub_extract, explainer=lambda p: p["verdict"], stage_delay=0)
    c = TestClient(create_app(pipeline=pipe, start_worker=False))
    c.drain = Worker(pipe).drain
    c.store = pipe.store
    return c


def upload(api, key):
    rid = api.post("/api/runs", files={"file": (f"{key}.pdf", pdf_for(key), "application/pdf")}).json()["run_id"]
    api.drain()
    return api.get(f"/api/runs/{rid}").json()


def test_alert_only_when_a_person_is_needed(api):
    upload(api, "H1")
    assert api.get("/api/notifications").json()["items"] == []
    d = upload(api, "D1")
    items = api.get("/api/notifications").json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "run_needs_review" and items[0]["status"] == "preview"
    assert items[0]["run_id"] == d["id"] and "Price variance" in items[0]["text"]
    assert f"/runs/{d['id']}/review" in str(items[0]["payload"]["blocks"])
    assert "correction email to the vendor is drafted" in items[0]["text"]


def test_alerts_can_be_turned_off(api, monkeypatch):
    monkeypatch.setenv("COCKPIT_SLACK_ALERTS", "off")
    upload(api, "D1")
    assert api.get("/api/notifications").json()["items"] == []


def test_webhook_receives_the_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    got = []

    def handler(req):
        got.append(req)
        return httpx.Response(200, text="ok")

    store = Store(tmp_path)
    s = Slack(store, httpx.Client(transport=httpx.MockTransport(handler)))
    row = s.post("test", "Title", "Body")
    assert row["status"] == "sent" and got[0].url.host == "hooks.slack.com" and b'"text"' in got[0].content


def test_webhook_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    s = Slack(Store(tmp_path), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, text="no"))))
    row = s.post("test", "T", "B")
    assert row["status"] == "failed" and "404" in row["error"]


def test_daily_digest_summarises_the_day(api):
    for k in ("H1", "S1", "D1", "E1"):
        upload(api, k)
    d = api.get("/api/notify/digest/preview").json()
    assert d["invoices"] == 4 and d["counts"] == {"auto_approved": 2, "needs_review": 1, "rejected": 1}
    assert "4 invoice(s) processed" in d["text"] and "Needs attention today" in d["text"]
    assert "Review queue (all days):* 1 open" in d["text"]
    sent = api.post("/api/notify/digest").json()
    assert sent["kind"] == "digest" and sent["status"] == "preview"


def test_digest_on_an_empty_day(api):
    assert "No invoices or emails today." in api.get("/api/notify/digest/preview").json()["text"]


def test_scheduler_sends_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_DIGEST_TIME", "18:00")
    store = Store(tmp_path)
    sch = DigestScheduler(Notifier(store))
    assert sch.tick(datetime(2026, 10, 28, 17, 59)) is None
    assert sch.tick(datetime(2026, 10, 28, 18, 0))["kind"] == "digest"
    assert sch.tick(datetime(2026, 10, 28, 21, 0)) is None
    assert sch.tick(datetime(2026, 10, 29, 18, 5))["kind"] == "digest"
    assert len([n for n in store.notifications() if n["kind"] == "digest"]) == 2


def test_scheduler_off(monkeypatch):
    monkeypatch.setenv("COCKPIT_DIGEST_TIME", "off")
    assert DigestScheduler.at() is None


def test_vendor_draft_for_price_variance(api):
    d = upload(api, "D1")
    v = api.get(f"/api/runs/{d['id']}/vendor-draft").json()
    assert v["available"] and v["to"] == "billing@northwind-supply.example" and v["to_source"] == "vendor master"
    assert v["subject"] == "Invoice INV-D-001 (PO-1051): correction needed"
    assert "credit note" in v["body"] and "Accounts Payable, Demo Buyer Corp" in v["body"]
    assert "Widget B, aluminium, 12 mm, item code WB-12, PO line 2" in v["body"]
    assert "Billed 20 x $103.00 = $2,060.00; PO price $100.00 gives $2,000.00. Difference $60.00." in v["body"]
    assert "Credit note due: $60.00; corrected invoice total $6,000.00." in v["body"]


def test_vendor_draft_lists_lines_not_on_the_po():
    run = {"outcome": "needs_review", "reasons": ["LINE_UNVERIFIED"], "rules": [], "currency": "EUR",
           "invoice_number": "X-1", "total_cents": 50_000,
           "line_matches": [{"inv_index": 0, "description": "Rush fee", "sku": "RUSH", "qty_milli": 1000,
                             "unit_price_cents": 50_000, "amount_cents": 50_000, "po_line_no": None, "bundle": []}]}
    v = draft(run, {"email": "a@b.example"}, "Demo Buyer Corp")
    assert "Line 1: Rush fee (item code RUSH), 1 x EUR 500.00 = EUR 500.00: not on the purchase order." in v["body"]


def test_no_vendor_draft_for_approved_or_internal_only(api):
    h = upload(api, "H1")
    assert api.get(f"/api/runs/{h['id']}/vendor-draft").json()["available"] is False
    out = draft({"outcome": "needs_review", "reasons": ["BANK_CHANGE"], "rules": []}, None, "Demo Buyer Corp")
    assert out["available"] is False and "phone" in out["internal_notes"][0]


def test_duplicate_draft_says_already_paid(api):
    d = upload(api, "E1")
    v = api.get(f"/api/runs/{d['id']}/vendor-draft").json()
    assert v["subject"].endswith("already paid") and "already been received and paid" in v["body"]


def test_connections_endpoint(api):
    c = api.get("/api/connections").json()
    assert c["po_source"]["kind"] == "seed" and c["po_source"]["counts"]["purchase_orders"] == 11
    assert {m["name"] for m in c["mailboxes"]} == {"folder", "gmail", "outlook"}
    assert c["slack"]["configured"] is False and c["slack"]["digest_time"] is None


def test_records_endpoint_lists_the_po_book(api):
    r = api.get("/api/records").json()
    po = next(p for p in r["purchase_orders"] if p["po_number"] == "PO-1042")
    assert po["vendor_name"] == "Northwind Supply Co." and po["buyer_company"] == "Demo Buyer Corp"
    assert po["total_cents"] == 20 * 10_000 + 40 * 4_500 + 10 * 10_000
