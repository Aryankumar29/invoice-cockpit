"""Mail intake as a LangGraph graph: one email in, invoice runs out, suspicious mail held for a person.

    seen --already ingested--> END (idempotent on Message-ID)
      `--new--> triage --> file_attachments (keep documents; submit the invoices) --> record --held--> alert --> END

Triage (cockpit/mail/triage.py) is deterministic: look-alike sender domains, bank-change requests, link-only
"invoices" and unclear mail are held before any invoice run starts. Invoice attachments are submitted one after
another, in attachment order, so the ledger sees them in a fixed order."""
from __future__ import annotations

import hashlib
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..mail.message import MailMessage
from ..mail.triage import KIND_LABEL, triage as classify

# label -> mail status: processed = attachments sent to the pipeline; held = waiting for a person;
# archived = not an invoice; released / dismissed = a person decided.
STATUS = {"invoice": "processed", "suspicious": "held", "unsure": "held", "not_invoice": "archived"}


class MailState(TypedDict, total=False):
    msg: MailMessage
    existing: dict | None
    triage: Any
    attachments: list[dict]
    row: dict


def build(intake):
    """`intake` is the MailIntake: its store, records source, submit callback, attachment folder and alert hook."""

    def seen(s: MailState) -> dict:
        return {"existing": intake.store.mail(s["msg"].message_id)}

    def triage(s: MailState) -> dict:
        return {"triage": classify(s["msg"], intake.records())}

    def file_attachments(s: MailState) -> dict:
        """Store each document attachment; send the ones triage identified as invoices into the pipeline."""
        msg, t = s["msg"], s["triage"]
        to_run = {d.filename for d in t.invoice_docs} if t.label == "invoice" else set()
        atts = []
        for a in msg.attachments:
            if not (a.is_document or a.is_risky):
                continue
            sha = hashlib.sha256(a.data).hexdigest()
            if not a.is_risky:
                (intake.dir / f"{sha}.bin").write_bytes(a.data)
            doc = next((d for d in t.docs if d.filename == a.filename), None)
            row = {"filename": a.filename, "content_type": a.content_type, "size": len(a.data), "sha256": sha,
                   "kind": doc.kind if doc else "other", "kind_label": KIND_LABEL.get(doc.kind if doc else "other"),
                   "run_id": None, "existing": False}
            if a.filename in to_run:
                res = intake.submit(a.filename, a.data)
                row["run_id"], row["existing"] = res["run_id"], res["existing"]
            atts.append(row)
        if t.label == "invoice" and any(a["existing"] for a in atts):
            t.reasons.append("The same file was already processed; linked to the earlier run instead of a new one.")
        return {"attachments": atts, "triage": t}

    def record(s: MailState) -> dict:
        msg, t, atts = s["msg"], s["triage"], s["attachments"]
        mid = intake.store.add_mail({
            "message_id": msg.message_id, "connector": msg.connector, "received_at": msg.received_at,
            "sender": msg.sender, "sender_name": msg.sender_name, "subject": msg.subject or "(no subject)",
            "snippet": " ".join(msg.text.split())[:240], "label": t.label, "status": STATUS[t.label],
            "reasons": t.reasons, "keys": t.keys, "signals": t.signals, "vendor_id": t.vendor_id,
            "attachments": atts, "run_ids": [a["run_id"] for a in atts if a["run_id"]]})
        return {"row": intake.store.mail(mid)}

    def alert(s: MailState) -> dict:
        try:
            intake.on_held(s["row"])
        except Exception:  # an alert problem never changes what happened to the email
            pass
        return {}

    g = StateGraph(MailState)
    for fn in (seen, triage, file_attachments, record, alert):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "seen")
    g.add_conditional_edges("seen", lambda s: END if s["existing"] else "triage", ["triage", END])
    g.add_edge("triage", "file_attachments")
    g.add_edge("file_attachments", "record")
    g.add_conditional_edges("record", lambda s: "alert" if s["row"]["status"] == "held" and intake.on_held else END,
                            ["alert", END])
    g.add_edge("alert", END)
    return g.compile(checkpointer=False, name="mail")
