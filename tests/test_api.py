"""Pipeline + API end to end with a stub extractor (seed fixtures), so no API key is needed."""
import io
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from cockpit.api import create_app
from cockpit.core.seed import EXPECTED, fixture
from cockpit.pipeline import STAGES, Pipeline, Worker
from cockpit.store import Store


@dataclass
class StubResult:
    invoice: object
    source: str = "local_text"
    grounding: dict = field(default_factory=lambda: {"checked": 3, "grounded": 3, "ungrounded": [], "arithmetic_ok": True})
    warnings: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def stub_extract(pdf: bytes, file_hash: str, filename: str = ""):
    key = pdf.split(b"(FIXTURE ", 1)[1].split(b")", 1)[0].decode()
    return StubResult(fixture(key))


def pdf_for(key: str, pages: int = 1) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pageCompression=0, invariant=1)
    for _ in range(pages):
        c.drawString(72, 720, f"FIXTURE {key}")
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path)
    pipe = Pipeline(store, extractor=stub_extract, explainer=lambda p: p["verdict"], stage_delay=0)
    client = TestClient(create_app(pipeline=pipe, start_worker=False))
    client.drain = Worker(pipe).drain
    return client


def upload(api, key, **kw):
    r = api.post("/api/runs", files={"file": (f"{key}.pdf", pdf_for(key, **kw), "application/pdf")})
    assert r.status_code == 200
    api.drain()
    return r.json()


def test_demo_sequence_end_to_end(api):
    for key in ("H1", "S1", "S2", "D1", "E1", "E2"):
        rid = upload(api, key)["run_id"]
        d = api.get(f"/api/runs/{rid}").json()
        assert (d["outcome"], d["reasons"][0]) == EXPECTED[key], key
        assert [e["stage"] for e in d["stages"] if e["status"] == "started"] == list(STAGES)
        assert len(d["rules"]) == 16 and d["packet_hash"]
    runs = api.get("/api/runs").json()
    assert len(runs) == 6 and runs[0]["filename"] == "E2.pdf"
    s = api.get("/api/stats").json()
    assert s["total"] == 6 and s["by_outcome"]["auto_approved"] == 3 and s["stp_rate"] == 0.5


def test_split_po_ledger_persists_across_runs(api):
    upload(api, "S1")
    rid = upload(api, "S2")["run_id"]
    r12 = next(r for r in api.get(f"/api/runs/{rid}").json()["rules"] if r["rule"] == "R12")
    assert r12["evidence"]["remaining_after"] == 0


def test_same_file_twice_links_existing_run(api):
    a = upload(api, "H1")
    b = upload(api, "H1")
    assert b == {"run_id": a["run_id"], "existing": True}
    assert len(api.get("/api/runs").json()) == 1


@pytest.mark.parametrize("data,reason", [
    (b"PK\x03\x04 a zip, not an invoice", "NOT_A_PDF"),
    (b"\x89PNG\r\n\x1a\nnot really a png", "IMAGE_CORRUPT"),
    (b"\x00\x00\x00\x18ftypheic....", "UNSUPPORTED_FORMAT"),
    (b"%PDF-1.4 garbage", "PDF_CORRUPT"),
])
def test_bad_files_fail_with_plain_message(api, data, reason):
    rid = api.post("/api/runs", files={"file": ("x.pdf", data, "application/pdf")}).json()["run_id"]
    api.drain()
    d = api.get(f"/api/runs/{rid}").json()
    assert d["outcome"] == "failed" and d["reasons"] == [reason] and d["verdict"]


def test_page_limit(api):
    rid = upload(api, "H1", pages=11)["run_id"]
    assert api.get(f"/api/runs/{rid}").json()["reasons"] == ["PAGE_LIMIT"]


def test_events_polling_and_sse_replay(api):
    rid = upload(api, "D1")["run_id"]
    evs = api.get(f"/api/runs/{rid}/events.json").json()
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert api.get(f"/api/runs/{rid}/events.json", params={"after": 10}).json()[0]["seq"] == 11
    with api.stream("GET", f"/api/runs/{rid}/events", headers={"Last-Event-ID": str(len(evs) - 2)}) as r:
        body = "".join(r.iter_text())
    ids = [int(l[4:]) for l in body.splitlines() if l.startswith("id: ")]
    assert ids == [len(evs) - 1, len(evs)] and "event: done" in body


