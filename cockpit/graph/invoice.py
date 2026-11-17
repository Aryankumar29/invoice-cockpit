"""Invoice graph: the nine pipeline stages as a LangGraph StateGraph, with a human review step that pauses the run.

    intake -> extraction -> validation -> vendor -> po -> matching -> policy -> decision -> explanation -> finalize
    finalize --needs_review--> [investigate] -> human_review (interrupt; loops until a final outcome) -> END
    any stage that stops or crashes -> failed -> END

Every edge is chosen by code from a field the rules set (stop, outcome). No model output picks a route or an
outcome: extraction reads, policy R01-R16 decides, a person resolves what the rules hold (docs/DESIGN.md D1, D2).
Stage logic lives in cockpit/nodes; this module only wires it, records events and persists results."""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .. import usage
from ..core.engine import Decision, _jsonable, to_prior
from ..core.matching import LineMatch
from ..nodes import NODES, StageStop
from ..nodes import decision as n_decision, explanation as n_explanation, extraction as n_extraction
from ..nodes import intake as n_intake, matching as n_matching, po as n_po, policy as n_policy
from ..nodes import validation as n_validation, vendor as n_vendor
from ..nodes._shared import line_rows, rule_row
from ..nodes.decision import dual_control_required

if TYPE_CHECKING:
    from ..pipeline import Pipeline

REVIEW_ACTIONS = ("approve", "reject", "return_to_vendor", "escalate")
AFTER_ACTION = {"reject": "rejected", "return_to_vendor": "returned", "escalate": "needs_review"}


class InvoiceState(TypedDict, total=False):
    run_id: str
    t0: float                   # wall-clock start, so duration survives a resume in another process
    extraction: Any             # ExtractionResult
    invoice: Any                # Invoice (domain)
    vendor_match: Any
    po_match: Any
    decision: Any               # Decision: outcome, reasons, rule results, packet + hash
    explanation: tuple          # (text, source)
    stop: dict | None           # set when a stage stops or crashes; routes to `failed`
    outcome: str
    dual_control: bool
    approvals: list[dict]
    review_error: str | None
    investigation: dict | None


def auto_investigate() -> bool:
    return os.environ.get("COCKPIT_AUTO_INVESTIGATE", "0") == "1"


