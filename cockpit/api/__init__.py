"""HTTP API (docs/CONTRACT.md). One process: FastAPI + the DB-backed worker + the built web UI."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import issues as run_issues
from .. import sources, usage
from ..core import policy as P
from ..core import seed
from ..core.engine import _jsonable
from ..core.rules import money
from ..mail import MailboxError, MailIntake
from ..mail import send as mail_send
from ..mail import status as mailbox_status
from ..notify import DigestScheduler, Notifier, alerts_enabled, vendor_draft
from ..graph.invoice import REVIEW_ACTIONS
from ..pipeline import NotWaitingForReview, Pipeline, Worker
from ..settings import SERVICES, Settings
from ..store import SUMMARY_COLS, Store

ROOT = Path(__file__).resolve().parents[2]


class ActionIn(BaseModel):
    action: str
    reviewer: str = ""
    note: str = ""


class MailDecisionIn(BaseModel):
    reviewer: str = ""
    note: str = ""


class SyncIn(BaseModel):
    connector: str = "folder"


class ConnectIn(BaseModel):
    values: dict[str, str] = {}


class SourceIn(BaseModel):
    kind: str


class VendorEmailIn(BaseModel):
    reviewer: str = ""
    subject: str = ""
    body: str = ""


def _fixtures_dir() -> Path:
    return Path(os.environ.get("COCKPIT_FIXTURES_DIR", ROOT / "fixtures"))


def load_manifest() -> list[dict]:
    path = _fixtures_dir() / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def create_app(data_dir: str | os.PathLike | None = None, pipeline: Pipeline | None = None,
               start_worker: bool = True) -> FastAPI:
    store = pipeline.store if pipeline else Store(data_dir or os.environ.get("COCKPIT_DATA_DIR", ROOT / "data"))
    settings = Settings(store.dir)
    pipe = pipeline or Pipeline(store)
    worker = Worker(pipe)
    usage.set_sink(store.add_usage)
    notifier = Notifier(store)
    scheduler = DigestScheduler(notifier, store.usage_stats)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            worker.start()
            scheduler.start()
        yield
        if start_worker:
            worker.stop()
            scheduler.stop()

    app = FastAPI(title="Invoice Cockpit", version=P.POLICY_VERSION, lifespan=lifespan)
    app.state.store, app.state.pipeline, app.state.worker = store, pipe, worker
    app.state.notifier, app.state.scheduler, app.state.settings = notifier, scheduler, settings

    def submit(filename: str, data: bytes, fixture_key: str | None = None) -> dict:
        rid, existing = store.create_run(filename, hashlib.sha256(data).hexdigest(), data, fixture_key)
        worker.wake()
        return {"run_id": rid, "existing": existing}

    intake = MailIntake(store, lambda name, data: submit(name, data), on_held=notifier.mail_alert)
    app.state.intake = intake

    def on_run_finished(rid: str) -> None:
        run = store.run(rid)
        if run is not None:
            notifier.run_alert(detail(run), store.mail_for_run(rid))

    pipe.listeners.append(on_run_finished)

    def records_or_503():
        try:
            return sources.records()
        except sources.BookError as e:
            raise HTTPException(503, str(e))

    def vendor_contact(out: dict) -> tuple[dict | None, str]:
        try:
            rec = sources.records()
        except sources.BookError:
            return None, sources.BUYER
        vm = out.get("vendor_match") or {}
        vid = vm.get("vendor") if isinstance(vm.get("vendor"), str) else None  # the packet stores the vendor id
        if not vid and out.get("vendor_name"):
            vid = next((v.id for v in rec.vendors if v.name.casefold() == out["vendor_name"].casefold()), None)
        buyer = rec.buyers.get(out.get("po_number") or "") or next(iter(set(rec.buyers.values())), sources.BUYER)
        return (rec.contacts.get(vid) if vid else None), buyer

    def get_run(rid: str) -> dict:
        run = store.run(rid)
        if run is None:
            raise HTTPException(404, f"Run {rid} not found")
        return run

    def review_state(rid: str) -> dict:
        """Whether the run's graph thread is paused at human_review (cockpit/graph/invoice.py)."""
        ask = pipe.waiting_for_review(rid)
        return {"waiting": ask is not None, "approvals_given": (ask or {}).get("approvals_given", 0),
                "dual_control": bool((ask or {}).get("dual_control"))}

    def detail(run: dict) -> dict:
        d = run["detail"] or {}
        actions = store.actions(run["id"])
        out = {k: run[k] for k in SUMMARY_COLS}
        out |= {k: d.get(k) for k in ("verdict", "recommended_action", "explanation", "explain_source", "rules",
                                      "invoice", "line_matches", "allocations", "duplicates", "vendor_match",
                                      "po_match", "grounding", "warnings", "attempts", "packet_hash",
                                      "policy_version", "dual_control")}
        for k, default in (("rules", []), ("line_matches", []), ("allocations", []), ("warnings", []),
                           ("verdict", ""), ("recommended_action", ""), ("explanation", ""), ("packet_hash", ""),
                           ("policy_version", P.POLICY_VERSION)):
            if out[k] is None:
                out[k] = default
        out |= {"stages": store.events(run["id"]), "actions": actions, "error": run["error"],
                "investigation": store.investigation(run["id"]), "mail": _mail_brief(store.mail_for_run(run["id"])),
                "issues": run_issues.for_run(run),
                "fixture_key": run["fixture_key"], "allowed_actions": [], "approvals": None,
                "review_state": review_state(run["id"])}
        if run["outcome"] == "needs_review" and out["review_state"]["waiting"]:
            approvals = [a for a in actions if a["action"] == "approve"]
            if d.get("dual_control"):
                out["approvals"] = {"required": 2, "given": len(approvals),
                                    "reviewers": [a["reviewer"] for a in approvals]}
            out["allowed_actions"] = list(REVIEW_ACTIONS)
        return out

    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        from ..extraction import provider
        name, llm = provider()
        mode = name if name and os.environ.get("COCKPIT_EXTRACTION") != "local" else "local"
        model = (llm.model() if name == "openai" else llm.MODEL) if llm else None
        return {"ok": True, "extraction_mode": mode, "api_key_present": bool(name), "llm_model": model,
                "policy_version": P.POLICY_VERSION, "db": "sqlite-wal", "today": pipe.today.isoformat(),
                "po_source": sources.configured()[0],
                "fixtures": len(load_manifest())}

    @app.get("/api/policy")
    def policy():
        """The limits the 16 checks use, straight from cockpit/core/policy.py (shown on the How it works tab)."""
        return {"version": P.POLICY_VERSION, "values": P.snapshot(),
                "consuming_outcomes": sorted(P.CONSUMING_OUTCOMES)}

    @app.get("/api/connections")
    def connections():
        """What the cockpit is connected to: PO source, mailboxes, Slack, digest schedule."""
        try:
            po = dict(sources.records().source)
        except sources.BookError as e:
            kind, loc = sources.configured()
            po = {"kind": kind, "location": loc, "error": str(e), "issues": []}
        at = DigestScheduler.at()
        sheet = os.environ.get("GOOGLE_SHEET_ID", "").strip()
        po["open_url"] = (f"https://docs.google.com/spreadsheets/d/{sheet}/edit" if po.get("kind") == "sheets" and sheet
                          else "/api/records/book" if po.get("kind") == "file" else None)
        po["options"] = source_options()
        return {"po_source": po, "mailboxes": mailbox_status(), "send_via": mail_send.available(),
                "fields": settings.describe(),
                "slack": {"configured": notifier.slack.configured, "alerts": sorted(alerts_enabled()),
                          "digest_time": f"{at[0]:02d}:{at[1]:02d}" if at else None,
                          "digest_last": store.kv_get("digest_last")}}

    def check_service(service: str) -> str:
        """Prove a connection works. Nothing is changed in the mailbox or the sheet."""
        if service in ("gmail", "outlook"):
            return mail_send.test(service)
        if service == "slack":
            if not notifier.slack.configured:
                raise MailboxError("Add a Slack incoming webhook URL (it starts with https://hooks.slack.com/).")
            row = notifier.slack.post("test", "Invoice Cockpit is connected",
                                      "Alerts and the daily summary will arrive in this channel.")
            if row["status"] != "sent":
                raise MailboxError(row["error"] or "Slack did not accept the message.")
            return "Test message posted to Slack."
        if service == "sheets":
            rec = sources.reload()
            if rec.source.get("kind") != "sheets":
                raise MailboxError("No Google Sheet is connected; the PO book is read from "
                                   f"{rec.source.get('location')}.")
            c = rec.source.get("counts") or {}
            bad = len(rec.source.get("issues") or [])
            return (f"Read {c.get('vendors', 0)} vendors and {c.get('purchase_orders', 0)} POs from the sheet"
                    + (f"; {bad} row(s) need fixing." if bad else "."))
        if service == "folder":
            from ..mail.connectors import FolderConnector
            return f"{len(FolderConnector().fetch())} email(s) in the demo folder."
        raise HTTPException(404, f"Unknown connection {service}")

    @app.post("/api/connections/{service}")
    def connect(service: str, body: ConnectIn):
        if service not in SERVICES:
            raise HTTPException(404, f"Unknown connection {service}")
        try:
            settings.save(service, body.values)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            result = {"ok": True, "message": check_service(service)}
        except (MailboxError, sources.BookError) as e:
            result = {"ok": False, "message": str(e)}
        return result | {"connections": connections()}

    @app.post("/api/connections/{service}/test")
    def connection_test(service: str):
        try:
            return {"ok": True, "message": check_service(service)}
        except (MailboxError, sources.BookError) as e:
            return {"ok": False, "message": str(e)}

    @app.delete("/api/connections/{service}")
    def disconnect(service: str):
        if service not in SERVICES:
            raise HTTPException(404, f"Unknown connection {service}")
        settings.disconnect(service)
        if service == "sheets":
            try:
                sources.reload()
            except sources.BookError:
                pass
        return {"ok": True, "connections": connections()}

    uploaded_book = store.dir / "po_book_uploaded.xlsx"

    def source_options() -> dict:
        """The three ways to load purchase orders, and which one is in use."""
        kind, loc = sources.configured()
        sheet = os.environ.get("GOOGLE_SHEET_ID", "").strip()
        in_use = ("sheets" if kind == "sheets" else "uploaded" if kind == "file" and Path(loc) == uploaded_book
                  else "sample" if kind == "file" else "builtin")
        return {"in_use": in_use,
                "uploaded": {"exists": uploaded_book.is_file(),
                             "filename": store.kv_get("po_book_filename"), "uploaded_at": store.kv_get("po_book_at")},
                "sheets": {"connected": bool(sheet), "url": f"https://docs.google.com/spreadsheets/d/{sheet}/edit"
                           if sheet else None},
                "sample": {"exists": sources.DEFAULT_BOOK.is_file()}}

    @app.post("/api/records/upload")
    async def records_upload(file: UploadFile = File(...)):
        """Replace the PO book with an uploaded Excel workbook. It is checked before it is used."""
        import io as _io
        from datetime import datetime, timezone
        from ..sources import book as _book
        from ..sources.files import read_xlsx
        data = await file.read()
        name = file.filename or "po_book.xlsx"
        if not name.lower().endswith(".xlsx"):
            raise HTTPException(400, "Upload an Excel workbook (.xlsx). In Google Sheets: File > Download > "
                                     "Microsoft Excel (.xlsx).")
        tmp = store.dir / "po_book_upload.tmp.xlsx"
        tmp.write_bytes(data)
        try:
            rec = _book.parse(read_xlsx(tmp))
        except sources.BookError as e:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, str(e))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "This file could not be opened as an Excel workbook.")
        if not rec.vendors or not rec.pos:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "No usable vendors or purchase orders were found. "
                                     + (" ".join(rec.source.get("issues", [])[:3]) or "Fill in the three tabs."))
        tmp.replace(uploaded_book)
        store.kv_set("po_book_filename", name)
        store.kv_set("po_book_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        settings.set_vars({"COCKPIT_PO_SOURCE": "file", "COCKPIT_PO_BOOK": str(uploaded_book)})
        loaded = sources.reload()
        issues = loaded.source.get("issues", [])
        return {"ok": True, "message": f"Loaded {len(loaded.vendors)} vendors and {len(loaded.pos)} purchase orders "
                                       f"from {name}." + (f" {len(issues)} row(s) were skipped; see below." if issues else ""),
                "issues": issues, "connections": connections()}

    @app.post("/api/records/source")
    def records_source(body: SourceIn):
        """Choose which PO book is in use: the uploaded file, the connected Google Sheet, or the sample book."""
        if body.kind == "uploaded":
            if not uploaded_book.is_file():
                raise HTTPException(409, "No spreadsheet has been uploaded yet.")
            settings.set_vars({"COCKPIT_PO_SOURCE": "file", "COCKPIT_PO_BOOK": str(uploaded_book)})
        elif body.kind == "sheets":
            if not os.environ.get("GOOGLE_SHEET_ID"):
                raise HTTPException(409, "Connect a Google Sheet first.")
            settings.set_vars({"COCKPIT_PO_SOURCE": "sheets", "COCKPIT_PO_BOOK": None})
        elif body.kind == "sample":
            settings.set_vars({"COCKPIT_PO_SOURCE": "file", "COCKPIT_PO_BOOK": str(sources.DEFAULT_BOOK)})
        else:
            raise HTTPException(400, "Choose uploaded, sheets or sample.")
        try:
            sources.reload()
        except sources.BookError as e:
            return {"ok": False, "message": str(e), "connections": connections()}
        return {"ok": True, "message": "Purchase orders are now read from the "
                + {"uploaded": "uploaded spreadsheet", "sheets": "Google Sheet", "sample": "sample spreadsheet"}[body.kind]
                + ".", "connections": connections()}

    @app.get("/api/records/template")
    def records_template(sample: bool = False):
        """A spreadsheet to fill in: blank (one example row per tab) or the sample book with the demo data."""
        if sample and sources.DEFAULT_BOOK.is_file():
            return FileResponse(sources.DEFAULT_BOOK, filename="po_book_sample.xlsx",
                                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        from ..sources.files import blank_tables, write_xlsx
        out = store.dir / "po_book_template.xlsx"
        write_xlsx(out, blank_tables())
        return FileResponse(out, filename="po_book_template.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/records/book")
    def records_book():
        """The spreadsheet the PO data comes from: the workbook itself, or a link to the Google Sheet."""
        kind, loc = sources.configured()
        if kind == "sheets":
            from fastapi.responses import RedirectResponse
            return RedirectResponse(f"https://docs.google.com/spreadsheets/d/{loc}/edit")
        path = Path(loc) if kind == "file" else None
        if path is None or not path.is_file():
            raise HTTPException(404, "The PO data is not read from a spreadsheet file.")
        return FileResponse(path, filename=path.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/records")
    def records():
        rec = records_or_503()
        names = {v.id: v.name for v in rec.vendors}
        pos = [{"po_number": p.po_number, "vendor_id": p.vendor_id, "vendor_name": names.get(p.vendor_id),
                "buyer_company": rec.buyers.get(p.po_number), "currency": p.currency, "status": p.status,
                "issued_on": p.issued.isoformat() if p.issued else None, "tax_rate_bp": p.tax_rate_bp,
                "total_cents": p.total_cents, "lines": _jsonable(p.lines)} for p in rec.pos.values()]
        vendors = [{"vendor_id": v.id, "vendor_name": v.name, "tax_id": v.tax_id, "status": v.status,
                    "bank_on_file": bool(v.bank_hash), **(rec.contacts.get(v.id) or {})} for v in rec.vendors]
        return {"source": rec.source, "vendors": vendors, "purchase_orders": pos}

    @app.get("/api/records/sheet")
    def records_sheet():
        """Every tab of the PO book with every template column, row numbers as in the sheet, and the reason any
        row was skipped. Bank accounts are masked to their last four digits."""
        import re as _re
        from ..sources.book import TABS
        rec = records_or_503()
        problems: dict[tuple[str, int], str] = {}
        for msg in rec.source.get("issues", []):
            m = _re.match(r"(\w+) row (\d+): (.*)", msg)
            if m:
                problems[(m.group(1), int(m.group(2)))] = m.group(3)
        def amount(cents: int | None) -> str:
            return f"{cents / 100:,.2f}" if cents is not None else ""

        # Amounts are calculated from the sheet (quantity x unit price), never typed, so they cannot disagree.
        lines = {(no, l.line_no): l.amount_cents for no, po in rec.pos.items() for l in po.lines}
        computed = {"POLines": ("unit_price", "line_amount", lambda r: amount(lines.get((r.get("po_number", ""), _int(r.get("line_no")))))),
                    "PurchaseOrders": ("currency", "po_total", lambda r: amount(rec.pos[r["po_number"]].total_cents)
                                       if r.get("po_number") in rec.pos else "")}
        tabs = []
        for tab, base in TABS.items():
            rows = rec.tables.get(tab, [])
            calc = computed.get(tab)
            cols = list(base)
            if calc:
                cols.insert(cols.index(calc[0]) + 1, calc[1])
            extra = [c for c in (rows[0].keys() if rows else []) if c not in cols]
            out = []
            for i, r in enumerate(rows, start=2):
                if not any(r.values()):
                    continue
                cells = [calc[2](r) if calc and c == calc[1] else r.get(c, "") for c in cols + extra]
                if "bank_account" in cols:
                    j = cols.index("bank_account")
                    digits = "".join(ch for ch in cells[j] if ch.isdigit())
                    cells[j] = f"****{digits[-4:]}" if digits else ""
                out.append({"row": i, "cells": cells, "problem": problems.get((tab, i))})
            tabs.append({"name": tab, "columns": cols + extra, "rows": out, "computed": [calc[1]] if calc else []})
        return {"source": rec.source, "tabs": tabs}

    @app.post("/api/records/reload")
    def records_reload():
        try:
            sources.reload()
        except sources.BookError as e:
            raise HTTPException(503, str(e))
        return records()

    # mail intake ------------------------------------------------------------
    @app.get("/api/mail")
    def mail_list(label: str | None = None):
        return {"mails": store.mails(label), "stats": store.mail_stats(), "mailboxes": mailbox_status()}

    @app.post("/api/mail/sync")
    def mail_sync(body: SyncIn | None = None):
        records_or_503()
        try:
            return intake.sync((body or SyncIn()).connector)
        except MailboxError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/mail/upload")
    async def mail_upload(file: UploadFile = File(...)):
        data = await file.read()
        if not data:
            raise HTTPException(400, "Empty file")
        if not (file.filename or "").lower().endswith(".eml"):
            raise HTTPException(400, "Upload an email saved as .eml (Outlook or Gmail: save or download the message).")
        records_or_503()
        row, new = intake.ingest_raw(data, "upload")
        return row | {"new": new}

    @app.get("/api/mail/{mid}")
    def mail_get(mid: str):
        row = store.mail(mid)
        if row is None:
            raise HTTPException(404, f"Email {mid} not found")
        return row

    def mail_decide(mid: str, body: MailDecisionIn, fn):
        reviewer, note = body.reviewer.strip(), body.note.strip()
        if not reviewer or not note:
            raise HTTPException(400, "Reviewer name and a note are required")
        try:
            return fn(mid, reviewer, note)
        except KeyError:
            raise HTTPException(404, f"Email {mid} not found")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/mail/{mid}/release")
    def mail_release(mid: str, body: MailDecisionIn):
        return mail_decide(mid, body, intake.release)

    @app.post("/api/mail/{mid}/dismiss")
    def mail_dismiss(mid: str, body: MailDecisionIn):
        return mail_decide(mid, body, intake.dismiss)

    # notifications ------------------------------------------------------------
    @app.get("/api/notifications")
    def notifications(limit: int = Query(50, ge=1, le=500)):
        return {"slack_configured": notifier.slack.configured, "items": store.notifications(limit)}

    @app.get("/api/notify/digest/preview")
    def digest_preview():
        return notifier.build_digest(usage=store.usage_stats())

    @app.post("/api/notify/digest")
    def digest_send():
        return notifier.send_digest(usage=store.usage_stats())

    def draft_for(rid: str) -> dict:
        out = detail(get_run(rid))
        contact, buyer = vendor_contact(out)
        d = vendor_draft(out, contact, buyer) or {"available": False, "internal_notes": [],
                                                  "why": "The invoice was approved; nothing to ask the vendor."}
        sent = [n for n in store.notifications(500) if n["run_id"] == rid and n["kind"] == "vendor_email"]
        return d | {"send_via": mail_send.available(), "sent": sent}

    @app.get("/api/runs/{rid}/vendor-draft")
    def run_vendor_draft(rid: str):
        return draft_for(rid)

    @app.post("/api/runs/{rid}/vendor-email")
    def run_vendor_email(rid: str, body: VendorEmailIn):
        """Send the reviewed draft to the vendor-master address (never to whoever emailed the invoice)."""
        run = get_run(rid)
        d = draft_for(rid)
        if not d.get("available"):
            raise HTTPException(409, d.get("why") or "Nothing to send to the vendor for this invoice.")
        if not d.get("to"):
            raise HTTPException(409, "The vendor master has no billing email for this vendor.")
        reviewer = body.reviewer.strip()
        if not reviewer:
            raise HTTPException(400, "Your name is required; it goes in the audit trail.")
        subject, text = body.subject.strip() or d["subject"], body.body.strip() or d["body"]
        try:
            res = mail_send.send(d["to"], subject, text)
        except MailboxError as e:
            store.add_notification("email", "vendor_email", subject, text, {"to": d["to"]}, "failed", rid, str(e))
            raise HTTPException(502, str(e))
        store.add_notification("email", "vendor_email", subject, text, {"to": d["to"], **res}, "sent", rid)
        before = run["outcome"]
        after = "returned" if before == "needs_review" else before
        store.add_action(rid, "email_vendor", reviewer, f"Emailed {d['to']} via {res['via']}: {subject}", before, after)
        if after != before:
            store.update_run(rid, outcome=after)
            if run["prior"]:
                store.update_run(rid, prior=run["prior"] | {"outcome": after})
        return {"sent": True, "to": d["to"], "via": res["via"], "run": detail(get_run(rid)), "draft": draft_for(rid)}

    @app.get("/api/fixtures")
    def fixtures():
        return [{k: v for k, v in f.items() if k != "extraction"} for f in load_manifest()]

    @app.post("/api/fixtures/{key}/run")
    def run_fixture(key: str):
        f = next((f for f in load_manifest() if f["key"] == key), None)
        if f is None:
            raise HTTPException(404, f"Fixture {key} not found")
        path = _fixtures_dir() / f["file"]
        return submit(path.name, path.read_bytes(), key)

    @app.post("/api/runs")
    async def upload(file: UploadFile = File(...)):
        data = await file.read()
        if not data:
            raise HTTPException(400, "Empty file")
        return submit(file.filename or "upload.pdf", data)

    @app.get("/api/runs")
    def runs(outcome: str | None = None, q: str | None = None, limit: int = Query(50, ge=1, le=500)):
        out = store.summaries(outcome, q, limit)
        for r in out:
            full = store.run(r["id"])
            r["issues"] = run_issues.for_run(full) if full else []
        return out

    @app.get("/api/runs/{rid}")
    def run_detail(rid: str):
        prior = next((h for h in seed.history() if h.run_id == rid), None)
        if prior is not None and store.run(rid) is None:
            return _seeded_detail(prior)
        return detail(get_run(rid))

    @app.get("/api/runs/{rid}/events.json")
    def events_json(rid: str, after: int = 0):
        get_run(rid)
        return store.events(rid, after)

    @app.get("/api/runs/{rid}/events")
    async def events_sse(rid: str, request: Request):
        get_run(rid)
        try:
            last = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)
        except ValueError:
            last = 0

        async def stream():
            nonlocal last
            idle = 0.0
            yield "retry: 1000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                for ev in store.events(rid, last):
                    last = ev["seq"]
                    yield f"id: {ev['seq']}\nevent: stage\ndata: {json.dumps(ev, default=str)}\n\n"
                    idle = 0.0
                run = store.run(rid)
                if run["status"] in ("done", "failed") and not store.events(rid, last):
                    payload = {"run_id": rid, "status": run["status"], "outcome": run["outcome"],
                               "reasons": run["reasons"]}
                    yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                    return
                await asyncio.sleep(0.2)
                idle += 0.2
                if idle >= 15:
                    yield ": keep-alive\n\n"
                    idle = 0.0

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{rid}/actions")
    def act(rid: str, body: ActionIn):
        run = get_run(rid)
        if body.action not in REVIEW_ACTIONS:
            raise HTTPException(400, f"Unknown action {body.action}")
        if run["outcome"] != "needs_review":
            raise HTTPException(409, f"Run is {run['outcome']}; only needs_review runs accept actions")
        reviewer, note = body.reviewer.strip(), body.note.strip()
        if not reviewer or not note:
            raise HTTPException(400, "Reviewer name and a note are required")
        try:  # the paused graph thread records the action and decides the outcome (graph/invoice.py human_review)
            pipe.review(rid, body.action, reviewer, note)
        except NotWaitingForReview:
            raise HTTPException(409, "This run is not waiting for a reviewer.")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return detail(get_run(rid))

    @app.post("/api/runs/{rid}/investigate")
    def run_investigate(rid: str):
        """Exception investigation agent (cockpit/graph/investigation.py). Read-only tools, bounded; advisory only."""
        run = get_run(rid)
        if run["status"] != "done" or not run["detail"]:
            raise HTTPException(409, "The run has not finished.")
        if run["outcome"] == "auto_approved":
            raise HTTPException(409, "Nothing to investigate: the invoice was approved automatically.")
        from ..graph.investigation import NoModel, chat_model, investigate
        try:
            model = chat_model()
        except NoModel as e:
            raise HTTPException(501, str(e))
        token = usage.run_id.set(rid)
        try:
            result = investigate(detail(run), pipe.context(exclude=rid), model=model)
        except Exception as e:
            raise HTTPException(502, f"Investigation failed: {type(e).__name__}: {str(e)[:200]}")
        finally:
            usage.run_id.reset(token)
        store.add_investigation(rid, result)
        return detail(get_run(rid))

    @app.get("/api/runs/{rid}/extraction.json")
    def run_extraction(rid: str):
        """Everything read from the document, for downstream systems: core fields, line items with their extra
        columns, every company-specific field (with verified flags), how it was read, and the decision."""
        run = get_run(rid)
        det = run["detail"] or {}
        body = {"run_id": rid, "filename": run["filename"], "file_sha256": run["file_hash"],
                "read_by": run["extraction_source"], "invoice": det.get("invoice"),
                "grounding": det.get("grounding"), "warnings": det.get("warnings", []),
                "investigation": store.investigation(rid),
                "decision": {"outcome": run["outcome"], "reasons": run["reasons"], "verdict": det.get("verdict"),
                             "policy_version": det.get("policy_version"), "packet_hash": det.get("packet_hash")}}
        return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{rid}-extraction.json"'})

    @app.get("/api/runs/{rid}/file")
    def run_file(rid: str):
        run = get_run(rid)
        return FileResponse(run["file_path"], media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{run["filename"]}"'})

    @app.get("/api/runs/{rid}/memo", response_class=HTMLResponse)
    def run_memo(rid: str):
        d = detail(get_run(rid))
        try:
            from ..explain import memo
            return memo((get_run(rid)["detail"] or {}).get("packet", {}), d)
        except ImportError:
            return _fallback_memo(d)

    @app.get("/api/stats")
    def stats():
        return store.stats() | {"api_usage": store.usage_stats()}

    @app.post("/api/reset")
    def reset():
        store.reset()
        pipe.reset()
        return {"ok": True}

    _mount_web(app)
    return app


def _int(v) -> int | None:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _mail_brief(m: dict | None) -> dict | None:
    if not m:
        return None
    return {k: m[k] for k in ("id", "sender", "sender_name", "subject", "received_at", "label", "keys", "connector")}


def _seeded_detail(h) -> dict:
    """Invoices processed before the demo (seed history) are read-only records duplicates can link to."""
    verdict = (f"Historical invoice {h.invoice_number} for {money(h.total_cents)}: approved {h.approved_on}, "
               f"paid {h.paid_on}. Seeded before the demo; no pipeline run.")
    return {"id": h.run_id, "created_at": f"{h.invoice_date}T00:00:00+00:00", "filename": f"{h.invoice_number}.pdf",
            "status": "done", "outcome": h.outcome, "reasons": [], "vendor_name": seed.vendors_by_id()[h.vendor_id].name,
            "invoice_number": h.invoice_number, "total_cents": h.total_cents, "currency": h.currency,
            "po_number": h.po_number, "extraction_source": "seed", "duration_ms": None, "verdict": verdict,
            "recommended_action": "None: already paid", "explanation": verdict, "explain_source": "template",
            "rules": [], "invoice": _jsonable(h), "line_matches": [], "allocations": _jsonable(h.allocations),
            "duplicates": None, "vendor_match": None, "po_match": None, "grounding": None, "warnings": [],
            "attempts": [], "packet_hash": "", "policy_version": P.POLICY_VERSION, "dual_control": False,
            "stages": [], "actions": [], "error": None, "fixture_key": None, "allowed_actions": [],
            "approvals": None, "seeded": True, "mail": None}


def _fallback_memo(d: dict) -> str:
    from html import escape
    rows = "".join(f"<tr><td>{escape(r['rule'])}</td><td>{escape(r['title'])}</td><td>{escape(r['status'])}</td>"
                   f"<td>{escape(r['message'])}</td></tr>" for r in d["rules"])
    return (f"<!doctype html><meta charset=utf-8><title>Decision memo {escape(d['id'])}</title>"
            f"<h1>{escape(d['outcome'])}</h1><p>{escape(d['verdict'])}</p><table>{rows}</table>"
            f"<p>Packet {escape(d['packet_hash'])} · policy {escape(d['policy_version'])}</p>")


def _mount_web(app: FastAPI) -> None:
    dist = Path(os.environ.get("COCKPIT_WEB_DIST", ROOT / "web" / "dist"))

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = dist / path
        if path and f.is_file() and dist in f.resolve().parents:
            return FileResponse(f)
        index = dist / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"message": "Invoice Cockpit API. Build the UI with `npm run build` in web/."})


def load_env_file(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE lines from invoice-cockpit/.env. A variable already set in the shell wins."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#") and v.strip().strip("'\""):
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main():
    import uvicorn
    load_env_file()
    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