def test_review_actions_and_ledger_release(api):
    rid = upload(api, "D1")["run_id"]
    assert api.post(f"/api/runs/{rid}/actions", json={"action": "reject", "reviewer": "", "note": ""}).status_code == 400
    d = api.post(f"/api/runs/{rid}/actions",
                 json={"action": "reject", "reviewer": "Priya", "note": "Price above PO"}).json()
    assert d["outcome"] == "rejected" and d["allowed_actions"] == [] and d["actions"][0]["outcome_after"] == "rejected"
    assert api.post(f"/api/runs/{rid}/actions",
                    json={"action": "approve", "reviewer": "Priya", "note": "x"}).status_code == 409


def test_bank_change_needs_two_different_approvers(tmp_path):
    def bank_change(pdf, h, f=""):
        inv = fixture("H1")
        inv.bank_hash = "acct-different"
        return StubResult(inv)
    pipe = Pipeline(Store(tmp_path), extractor=bank_change, explainer=lambda p: "", stage_delay=0)
    api = TestClient(create_app(pipeline=pipe, start_worker=False))
    rid = api.post("/api/runs", files={"file": ("b.pdf", pdf_for("H1"), "application/pdf")}).json()["run_id"]
    Worker(pipe).drain()
    d = api.get(f"/api/runs/{rid}").json()
    assert d["reasons"][0] == "BANK_CHANGE" and d["dual_control"]
    d = api.post(f"/api/runs/{rid}/actions", json={"action": "approve", "reviewer": "Ana", "note": "Called vendor"}).json()
    assert d["outcome"] == "needs_review" and d["approvals"]["given"] == 1
    assert api.post(f"/api/runs/{rid}/actions",
                    json={"action": "approve", "reviewer": "ana", "note": "again"}).status_code == 409
    d = api.post(f"/api/runs/{rid}/actions", json={"action": "approve", "reviewer": "Ben", "note": "Verified"}).json()
    assert d["outcome"] == "approved"


def test_extractor_crash_is_failed_not_a_decision(tmp_path):
    def boom(*a):
        raise RuntimeError("model down")
    pipe = Pipeline(Store(tmp_path), extractor=boom, stage_delay=0)
    api = TestClient(create_app(pipeline=pipe, start_worker=False))
    rid = api.post("/api/runs", files={"file": ("h.pdf", pdf_for("H1"), "application/pdf")}).json()["run_id"]
    Worker(pipe).drain()
    d = api.get(f"/api/runs/{rid}").json()
    assert d["outcome"] == "failed" and d["reasons"] == ["SYSTEM_ERROR"]


def test_interrupted_run_restarts_cleanly(tmp_path):
    store = Store(tmp_path)
    rid, _ = store.create_run("h.pdf", "abc", pdf_for("H1"))
    store.claim(rid)
    store.add_event(rid, "intake", "started", "Intake", "")
    assert store.requeue_interrupted() == 1 and store.events(rid) == []
    pipe = Pipeline(store, extractor=stub_extract, explainer=lambda p: "", stage_delay=0)
    Worker(pipe).drain()
    assert store.run(rid)["outcome"] == "auto_approved"


def test_reset_and_health(api):
    upload(api, "H1")
    assert api.post("/api/reset").json() == {"ok": True}
    assert api.get("/api/runs").json() == []
    h = api.get("/api/health").json()
    assert h["ok"] and h["extraction_mode"] in ("claude", "local")


def test_unknown_run_404(api):
    assert api.get("/api/runs/nope").status_code == 404


def test_duplicate_links_to_seeded_original(api):
    rid = upload(api, "E1")["run_id"]
    hit = api.get(f"/api/runs/{rid}").json()["duplicates"]["hits"][0]["run_id"]
    orig = api.get(f"/api/runs/{hit}").json()
    assert orig["invoice_number"] == "NW-2026-001" and orig["seeded"] and orig["allowed_actions"] == []
