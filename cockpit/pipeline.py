"""Runs invoices through the LangGraph invoice graph (cockpit/graph/invoice.py) and drains the queue on one
DB-backed worker, so ledger checks see every earlier invoice in upload order (docs/DESIGN.md D7).

Each run is one checkpointed graph thread (thread_id = run id): a process that dies mid-run resumes from the
last completed stage, and a run held for review stays paused at `human_review` until a reviewer acts."""
from __future__ import annotations

import copy
import os
import threading
import time
import traceback
from dataclasses import fields
from datetime import date
from typing import Callable

from langgraph.types import Command

from . import sources
from .core import seed
from .core.domain import Allocation, Context, PriorInvoice
from .graph import checkpoint, invoice as invoice_graph
from .nodes import STAGES, StageStop
from .nodes.decision import dual_control_required
from .store import Store

__all__ = ["STAGES", "NotWaitingForReview", "Pipeline", "StageStop", "Worker", "dual_control_required",
           "prior_from_dict"]


class NotWaitingForReview(Exception):
    pass


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
                 stage_delay: float | None = None, today: date | None = None, checkpointer=None):
        self.store = store
        self._extractor = extractor
        self._explainer = explainer
        self.delay = float(os.environ.get("COCKPIT_STAGE_DELAY_MS", "350")) / 1000 if stage_delay is None else stage_delay
        self.today = today or _date(os.environ.get("COCKPIT_TODAY")) or seed.TODAY
        self.listeners: list[Callable[[str], None]] = []  # called with the run id once a run finishes, fails or pauses
        self.checkpointer = checkpointer or checkpoint.saver(store.dir)
        self.graph = invoice_graph.build(self, self.checkpointer)
        self._contexts: dict[str, Context] = {}

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

    def run_context(self, rid: str) -> Context:
        """One Context per run, shared by the vendor, PO, matching and policy stages (rebuilt after a resume:
        runs are serial, so the same master data and history give the same Context)."""
        if rid not in self._contexts:
            self._contexts[rid] = self.context(exclude=rid)
        return self._contexts[rid]

    def forget_context(self, rid: str) -> None:
        self._contexts.pop(rid, None)

    # --- graph threads ------------------------------------------------------------------------------------------
    @staticmethod
    def config(rid: str) -> dict:
        return {"configurable": {"thread_id": rid}}

    def state(self, rid: str):
        return self.graph.get_state(self.config(rid))

    def has_checkpoint(self, rid: str) -> bool:
        return bool(self.state(rid).values)

    def waiting_for_review(self, rid: str) -> dict | None:
        """The interrupt payload when the run is paused at human_review, else None."""
        for task in self.state(rid).tasks:
            for i in task.interrupts:
                return i.value
        return None

    def process(self, rid: str) -> None:
        self._process(rid)
        for fn in self.listeners:
            try:
                fn(rid)
            except Exception:  # a notification problem never touches the decision
                traceback.print_exc()

    def _process(self, rid: str) -> None:
        st = self.state(rid)
        if st.values and (not st.next or self.waiting_for_review(rid) is not None):
            return  # finished, or paused for a reviewer: nothing to run
        if st.next:  # the process died mid-run: continue from the last completed stage
            self.store.add_event(rid, st.next[0], "warn", "Resumed",
                                 "Continued from the last saved stage after a restart.", {})
            inp = None
        else:
            inp = {"run_id": rid, "t0": time.time()}
        try:
            self.graph.invoke(inp, self.config(rid))
        except Exception as e:  # the graph itself failed (not a stage): never leave the run looking alive
            self.store.add_event(rid, "decision", "failed", "System error", f"{type(e).__name__}: {e}",
                                 {"trace": traceback.format_exc()[-2000:]})
            self.store.update_run(rid, status="failed", outcome="failed", reasons=["SYSTEM_ERROR"], error=str(e),
                                  detail={"verdict": f"Processing failed: {e}", "recommended_action": "Retry the run"})

    def review(self, rid: str, action: str, reviewer: str, note: str) -> dict | None:
        """Resume a run paused at human_review with a reviewer's action. Returns the next interrupt payload (dual
        control, escalation) or None once the run has a final outcome."""
        if self.waiting_for_review(rid) is None:
            raise NotWaitingForReview(rid)
        out = self.graph.invoke(Command(resume={"action": action, "reviewer": reviewer, "note": note}),
                                self.config(rid))
        if out.get("review_error"):
            raise ValueError(out["review_error"])
        return self.waiting_for_review(rid)

    def reset(self) -> None:
        checkpoint.clear(self.checkpointer)
        self._contexts.clear()


class Worker:
    """One background thread draining the DB queue in order, so ledger checks see every earlier invoice."""

    def __init__(self, pipeline: Pipeline, poll: float = 0.2):
        self.pipeline, self.poll = pipeline, poll
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.pipeline.store.requeue_interrupted(resumable=self.pipeline.has_checkpoint)
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
