"""The extraction ladder as a LangGraph subgraph. The model reads; nothing here decides (docs/DESIGN.md D2-D4).

    text_layer -> [replay_forced] -> [cache] -> [local_first] -> llm -> [llm_repair] -> llm_accept
                                                                    `-> llm_fallthrough -> local_fallback -> replay -> settle

Each route either produces a reading that passes its checks (grounded in the PDF text, arithmetic reconciles) and
ends the graph, or falls through to the next, cheaper or safer, route. The order and every fallback condition are
edges, so the path a document took is visible in the graph and recorded in `attempts`.

Compiled with checkpointer=False: it runs inside the invoice graph's extraction node and holds the PDF bytes and
OCR cache in memory; only its result (an ExtractionResult) is checkpointed, by the parent."""
from __future__ import annotations

import os
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import extraction as X
from ..core.domain import Invoice


class Job:
    """Per-document scratch shared by the nodes: inputs, page texts, the OCR cache and the attempt log."""

    def __init__(self, pdf: bytes, file_hash: str, filename: str, known_vendor):
        self.pdf, self.file_hash, self.filename, self.known_vendor = pdf, file_hash, filename, known_vendor
        self.mode = os.environ.get("COCKPIT_EXTRACTION", "auto").lower()
        self.auto = self.mode == "auto"
        self.attempts: list[dict] = []
        self.warnings: list[str] = []
        self.candidates: list = []
        self.ocr_state: dict = {}
        self.pinfo: list[dict] = []
        self.digital: list[dict] = []
        self.scanned: list[int] = []
        self.text = self.gtext = ""
        self.name = self.llm = self.client = None
        self.llm_skipped = False
        self.local_first_tried, self.local_first_result = False, None

    # --- bookkeeping ----------------------------------------------------------------------------------------
    def step(self, name, fn):
        t0 = time.perf_counter()
        try:
            ok, detail, out = fn()
        except Exception as e:  # a broken route is a failed attempt, never a crash
            ok, detail, out = False, f"{type(e).__name__}: {e}", None
        self.attempts.append({"step": name, "ok": ok, "detail": detail, "ms": int((time.perf_counter() - t0) * 1000)})
        return out

    def done(self, c, extra=(), notes=()) -> X.ExtractionResult:
        return X.ExtractionResult(c.invoice, c.source, c.grounding, self.warnings + list(extra) + c.warnings,
                                  self.attempts, c.raw, list(notes))

    # --- readers --------------------------------------------------------------------------------------------
    def ocr_pages(self):
        """OCR of the non-digital pages, run once and only when needed. None when there is nothing to OCR."""
        if "pages" not in self.ocr_state:
            self.ocr_state["pages"] = None
            if self.scanned and X.local.tesseract_available():
                pages = self.step("ocr", lambda: (True, "", X.local.ocr_pages(self.pdf, self.scanned)))
                if pages:
                    rot = [f"p{p['page']} {p['rotation']}deg" for p in pages if p["rotation"]]
                    sk = [f"p{p['page']} {p['skew']:+g}deg" for p in pages if abs(p.get("skew", 0)) >= 0.5]
                    self.attempts[-1]["detail"] = (f"{len(pages)} page(s) OCR'd" + (f"; rotated {', '.join(rot)}" if rot else "")
                                                   + (f"; deskewed {', '.join(sk)}" if sk else ""))
                    self.ocr_state["pages"] = pages
        return self.ocr_state["pages"]

    def ocr_text(self, mode: str | None = None) -> str:
        pages = self.ocr_pages() or []
        if mode:
            out = []
            for p in pages:
                try:
                    out.append(p["texts"][mode])
                except Exception:  # one failed OCR pass drops out of the vote; it never fails the read
                    pass
            return "\n".join(out)
        return "\n".join(t for p in pages for _, t in p["texts"].items())

    def model_candidate(self, source, raw):
        corroborate = self.ocr_text() if self.digital and self.scanned else ""
        c = X._candidate(source, raw, self.text, self.file_hash, self.gtext, corroborate)
        if self.scanned and self.ocr_pages():
            X._verify_by_ocr(c.invoice, self.ocr_text())
        if self.scanned and not self.digital and self.ocr_pages():
            x = X.crosscheck(c.invoice, self.ocr_text(), c.derived)
            c.grounding["ocr_crosscheck"] = x
            if x["disagreed"]:
                c.warnings.append(f"OCR cross-check: {x['agreed']}/{x['checked']} amounts also read by Tesseract; "
                                  f"not confirmed: {', '.join(x['disagreed'])}. Compare with the scan.")
        return c

    def replay(self):
        hit = X._cache_read(self.file_hash)
        if not hit or not isinstance(hit.get("raw"), dict):
            return False, "no cache entry for this file hash", None
        c = self.model_candidate("replay", hit["raw"])
        c.warnings.insert(0, f"Extraction replayed from cache (recorded {hit.get('saved_at', 'earlier')}); "
                             "live extraction was not used.")
        return True, f"cache hit, recorded {hit.get('saved_at')}", c

    def local_text(self):
        c = X._candidate("local_text", X.local.parse_text(self.gtext), self.gtext, self.file_hash)
        detail = "grounded" if c.clean else "; ".join(X._problems(c.grounding))
        if self.scanned:
            detail += f"; page(s) {', '.join(map(str, self.scanned))} not read (no OCR engine)"
        return c.invoice.readable and c.clean, detail, c if c.invoice.readable else None

    def local_ocr(self):
        if not X.local.tesseract_available():
            return False, "Tesseract not installed", None
        if not self.ocr_pages():
            return False, "OCR produced no pages", None
        tried = []
        for m in X.local.OCR_MODES:  # every mode reads; the first whose reading reconciles supplies the amounts
            merged = "\n".join([self.gtext, self.ocr_text(m)]).strip()
            c = X._candidate("local_ocr", X.local.parse_text(merged, ocr=True), merged, self.file_hash)
            c.grounding = X.ground(c.invoice, "", set())  # OCR text is not an independent text layer
            c.grounding["ocr_mode"] = m
            c.warnings.insert(0, f"Page(s) {', '.join(map(str, self.scanned))} read by OCR ({m}); amounts checked by "
                                 "arithmetic only.")
            tried.append(c)
        readable = [c for c in tried if c.invoice.readable]
        best = next((c for c in readable if c.grounding["arithmetic_ok"]), readable[0] if readable else None)
        if not best:
            return False, "nothing readable", None
        X._vote_identifiers(best, readable)
        X._fill_dates(best, readable)
        ok = best.grounding["arithmetic_ok"]
        return ok, (f"arithmetic reconciles ({best.grounding['ocr_mode']})" if ok
                    else "; ".join(best.grounding["arithmetic_problems"])), best

    @property
    def cache_first(self) -> bool:
        return self.auto and os.environ.get("COCKPIT_CACHE_FIRST", "1") != "0"

    @property
    def local_first(self) -> bool:
        return self.auto and bool(self.digital) and not self.scanned and os.environ.get("COCKPIT_LOCAL_FIRST", "1") != "0"


