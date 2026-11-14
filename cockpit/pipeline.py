"""Code-orchestrated pipeline (docs/DESIGN.md D1): runs the nine nodes in cockpit/nodes/ in order, persists each
result as an event, and drains the queue on one DB-backed worker."""
from __future__ import annotations

import copy
import os
import threading
import time
import traceback
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Callable

from . import sources, usage
from .core import seed
from .core.domain import Allocation, Context, Invoice, PriorInvoice
from .core.engine import Decision, _jsonable, to_prior
from .core.matching import LineMatch
from .nodes import STAGES, StageStop
from .nodes import decision as n_decision, explanation as n_explanation, extraction as n_extraction
from .nodes import intake as n_intake, matching as n_matching, po as n_po, policy as n_policy
from .nodes import validation as n_validation, vendor as n_vendor
from .nodes._shared import line_rows, rule_row
from .nodes.decision import dual_control_required
from .store import Store

__all__ = ["STAGES", "Pipeline", "StageStop", "Worker", "dual_control_required", "prior_from_dict"]


def _date(v):
    return date.fromisoformat(v) if isinstance(v, str) else v


def prior_from_dict(d: dict) -> PriorInvoice:
    kw = {f.name: d.get(f.name) for f in fields(PriorInvoice) if f.name in d}
    for k in ("invoice_date", "approved_on", "paid_on"):
        kw[k] = _date(kw.get(k))
    kw["allocations"] = [Allocation(**a) for a in d.get("allocations") or []]
    kw["line_descriptions"] = list(d.get("line_descriptions") or [])
    return PriorInvoice(**kw)


def _default_extractor():
    from .extraction import extract
    return extract


def _default_explainer():
    from .explain import explain_source
    return explain_source


class Pipeline:
    def __init__(self, store: Store, extractor: Callable | None = None, explainer: Callable | None = None,
                 stage_delay: float | None = None, today: date | None = None):
        self.store = store
        self._extractor = extractor
        self._explainer = explainer
        self.delay = float(os.environ.get("COCKPIT_STAGE_DELAY_MS", "350")) / 1000 if stage_delay is None else stage_delay
        self.today = today or _date(os.environ.get("COCKPIT_TODAY")) or seed.TODAY
        self.listeners: list[Callable[[str], None]] = []  # called with the run id once a run finishes or fails

    @property
    def extractor(self):
        if self._extractor is None:
            self._extractor = _default_extractor()
        return self._extractor

    @property
    def explainer(self):
        if self._explainer is None:
            self._explainer = _default_explainer()
        return self._explainer

    def context(self, exclude: str | None = None) -> Context:
        """Master data from the configured PO source (cockpit/sources), copied so a run never edits the cache."""
        rec = sources.records()
        history = seed.history() + [prior_from_dict(p) for p in self.store.priors(exclude)]
        return Context(copy.deepcopy(rec.vendors), copy.deepcopy(rec.pos), history, self.today)

    # ------------------------------------------------------------------
    def _emit(self, rid, stage, status, title, message, data=None):
        self.store.add_event(rid, stage, status, title, message, _jsonable(data or {}))

    def _stage(self, rid, node, fn):
        """Run one node: a 'started' event, the work, then ok/warn/stopped with its evidence."""
        stage, title = node.STAGE, node.TITLE
        self._emit(rid, stage, "started", title, "")
        t0 = time.perf_counter()
        status, message, data, value = fn()
        data = dict(data or {}, ms=round((time.perf_counter() - t0) * 1000))
        self._emit(rid, stage, status, title, message, data)
        if self.delay:
            time.sleep(self.delay)
        return value

    def process(self, rid: str) -> None:
        self._process(rid)
        for fn in self.listeners:
            try:
                fn(rid)
            except Exception:  # a notification problem never touches the decision
                traceback.print_exc()

    def _process(self, rid: str) -> None:
        t0 = time.perf_counter()
        run = self.store.run(rid)
        usage.run_id.set(rid)
        try:
            pdf = Path(run["file_path"]).read_bytes()
            pdf = self._stage(rid, n_intake, lambda: n_intake.run(pdf, run))
            res = self._stage(rid, n_extraction, lambda: n_extraction.run(pdf, run, self.extractor))
            inv: Invoice = res.invoice
            inv.file_hash = run["file_hash"]
            self.store.update_run(rid, vendor_name=inv.vendor_name, invoice_number=inv.invoice_number,
                                  total_cents=inv.total_cents, currency=inv.currency,
                                  extraction_source=res.source)
            ctx = self.context(exclude=rid)
            self._stage(rid, n_validation, lambda: n_validation.run(inv))
            vm = self._stage(rid, n_vendor, lambda: n_vendor.run(inv, ctx))
            vendor = vm.vendor if vm.status == "matched" else None
            pm = self._stage(rid, n_po, lambda: n_po.run(inv, vendor, ctx))
            po = pm.po if pm.status in ("exact", "fuzzy") else None
            self._stage(rid, n_matching, lambda: n_matching.run(inv, po, vendor, ctx))
            d: Decision = self._stage(rid, n_policy, lambda: n_policy.run(inv, ctx))
            self._stage(rid, n_decision, lambda: n_decision.run(d))
            explanation = self._stage(rid, n_explanation, lambda: n_explanation.run(d, self.explainer))
            detail = self._detail(inv, d, res, explanation, po)
            self.store.update_run(rid, status="done", outcome=d.outcome, reasons=d.reasons, po_number=d.po_number,
                                  detail=detail, prior=_jsonable(to_prior(inv, d, rid)),
                                  duration_ms=round((time.perf_counter() - t0) * 1000))
            self._emit(rid, "decision", "ok", "Done", d.verdict, {"outcome": d.outcome, "reasons": d.reasons})
        except StageStop as s:
            self._emit(rid, s.stage, "failed", s.title, s.message, s.data)
            self.store.update_run(rid, status="failed", outcome="failed", reasons=[s.data.get("reason", "FAILED")],
                                  error=s.message, duration_ms=round((time.perf_counter() - t0) * 1000),
                                  detail={"verdict": s.message, "recommended_action": "Upload a valid PDF invoice"})
        except Exception as e:  # a system error never becomes a decision
            self._emit(rid, "decision", "failed", "System error", f"{type(e).__name__}: {e}",
                       {"trace": traceback.format_exc()[-2000:]})
            self.store.update_run(rid, status="failed", outcome="failed", reasons=["SYSTEM_ERROR"], error=str(e),
                                  duration_ms=round((time.perf_counter() - t0) * 1000),
                                  detail={"verdict": f"Processing failed: {e}", "recommended_action": "Retry the run"})


    def _detail(self, inv: Invoice, d: Decision, res, explanation, po) -> dict:
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


class Worker:
    """One background thread draining the DB queue in order, so ledger checks see every earlier invoice."""

    def __init__(self, pipeline: Pipeline, poll: float = 0.2):
        self.pipeline, self.poll = pipeline, poll
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.pipeline.store.requeue_interrupted()
        self._thread = threading.Thread(target=self._loop, name="cockpit-worker", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def drain(self) -> None:
        """Process everything queued on the calling thread (tests, CLI)."""
        while (rid := self.pipeline.store.next_queued()) is not None:
            if self.pipeline.store.claim(rid):
                self.pipeline.process(rid)

    def _loop(self) -> None:
        while not self._stop.is_set():
            rid = self.pipeline.store.next_queued()
            if rid and self.pipeline.store.claim(rid):
                self.pipeline.process(rid)
                continue
            self._wake.wait(self.poll)
            self._wake.clear()
