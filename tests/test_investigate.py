"""Exception investigation agent (LangGraph tool loop): lookups, guardrails, storage, API. A scripted fake chat
model; no network."""
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from cockpit import investigate as inv_mod
from cockpit.api import create_app
from cockpit.core import seed
from cockpit.extraction import openai_llm
from cockpit.graph import investigation as agent
from cockpit.pipeline import Pipeline, Worker
from cockpit.store import Store
from test_extraction import make_pdf


def call(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def resp(*calls, inp=500, out=80):
    return AIMessage(content="", tool_calls=list(calls), response_metadata={"model_name": "gpt-6-sol"},
                     usage_metadata={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out})


FINDINGS = {"summary": "Line 2 is billed at $103.00 against $100.00 on PO-1051, $60.00 over; also $999.99.",
            "findings": [{"finding": "Price is above the PO price", "evidence": "get_purchase_order: unit_price 100.00"}],
            "recommended_action": "request_credit_note", "confidence": "high",
            "draft_message": {"to": "Northwind AR", "subject": "Credit note INV-D-001",
                              "body": "Please issue a credit note for $60.00."},
            "open_questions": []}


class FakeChat(BaseChatModel):
    """Plays a script of replies. Records every turn: the messages it saw and the forced tool choice."""
    script: list = []
    calls: list = []
    model_name: str = "gpt-6-sol"

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, tool_choice=None, **kw):
        return self.bind(tool_choice=tool_choice, tool_names=[t.name for t in tools])

    def _generate(self, messages, stop=None, run_manager=None, tool_choice: Any = None, tool_names=None, **kw):
        self.calls.append({"messages": list(messages), "tool_choice": tool_choice, "tools": tool_names})
        nxt = self.script.pop(0)
        msg = nxt(self.calls[-1]) if callable(nxt) else nxt
        return ChatResult(generations=[ChatGeneration(message=msg)])


def fake(*script):
    return FakeChat(script=list(script), calls=[])


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
    model = fake(resp(call("get_purchase_order", {"po_number": "PO-1051"}, "c1"),
                      call("vendor_invoice_history", {"vendor_id": "V-NW", "limit": None}, "c2")),
                 resp(call("submit_findings", FINDINGS, "c3")))
    monkeypatch.setattr(agent, "chat_model", lambda: model)
    d = app.post(f"/api/runs/{rid}/investigate").json()
    i = d["investigation"]
    assert i["status"] == "submitted" and i["turns"] == 2 and [s["tool"] for s in i["steps"]] == [
        "get_purchase_order", "vendor_invoice_history"]
    assert '"unit_price": "$100.00"' in i["steps"][0]["result_preview"]
    assert i["findings"]["recommended_action"] == "request_credit_note"
    assert i["unverified_figures"] == ["$999.99"]  # $103.00, $100.00, $60.00 are in the case or tool results
    assert d["outcome"] == before["outcome"] and d["reasons"] == before["reasons"]  # advisory only
    assert d["packet_hash"] == before["packet_hash"]
    # the second turn carried both lookup results back to the model
    outs = [m for m in model.calls[1]["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in outs} == {"c1", "c2"}
    assert set(model.calls[0]["tools"]) == {"get_purchase_order", "get_vendor_record", "vendor_invoice_history",
                                            "search_invoices", "submit_findings"}
    u = app.get("/api/stats").json()["api_usage"]
    assert {p["purpose"] for p in u["by_purpose"]} == {"investigation"} and u["calls"] == 2
    exported = app.get(f"/api/runs/{rid}/extraction.json").json()
    assert exported["investigation"]["findings"]["summary"].startswith("Line 2")


def test_last_turn_forces_submission(app):
    rid = run_fixture(app, "D1")
    loop = lambda c: resp(call("get_vendor_record", {"name_or_tax_id": "Northwind"}, f"c{len(model.calls)}"))

    def final(c):
        assert c["tool_choice"] == "submit_findings"
        return resp(call("submit_findings", FINDINGS, "cz"))
    model = fake(loop, loop, final)
    r = agent.investigate(app.get(f"/api/runs/{rid}").json(), seed.context(), model=model, max_turns=3)
    assert r["status"] == "submitted" and r["turns"] == 3 and len(model.calls) == 3
    assert model.calls[0]["tool_choice"] is None


def test_token_budget_stops_the_loop(app):
    rid = run_fixture(app, "D1")
    big = lambda c: resp(call("get_vendor_record", {"name_or_tax_id": "x"}, "c"), inp=30000)
    model = fake(big, lambda c: resp(call("submit_findings", FINDINGS, "z")))
    r = agent.investigate(app.get(f"/api/runs/{rid}").json(), seed.context(), model=model, token_budget=20000)
    assert r["status"] == "submitted" and len(model.calls) == 2
    assert model.calls[1]["tool_choice"] == "submit_findings"


def test_turn_limit_without_submission_escalates(app):
    rid = run_fixture(app, "D1")
    model = fake(*[lambda c: resp(call("search_invoices", {"invoice_number": "INV"}, f"c{len(model.calls)}"))] * 2)
    r = agent.investigate(app.get(f"/api/runs/{rid}").json(), seed.context(), model=model, max_turns=2)
    assert r["status"] == "turn_limit" and r["findings"]["recommended_action"] == "escalate"


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
    assert {x.name for x in agent.lookups(t)} == {"get_purchase_order", "get_vendor_record",
                                                  "vendor_invoice_history", "search_invoices"}


def test_state_has_no_field_that_can_change_a_decision():
    assert not {"outcome", "decision", "reasons", "approvals"} & set(agent.InvestigationState.__annotations__)


def test_endpoint_refuses_auto_approved_and_no_model(app, monkeypatch):
    rid = run_fixture(app, "H1")
    assert app.post(f"/api/runs/{rid}/investigate").status_code == 409
    rid2 = run_fixture(app, "D1")
    monkeypatch.delenv("OPENAI_API_KEY")
    r = app.post(f"/api/runs/{rid2}/investigate")
    assert r.status_code == 501 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_claude_is_supported_too(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    m = agent.chat_model()
    assert type(m).__name__ == "ChatAnthropic"


def test_agent_without_submission_falls_back_to_escalate(app):
    rid = run_fixture(app, "D1")
    model = fake(resp())  # no tool calls, no text
    r = agent.investigate(app.get(f"/api/runs/{rid}").json(), seed.context(), model=model)
    assert r["status"] == "no_submit" and r["findings"]["recommended_action"] == "escalate"