def build(pipe: "Pipeline", checkpointer=None):
    store = pipe.store

    def emit(rid, stage, status, title, message, data=None):
        ev = store.add_event(rid, stage, status, title, message, _jsonable(data or {}))
        try:
            get_stream_writer()(ev)  # live consumers (LangGraph Studio, `graph.stream(stream_mode="custom")`)
        except Exception:
            pass

    def stage(module, work: Callable[[InvoiceState, dict], tuple[tuple, dict]]):
        """Wrap one cockpit/nodes module: 'started' event, the work, then ok/warn/stopped with its evidence.
        A StageStop or an exception becomes `stop` in the state; the edge after the node routes it to `failed`."""
        name, title = module.STAGE, module.TITLE

        def node(state: InvoiceState) -> dict:
            rid = state["run_id"]
            usage.run_id.set(rid)
            emit(rid, name, "started", title, "")
            t0 = time.perf_counter()
            try:
                (status, message, data, _value), update = work(state, store.run(rid))
            except StageStop as s:
                return {"stop": {"kind": "stage", "stage": s.stage, "title": s.title, "message": s.message,
                                 "data": s.data}}
            except Exception as e:  # a system error never becomes a decision
                return {"stop": {"kind": "system", "message": f"{type(e).__name__}: {e}", "error": str(e),
                                 "trace": traceback.format_exc()[-2000:]}}
            emit(rid, name, status, title, message, dict(data or {}, ms=round((time.perf_counter() - t0) * 1000)))
            if pipe.delay:
                time.sleep(pipe.delay)
            return update

        node.__name__ = name
        return node

    # --- the nine stages -------------------------------------------------------------------------------------
    def intake(state, run):
        out = n_intake.run(Path(run["file_path"]).read_bytes(), run)
        return out, {}  # a normalized PDF (image, rotated scan) is written back to run["file_path"]

    def extraction(state, run):
        out = n_extraction.run(Path(run["file_path"]).read_bytes(), run, pipe.extractor)
        res = _as_result(out[3])
        inv = res.invoice
        inv.file_hash = run["file_hash"]
        store.update_run(run["id"], vendor_name=inv.vendor_name, invoice_number=inv.invoice_number,
                         total_cents=inv.total_cents, currency=inv.currency, extraction_source=res.source)
        return out, {"extraction": res, "invoice": inv}

    def validation(state, run):
        return n_validation.run(state["invoice"]), {}

    def vendor(state, run):
        out = n_vendor.run(state["invoice"], pipe.run_context(run["id"]))
        return out, {"vendor_match": out[3]}

    def _vendor(state):
        vm = state["vendor_match"]
        return vm.vendor if vm.status == "matched" else None

    def _po(state):
        pm = state["po_match"]
        return pm.po if pm.status in ("exact", "fuzzy") else None

    def po(state, run):
        out = n_po.run(state["invoice"], _vendor(state), pipe.run_context(run["id"]))
        return out, {"po_match": out[3]}

    def matching(state, run):
        return n_matching.run(state["invoice"], _po(state), _vendor(state), pipe.run_context(run["id"])), {}

    def policy(state, run):
        out = n_policy.run(state["invoice"], pipe.run_context(run["id"]))
        return out, {"decision": out[3]}

    def decision(state, run):
        return n_decision.run(state["decision"]), {}

    def explanation(state, run):
        out = n_explanation.run(state["decision"], pipe.explainer)
        return out, {"explanation": out[3]}

    work = {"intake": intake, "extraction": extraction, "validation": validation, "vendor": vendor, "po": po,
            "matching": matching, "policy": policy, "decision": decision, "explanation": explanation}

    # --- terminal and review nodes ---------------------------------------------------------------------------
    def finalize(state: InvoiceState) -> dict:
        rid = state["run_id"]
        inv, d, res = state["invoice"], state["decision"], state["extraction"]
        detail = _detail(inv, d, res, state["explanation"], _po(state))
        store.update_run(rid, status="done", outcome=d.outcome, reasons=d.reasons, po_number=d.po_number,
                         detail=detail, prior=_jsonable(to_prior(inv, d, rid)),
                         duration_ms=round((time.time() - state["t0"]) * 1000))
        emit(rid, "decision", "ok", "Done", d.verdict, {"outcome": d.outcome, "reasons": d.reasons})
        pipe.forget_context(rid)
        return {"outcome": d.outcome, "dual_control": detail["dual_control"], "approvals": []}

    def failed(state: InvoiceState) -> dict:
        rid, s = state["run_id"], state["stop"]
        ms = round((time.time() - state["t0"]) * 1000)
        pipe.forget_context(rid)
        if s["kind"] == "stage":
            emit(rid, s["stage"], "failed", s["title"], s["message"], s["data"])
            store.update_run(rid, status="failed", outcome="failed", reasons=[s["data"].get("reason", "FAILED")],
                             error=s["message"], duration_ms=ms,
                             detail={"verdict": s["message"], "recommended_action": "Upload a valid PDF invoice"})
        else:
            emit(rid, "decision", "failed", "System error", s["message"], {"trace": s["trace"]})
            store.update_run(rid, status="failed", outcome="failed", reasons=["SYSTEM_ERROR"], error=s["error"],
                             duration_ms=ms, detail={"verdict": f"Processing failed: {s['error']}",
                                                     "recommended_action": "Retry the run"})
        return {"outcome": "failed"}

    def investigate(state: InvoiceState) -> dict:
        """Optional (COCKPIT_AUTO_INVESTIGATE=1): the read-only investigation agent prepares the reviewer before
        the run pauses. Advisory only: it has no key in this state that can change `outcome`."""
        from . import investigation
        rid = state["run_id"]
        run = store.run(rid)
        try:
            result = investigation.investigate(case_view(run), pipe.context(exclude=rid))
        except Exception as e:  # no model, no key, network: the review simply starts without it
            return {"investigation": {"status": "error", "error": f"{type(e).__name__}: {e}"}}
        store.add_investigation(rid, result)
        return {"investigation": result}

    def human_review(state: InvoiceState) -> dict:
        """Pause until a reviewer acts. The API resumes the thread with Command(resume={action, reviewer, note}).
        Dual control: the first approval keeps the run open; the second must come from someone else."""
        rid = state["run_id"]
        approvals = list(state.get("approvals") or [])
        d = state["decision"]
        ask = interrupt({"run_id": rid, "outcome": state["outcome"], "reasons": d.reasons,
                         "dual_control": state.get("dual_control", False), "approvals_given": len(approvals),
                         "approved_by": [a["reviewer"] for a in approvals], "allowed_actions": list(REVIEW_ACTIONS)})
        action, reviewer, note = ask.get("action"), (ask.get("reviewer") or "").strip(), (ask.get("note") or "").strip()
        if action not in REVIEW_ACTIONS or not reviewer or not note:
            return {"review_error": "An allowed action, a reviewer name and a note are required"}
        before, after = state["outcome"], AFTER_ACTION.get(action, "approved")
        if action == "approve" and state.get("dual_control"):
            if any(a["reviewer"].casefold() == reviewer.casefold() for a in approvals):
                return {"review_error": "Dual control: the second approval must come from a different reviewer"}
            if not approvals:
                after = "needs_review"
        store.add_action(rid, action, reviewer, note, before, after)
        if after != before:
            store.update_run(rid, outcome=after)
            run = store.run(rid)
            if run["prior"]:
                store.update_run(rid, prior=run["prior"] | {"outcome": after})
        if action == "approve":
            approvals.append({"reviewer": reviewer, "note": note})
        return {"outcome": after, "approvals": approvals, "review_error": None}

    # --- wiring -----------------------------------------------------------------------------------------------
    g = StateGraph(InvoiceState)
    names = [m.STAGE for m in NODES]
    for m in NODES:
        g.add_node(m.STAGE, stage(m, work[m.STAGE]))
    for fn in (finalize, failed, investigate, human_review):
        g.add_node(fn.__name__, fn)

    g.add_edge(START, names[0])
    for here, nxt in zip(names, names[1:] + ["finalize"]):
        g.add_conditional_edges(here, lambda s, nxt=nxt: "failed" if s.get("stop") else nxt, [nxt, "failed"])
    g.add_conditional_edges("finalize", after_finalize, ["investigate", "human_review", END])
    g.add_edge("investigate", "human_review")
    g.add_conditional_edges("human_review", lambda s: "human_review" if s["outcome"] == "needs_review" else END,
                            ["human_review", END])
    g.add_edge("failed", END)
    return g.compile(checkpointer=checkpointer, name="invoice")