class ExtractState(TypedDict, total=False):
    job: Job
    current: Any    # the model reading under consideration (_Candidate)
    result: Any     # ExtractionResult, once a route passes; ends the graph


# --- nodes ------------------------------------------------------------------------------------------------------

def text_layer(s: ExtractState) -> dict:
    """Classify every page (digital / scanned / garbled) and strip hidden text before anything reads it."""
    j = s["job"]
    j.pinfo = j.step("text_layer", lambda: (True, "", X.local.page_texts(j.pdf))) or []
    j.digital = [p for p in j.pinfo if p["class"] == "digital"]
    j.scanned = [p["page"] for p in j.pinfo if p["class"] != "digital"]
    j.text = "\n".join(p["text"] for p in j.pinfo)       # every visible, readable character: for parsing
    j.gtext = "\n".join(p["text"] for p in j.digital)    # digital pages only: the grounding reference
    j.attempts[-1]["detail"] = (f"{len(j.pinfo)} page(s): " + ", ".join(f"p{p['page']} {p['class']}" for p in j.pinfo)
                                + f"; {len(j.text.strip())} characters")
    j.attempts[-1]["pages"] = [{k: p[k] for k in ("page", "class", "metrics", "hidden")} for p in j.pinfo]
    for p in j.pinfo:
        for why, txt in p["hidden"].items():
            j.warnings.append(f"Hidden text on page {p['page']} ({why}) was removed before reading: {txt[:100]!r}")
    garbled = [p["page"] for p in j.pinfo if p["class"] == "garbled"]
    if garbled:
        j.warnings.append(f"Page(s) {', '.join(map(str, garbled))}: the text layer is not real text (unmapped font); "
                          "treated as scanned.")
    return {}


