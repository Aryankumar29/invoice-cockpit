"""The LangGraph invoice graph: decides exactly like the v1 pipeline, pauses for reviewers, survives a crash."""
import importlib.util
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END

from cockpit.api import create_app
from cockpit.core.engine import Decision
from cockpit.pipeline import STAGES, NotWaitingForReview, Pipeline, Worker
from cockpit.store import Store
from test_api import pdf_for, stub_extract

ROOT = Path(__file__).resolve().parents[1]


def make(tmp_path, **kw):
    kw.setdefault("extractor", stub_extract)
    kw.setdefault("explainer", lambda p: p["verdict"])
    return Pipeline(Store(tmp_path), stage_delay=0, **kw)


def submit(pipe, key):
    pdf = pdf_for(key)
    import hashlib
    return pipe.store.create_run(f"{key}.pdf", hashlib.sha256(pdf).hexdigest(), pdf)[0]


# --- parity with v1 ---------------------------------------------------------------------------------------------

def test_decisions_match_the_v1_pipeline(tmp_path, monkeypatch):
    """All 22 fixtures in demo order through the graph: outcome, reasons, packet SHA-256 and the event sequence
    equal the snapshot taken from the hand-orchestrated v1 pipeline (git tag v1-pipeline)."""
    from cockpit.extraction import local
    if not local.tesseract_available():
        pytest.skip("tesseract not installed (the scanned fixture needs it; runs in Docker)")
    spec = importlib.util.spec_from_file_location("snap", ROOT / "scripts" / "decision_snapshot.py")
    snap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snap)
    monkeypatch.setenv("COCKPIT_EXTRACTION", "local")
    manifest = json.loads((ROOT / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    pdfs = [ROOT / "fixtures" / c["file"] for c in sorted(manifest, key=lambda c: c.get("order", 99))]
    golden = json.loads((ROOT / "tests" / "golden" / "v1_decisions.json").read_text(encoding="utf-8"))
    assert snap.snapshot(pdfs) == golden


# --- topology ---------------------------------------------------------------------------------------------------

def test_topology_is_fixed_in_code(tmp_path):
    g = make(tmp_path).graph.get_graph()
    edges = {(e.source, e.target) for e in g.edges}
    chain = list(STAGES) + ["finalize"]
    for here, nxt in zip(chain, chain[1:]):
        assert (here, nxt) in edges and (here, "failed") in edges
    assert ("human_review", "human_review") in edges and ("human_review", END) in edges
    assert ("finalize", "human_review") in edges and ("finalize", END) in edges
    assert {n for n in g.nodes} >= set(STAGES) | {"finalize", "failed", "investigate", "human_review"}


# --- human review -----------------------------------------------------------------------------------------------

def test_held_invoice_pauses_at_human_review_and_resumes(tmp_path):
    pipe = make(tmp_path)
    rid = submit(pipe, "D1")
    Worker(pipe).drain()
    run = pipe.store.run(rid)
    assert run["status"] == "done" and run["outcome"] == "needs_review"
    ask = pipe.waiting_for_review(rid)
    assert pipe.state(rid).next == ("human_review",)
    assert ask["run_id"] == rid and ask["reasons"] == run["reasons"] and "approve" in ask["allowed_actions"]
    assert pipe.review(rid, "approve", "Ana", "Price agreed by email") is None
    assert pipe.store.run(rid)["outcome"] == "approved" and pipe.state(rid).next == ()
    assert [a["action"] for a in pipe.store.actions(rid)] == ["approve"]
    with pytest.raises(NotWaitingForReview):
        pipe.review(rid, "reject", "Ben", "too late")


def test_clean_invoice_never_waits(tmp_path):
    pipe = make(tmp_path)
    rid = submit(pipe, "H1")
    Worker(pipe).drain()
    assert pipe.store.run(rid)["outcome"] == "auto_approved"
    assert pipe.waiting_for_review(rid) is None and pipe.state(rid).next == ()


def test_dual_control_loops_until_a_second_reviewer(tmp_path):
    def bank_change(pdf, h, f=""):
        r = stub_extract(pdf, h, f)
        r.invoice.bank_hash = "acct-different"
        return r
    pipe = make(tmp_path, extractor=bank_change)
    rid = submit(pipe, "H1")
    Worker(pipe).drain()
    ask = pipe.review(rid, "approve", "Ana", "Called the vendor")
    assert ask["dual_control"] and ask["approved_by"] == ["Ana"]
    assert pipe.store.run(rid)["outcome"] == "needs_review"
    with pytest.raises(ValueError, match="different reviewer"):
        pipe.review(rid, "approve", "ANA", "again")
    assert pipe.waiting_for_review(rid) is not None  # still paused, nothing recorded for the refused approval
    assert pipe.review(rid, "approve", "Ben", "Verified out of band") is None
    assert pipe.store.run(rid)["outcome"] == "approved"
    assert [a["reviewer"] for a in pipe.store.actions(rid)] == ["Ana", "Ben"]


def test_escalate_keeps_waiting_then_reject(tmp_path):
    pipe = make(tmp_path)
    rid = submit(pipe, "D1")
    Worker(pipe).drain()
    assert pipe.review(rid, "escalate", "Ana", "Needs the controller") is not None
    assert pipe.review(rid, "reject", "Cleo", "Over-billed") is None
    assert pipe.store.run(rid)["outcome"] == "rejected"
    assert pipe.store.run(rid)["prior"]["outcome"] == "rejected"  # releases the PO balance for later invoices


def test_api_refuses_runs_that_are_not_paused(tmp_path):
    pipe = make(tmp_path)
    api = TestClient(create_app(pipeline=pipe, start_worker=False))
    rid = api.post("/api/runs", files={"file": ("d.pdf", pdf_for("D1"), "application/pdf")}).json()["run_id"]
    Worker(pipe).drain()
    pipe.reset()  # checkpoints gone: the run can no longer be resumed
    r = api.post(f"/api/runs/{rid}/actions", json={"action": "approve", "reviewer": "Ana", "note": "ok"})
    assert r.status_code == 409 and "not waiting" in r.json()["detail"]


# --- durability -------------------------------------------------------------------------------------------------

class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so no stage handler catches it."""


def test_crash_mid_run_resumes_from_the_last_saved_stage(tmp_path):
    def crash(packet):
        raise Crash()
    pipe = make(tmp_path, explainer=crash)
    rid = submit(pipe, "D1")
    pipe.store.claim(rid)
    with pytest.raises(Crash):
        pipe.process(rid)
    assert pipe.store.run(rid)["status"] == "running" and pipe.state(rid).next == ("explanation",)

    restarted = make(tmp_path)  # a new process: new graph, new connection to the same checkpoint file
    assert restarted.store.requeue_interrupted(resumable=restarted.has_checkpoint) == 1
    Worker(restarted).drain()
    run = restarted.store.run(rid)
    stages = [(e["stage"], e["status"]) for e in restarted.store.events(rid)]
    assert run["status"] == "done" and run["outcome"] == "needs_review"
    assert stages.count(("intake", "started")) == 1  # earlier stages were not re-run
    assert ("explanation", "warn") in stages and stages[-1] == ("decision", "ok")

    clean = make(tmp_path / "clean")
    rid2 = submit(clean, "D1")
    Worker(clean).drain()
    assert run["detail"]["packet_hash"] == clean.store.run(rid2)["detail"]["packet_hash"]


def test_state_reloads_from_disk_with_the_allowlisted_types(tmp_path, caplog):
    pipe = make(tmp_path)
    rid = submit(pipe, "D1")
    Worker(pipe).drain()
    with caplog.at_level(logging.WARNING):
        values = make(tmp_path).state(rid).values  # deserialized by a fresh process
    d = values["decision"]
    assert isinstance(d, Decision) and d.packet_hash == pipe.store.run(rid)["detail"]["packet_hash"]
    assert values["invoice"].invoice_date is not None
    assert not [r for r in caplog.records if "deserializ" in r.getMessage().lower()]


def test_reset_drops_every_thread(tmp_path):
    pipe = make(tmp_path)
    rid = submit(pipe, "D1")
    Worker(pipe).drain()
    assert pipe.has_checkpoint(rid)
    pipe.reset()
    assert not pipe.has_checkpoint(rid)