def after_finalize(state: InvoiceState) -> str:
    if state["outcome"] != "needs_review":
        return END
    return "investigate" if auto_investigate() else "human_review"


def _as_result(res):
    """Whatever the configured extractor returned, the state holds an ExtractionResult: a checkpointed type on
    the deserialization allowlist (graph/checkpoint.py), so a resumed run reads back the same object."""
    from ..extraction import ExtractionResult
    if isinstance(res, ExtractionResult):
        return res
    return ExtractionResult(res.invoice, res.source, res.grounding, list(getattr(res, "warnings", []) or []),
                            list(getattr(res, "attempts", []) or []), dict(getattr(res, "raw", {}) or {}),
                            list(getattr(res, "notes", []) or []))


def case_view(run: dict) -> dict:
    """The run as the investigation agent's brief expects it (the API's detail view, minus UI-only fields)."""
    return {**(run.get("detail") or {}), **{k: v for k, v in run.items() if k != "detail"}}


def _detail(inv, d: Decision, res, explanation, po) -> dict:
    text, src = explanation
    lines = []
    if po:
        lm = [LineMatch(**m) for m in d.packet["line_matches"]]
        lines = line_rows(d.work or inv, po, lm)
    detail = {
        "verdict": d.verdict, "recommended_action": d.recommended_action, "explanation": text,
        "explain_source": src, "rules": [rule_row(r) for r in d.results], "invoice": d.packet["invoice"],
        "line_matches": lines, "allocations": d.packet["allocations"], "duplicates": d.packet["duplicates"],
        "tax_basis": d.tax,
        "vendor_match": d.packet["vendor_match"], "po_match": d.packet["po_match"],
        "grounding": res.grounding, "warnings": list(res.warnings), "attempts": res.attempts,
        "packet_hash": d.packet_hash, "policy_version": d.policy_version, "packet": d.packet,
    }
    detail["dual_control"] = dual_control_required(detail, inv.total_cents)
    return detail