def replay_forced(s: ExtractState) -> dict:
    """COCKPIT_EXTRACTION=replay: use the recorded reading for these bytes (offline demos)."""
    j = s["job"]
    c = j.step("replay_forced", j.replay)
    return {"result": j.done(c)} if c else {}


def cache(s: ExtractState) -> dict:
    """The same bytes were already read by the model: reuse that reading, re-checked against this PDF."""
    j = s["job"]
    hit = X._cache_read(j.file_hash)
    if not (hit and hit.get("prompt_version") == X.prompt_version() and isinstance(hit.get("raw"), dict)):
        return {}

    def reuse():
        c = j.model_candidate("cache", hit["raw"])
        return c.clean, "earlier model reading of identical bytes; " + (
            "re-grounded" if c.clean else "; ".join(X._problems(c.grounding))), c
    c = j.step("cache", reuse)
    if c and c.clean:
        return {"result": j.done(c, notes=[f"Same file was read by {hit.get('model')} on {hit.get('saved_at')}; that "
                                           "reading was reused and re-checked against this PDF (no API call)."])}
    return {}


def local_first(s: ExtractState) -> dict:
    """A fully digital PDF whose local reading is complete, grounded and reconciles needs no model call. A new
    vendor (unknown template) always gets the model read, so every company-specific field is captured."""
    j = s["job"]
    j.local_first_tried = True
    r = j.local_first_result = j.step("local_text", j.local_text)
    if (r and r.clean and X._complete(r.invoice)
            and (j.known_vendor is None or j.known_vendor(r.invoice))):
        return {"result": j.done(r, notes=["Read by the local parser: every required field present, every amount "
                                           "found in the PDF text, arithmetic reconciles. No model call needed."])}
    return {}


def llm(s: ExtractState) -> dict:
    """Claude or OpenAI (whichever key is configured): native PDF input, strict JSON schema, one retry inside."""
    j = s["job"]
    j.name, j.llm = X.provider()
    if j.mode == "local":
        j.attempts.append({"step": j.name or "llm", "ok": False, "detail": "skipped: COCKPIT_EXTRACTION=local", "ms": 0})
        j.llm_skipped = True
        return {}
    if j.llm is None:
        j.attempts.append({"step": "llm", "ok": False, "detail": "skipped: no ANTHROPIC_API_KEY or OPENAI_API_KEY",
                           "ms": 0})
        j.warnings.append("No API key: local extractor used.")
        j.llm_skipped = True
        return {}
    j.client = j.step(f"{j.name}_client", lambda: (True, "", j.llm.make_client()))
    if not j.client:
        return {"current": None}

    def first():
        raw, note, n = j.llm.call(j.client, j.pdf)
        c = j.model_candidate(j.name, raw)
        return c.clean, f"{note}; {n} call(s); " + ("grounded" if c.clean else "; ".join(X._problems(c.grounding))), c
    return {"current": j.step(j.name, first)}


def llm_repair(s: ExtractState) -> dict:
    """One repair call: the model is told which amounts are not on the page or do not add up."""
    j, c = s["job"], s["current"]

    def repair():
        raw, note, _ = j.llm.call(j.client, j.pdf, j.llm.repair_instruction(c.raw, X._problems(c.grounding)), 0)
        r = j.model_candidate(j.name, raw)
        return r.clean, f"{note}; " + ("grounded" if r.clean else "; ".join(X._problems(r.grounding))), r
    r = j.step(f"{j.name}_repair", repair)
    if r and (r.clean or not r.grounding["ungrounded"]):
        c = r
    if not c.clean:
        j.candidates.append(c)
    return {"current": c}


def llm_accept(s: ExtractState) -> dict:
    j, c = s["job"], s["current"]
    err = X._cache_write(j.file_hash, c.raw, j.name, j.llm.model() if j.name == "openai" else j.llm.MODEL)
    return {"result": j.done(c, [err] if err else [])}


def llm_fallthrough(s: ExtractState) -> dict:
    j = s["job"]
    if j.client:
        j.warnings.append(f"{X.PROVIDER_LABEL[j.name]} extraction did not pass ({j.attempts[-1]['detail']}); "
                          "falling back.")
    return {}


