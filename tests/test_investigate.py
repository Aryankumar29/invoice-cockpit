"""Exception investigation agent: tool loop, guardrails, storage, API. A scripted fake model; no network."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cockpit import investigate as inv_mod
from cockpit.api import create_app
from cockpit.core import seed
from cockpit.extraction import openai_llm
from cockpit.pipeline import Pipeline, Worker
from cockpit.store import Store
from test_extraction import make_pdf


def call(name, args, cid):
    return SimpleNamespace(type="function_call", call_id=cid, name=name, arguments=json.dumps(args))


def resp(*items, inp=500, out=80):
    return SimpleNamespace(output=list(items), output_text="", model="gpt-6-sol",
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out,
                                                 input_tokens_details=SimpleNamespace(cached_tokens=0)))


FINDINGS = {"summary": "Line 2 is billed at $103.00 against $100.00 on PO-1051, $60.00 over; also $999.99.",
            "findings": [{"finding": "Price is above the PO price", "evidence": "get_purchase_order: unit_price 100.00"}],
            "recommended_action": "request_credit_note", "confidence": "high",
            "draft_message": {"to": "Northwind AR", "subject": "Credit note INV-D-001",
                              "body": "Please issue a credit note for $60.00."},
            "open_questions": []}


class FakeModel:
    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(dict(kw, input=list(kw.get("input", []))))  # snapshot: the loop reuses the list
        nxt = self.script.pop(0)
        return nxt(kw) if callable(nxt) else nxt


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("COCKPIT_REPLAY_DIR", str(tmp_path / "replay"))
    monkeypatch.setattr(openai_llm, "make_client", lambda: (_ for _ in ()).throw(AssertionError("extraction must not call")))
    pipe = Pipeline(Store(tmp_path / "db"), explainer=lambda p: p["verdict"], stage_delay=0)
    api = TestClient(create_app(pipeline=pipe, start_worker=False))
    api.drain = Worker(pipe).drain
    return api


def run_fixture(api, key):
    pdf = make_pdf(seed.fixture(key))
    rid = api.post("/api/runs", files={"file": (f"{key}.pdf", pdf, "application/pdf")}).json()["run_id"]
    api.drain()
    return rid


def test_agent_investigates_with_tools_and_flags_invented_figures(app, monkeypatch):
    rid = run_fixture(app, "D1")
    before = app.get(f"/api/runs/{rid}").json()
    assert before["outcome"] == "needs_review" and before["investigation"] is None
    fake = FakeModel(resp(call("get_purchase_order", {"po_number": "PO-1051"}, "c1"),
                          call("vendor_invoice_history", {"vendor_id": "V-NW", "limit": None}, "c2")),
                     resp(call("submit_findings", FINDINGS, "c3")))
    monkeypatch.setattr(openai_llm, "make_client", lambda: fake)
    d = app.post(f"/api/runs/{rid}/investigate").json()
    i = d["investigation"]
    assert i["status"] == "submitted" and i["turns"] == 2 and [s["tool"] for s in i["steps"]] == [
        "get_purchase_order", "vendor_invoice_history"]
    assert '"unit_price": "$100.00"' in i["steps"][0]["result_preview"]
    assert i["findings"]["recommended_action"] == "request_credit_note"
    assert i["unverified_figures"] == ["$999.99"]  # $103.00, $100.00, $60.00 are in the case or tool results
    assert d["outcome"] == before["outcome"] and d["reasons"] == before["reasons"]  # advisory only
    assert d["packet_hash"] == before["packet_hash"]
    # the second turn carried the tool results back to the model
    outs = [x for x in fake.calls[1]["input"] if isinstance(x, dict) and x.get("type") == "function_call_output"]
    assert {o["call_id"] for o in outs} == {"c1", "c2"}
    assert all(k["store"] is False for k in fake.calls)
    u = app.get("/api/stats").json()["api_usage"]
    assert {p["purpose"] for p in u["by_purpose"]} == {"investigation"} and u["calls"] == 2
    exported = app.get(f"/api/runs/{rid}/extraction.json").json()
    assert exported["investigation"]["findings"]["summary"].startswith("Line 2")


def test_last_turn_forces_submission(app, monkeypatch):
    rid = run_fixture(app, "D1")
    loop = lambda kw: resp(call("get_vendor_record", {"name_or_tax_id": "Northwind"}, f"c{len(fake.calls)}"))

    def final(kw):
        assert kw["tool_choice"] == {"type": "function", "name": "submit_findings"}
        return resp(call("submit_findings", FINDINGS, "cz"))
    fake = FakeModel(loop, loop, final)
    monkeypatch.setattr(openai_llm, "make_client", lambda: fake)
    monkeypatch.setattr(inv_mod, "MAX_TURNS", 3)
    import cockpit.investigate as m
    r = m.investigate(app.get(f"/api/runs/{rid}").json(), seed.context(), client=fake, max_turns=3)
    assert r["status"] == "submitted" and r["turns"] == 3 and len(fake.calls) == 3
    assert fake.calls[0]["tool_choice"] == "auto"


def test_token_budget_stops_the_loop(app):
    rid = run_fixture(app, "D1")
    big = lambda kw: resp(call("get_vendor_record", {"name_or_tax_id": "x"}, "c"), inp=30000)
    fake = FakeModel(big, lambda kw: resp(call("submit_findings", FINDINGS, "z")))
    r = inv_mod.investigate(app.get(f"/api/runs/{rid}").json(), seed.context(), client=fake, token_budget=20000)
    assert r["status"] == "submitted" and len(fake.calls) == 2
    assert fake.calls[1]["tool_choice"] == {"type": "function", "name": "submit_findings"}


def test_tools_are_read_only_views_of_the_ledger(app):
    run_fixture(app, "S1")  # bills $6,000 of PO-1047
    pipe_ctx = app.app.state.pipeline.context()
    t = inv_mod.Tools(pipe_ctx)
    po = t.get_purchase_order("po-1047")
    assert po["found"] and po["billed_so_far"] == "$6,000.00" and po["remaining"] == "$4,000.00"
    assert t.get_purchase_order("PO-9999")["found"] is False
    v = t.get_vendor_record("94-1234567")
    assert v["vendor_id"] == "V-NW" and v["bank_details_on_file"] is True and "bank_hash" not in v
    assert t.get_vendor_record("Totally Unknown Ltd")["found"] is False
    h = t.vendor_invoice_history("V-GX")
    assert h["count"] >= 1 and h["invoices"][0]["lines"][0]["unit_price"] == "$1,000.00"
    assert t.search_invoices(invoice_number="NW-2026-001")["count"] >= 1
    assert t.dispatch("delete_everything", {})["error"].startswith("unknown tool")


def test_endpoint_refuses_auto_approved_and_non_openai(app, monkeypatch):
    rid = run_fixture(app, "H1")
    assert app.post(f"/api/runs/{rid}/investigate").status_code == 409
    rid2 = run_fixture(app, "D1")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert app.post(f"/api/runs/{rid2}/investigate").status_code == 501


def test_agent_without_submission_falls_back_to_escalate(app):
    rid = run_fixture(app, "D1")
    fake = FakeModel(resp())  # no tool calls, no text
    r = inv_mod.investigate(app.get(f"/api/runs/{rid}").json(), seed.context(), client=fake)
    assert r["status"] == "no_submit" and r["findings"]["recommended_action"] == "escalate"