def local_fallback(s: ExtractState) -> dict:
    """Scanned pages: Tesseract (several layouts, identifiers by majority vote). Digital: the local text parser."""
    j = s["job"]
    if j.scanned:
        c = j.step("local_ocr", j.local_ocr)
        if j.attempts[-1]["detail"] == "Tesseract not installed":
            j.warnings.append("Scanned page and no OCR engine available (Tesseract not installed).")
            c = j.step("local_text", j.local_text) if j.digital else None
    else:
        c = j.local_first_result if j.local_first_tried else j.step("local_text", j.local_text)
    if c and (c.clean or c.source == "local_ocr" and c.grounding["arithmetic_ok"]):
        return {"result": j.done(c)}
    if c:
        j.candidates.append(c)
    return {}


def replay(s: ExtractState) -> dict:
    j = s["job"]
    r = j.step("replay", j.replay)
    return {"result": j.done(r)} if r else {}


def settle(s: ExtractState) -> dict:
    """Nothing passed cleanly. A reading whose every amount is in the document but whose arithmetic fails is the
    document's own math: pass it on and let R03 decide. Ungrounded amounts never reach the rules (fail closed)."""
    j = s["job"]
    grounded = [c for c in j.candidates if not c.grounding["ungrounded"]]
    if grounded:
        return {"result": j.done(grounded[0], ["Totals do not reconcile on the document; the rules will decide."])}
    if j.candidates:
        c = j.candidates[0]
        c.invoice.readable = False
        return {"result": j.done(c, [f"Extracted amounts not found in the document text "
                                     f"({', '.join(c.grounding['ungrounded'])}); treated as unreadable (fail closed)."])}
    j.attempts.append({"step": "none", "ok": False, "detail": "every extraction route failed", "ms": 0})
    inv = Invoice(None, None, None, None, None, file_hash=j.file_hash, readable=False)
    g = {"checked": 0, "grounded": 0, "ungrounded": [], "arithmetic_ok": False,
         "arithmetic_problems": ["nothing extracted"], "text_layer": bool(j.digital)}
    return {"result": X.ExtractionResult(inv, "none", g, j.warnings + ["Document could not be read by any extraction "
                                                                       "route."], j.attempts, {})}


# --- edges ------------------------------------------------------------------------------------------------------

def _after_text(s):
    j = s["job"]
    if j.mode == "replay":
        return "replay_forced"
    return "cache" if j.cache_first else _after_cache(s)


def _after_cache(s):
    if s.get("result"):
        return END
    return "local_first" if s["job"].local_first else "llm"


def _after_llm(s):
    j, c = s["job"], s.get("current")
    if j.llm_skipped:
        return "local_fallback"
    if c is None:
        return "llm_fallthrough"
    return "llm_accept" if c.clean else "llm_repair"


def _end_or(nxt):
    return lambda s: END if s.get("result") else nxt


def build():
    g = StateGraph(ExtractState)
    for fn in (text_layer, replay_forced, cache, local_first, llm, llm_repair, llm_accept, llm_fallthrough,
               local_fallback, replay, settle):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "text_layer")
    g.add_conditional_edges("text_layer", _after_text, ["replay_forced", "cache", "local_first", "llm"])
    g.add_conditional_edges("replay_forced", _end_or("llm"), ["llm", END])
    g.add_conditional_edges("cache", _after_cache, ["local_first", "llm", END])
    g.add_conditional_edges("local_first", _end_or("llm"), ["llm", END])
    g.add_conditional_edges("llm", _after_llm, ["local_fallback", "llm_fallthrough", "llm_accept", "llm_repair"])
    g.add_conditional_edges("llm_repair", lambda s: "llm_accept" if s["current"].clean else "llm_fallthrough",
                            ["llm_accept", "llm_fallthrough"])
    g.add_edge("llm_accept", END)
    g.add_edge("llm_fallthrough", "local_fallback")
    g.add_conditional_edges("local_fallback",
                            lambda s: END if s.get("result") else ("settle" if s["job"].mode == "replay" else "replay"),
                            ["replay", "settle", END])
    g.add_conditional_edges("replay", _end_or("settle"), ["settle", END])
    g.add_edge("settle", END)
    return g.compile(checkpointer=False, name="extraction")


graph = build()


def run(pdf_bytes: bytes, file_hash: str, filename: str = "", known_vendor=None) -> X.ExtractionResult:
    return graph.invoke({"job": Job(pdf_bytes, file_hash, filename, known_vendor)})["result"]
