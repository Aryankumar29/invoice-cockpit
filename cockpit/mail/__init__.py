"""Mail intake: pull emails from a mailbox, triage each one (triage.py), send invoice attachments into the
pipeline, and hold suspicious or unclear emails for a person. Idempotent on Message-ID."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from .. import sources
from ..store import Store
from .connectors import MailboxError, get, status
from .message import MailMessage, parse_eml
from .triage import KIND_LABEL, triage

__all__ = ["MailIntake", "MailboxError", "parse_eml", "status"]

# label -> mail status: processed = attachments sent to the pipeline; held = waiting for a person;
# archived = not an invoice; released / dismissed = a person decided.
_STATUS = {"invoice": "processed", "suspicious": "held", "unsure": "held", "not_invoice": "archived"}


class MailIntake:
    def __init__(self, store: Store, submit: Callable[[str, bytes], dict], records=sources.records,
                 on_held: Callable[[dict], None] | None = None):
        self.store, self.submit, self.records, self.on_held = store, submit, records, on_held
        self.dir = Path(store.dir) / "mail"
        self.dir.mkdir(parents=True, exist_ok=True)

    def ingest(self, msg: MailMessage) -> tuple[dict, bool]:
        """Triage one email. Returns (mail row, is_new)."""
        existing = self.store.mail(msg.message_id)
        if existing:
            return existing, False
        t = triage(msg, self.records())
        to_run = {d.filename for d in t.invoice_docs} if t.label == "invoice" else set()
        atts = []
        for a in msg.attachments:
            if not (a.is_document or a.is_risky):
                continue
            sha = hashlib.sha256(a.data).hexdigest()
            if not a.is_risky:
                (self.dir / f"{sha}.bin").write_bytes(a.data)
            doc = next((d for d in t.docs if d.filename == a.filename), None)
            row = {"filename": a.filename, "content_type": a.content_type, "size": len(a.data), "sha256": sha,
                   "kind": doc.kind if doc else "other", "kind_label": KIND_LABEL.get(doc.kind if doc else "other"),
                   "run_id": None, "existing": False}
            if a.filename in to_run:
                res = self.submit(a.filename, a.data)
                row["run_id"], row["existing"] = res["run_id"], res["existing"]
            atts.append(row)
        if t.label == "invoice" and any(a["existing"] for a in atts):
            t.reasons.append("The same file was already processed; linked to the earlier run instead of a new one.")
        mid = self.store.add_mail({
            "message_id": msg.message_id, "connector": msg.connector, "received_at": msg.received_at,
            "sender": msg.sender, "sender_name": msg.sender_name, "subject": msg.subject or "(no subject)",
            "snippet": " ".join(msg.text.split())[:240], "label": t.label, "status": _STATUS[t.label],
            "reasons": t.reasons, "keys": t.keys, "signals": t.signals, "vendor_id": t.vendor_id,
            "attachments": atts, "run_ids": [a["run_id"] for a in atts if a["run_id"]]})
        row = self.store.mail(mid)
        if row["status"] == "held" and self.on_held:
            try:
                self.on_held(row)
            except Exception:
                pass
        return row, True

    def ingest_raw(self, raw: bytes, connector: str = "upload") -> tuple[dict, bool]:
        return self.ingest(parse_eml(raw, connector))

    def sync(self, connector: str = "folder") -> dict:
        msgs = get(connector).fetch()
        new = []
        for m in msgs:
            row, is_new = self.ingest(m)
            if is_new:
                new.append(row)
        by: dict[str, int] = {}
        for r in new:
            by[r["label"]] = by.get(r["label"], 0) + 1
        return {"connector": connector, "fetched": len(msgs), "new": len(new), "by_label": by,
                "runs_started": sum(len(r["run_ids"]) for r in new), "mails": new}

    def release(self, mid: str, reviewer: str, note: str) -> dict:
        """A person vouches for a held email: its documents go to the pipeline, where every rule still applies."""
        row = self._held(mid)
        atts, runs = [], list(row["run_ids"])
        for a in row["attachments"]:
            path = self.dir / f"{a['sha256']}.bin"
            if a["kind"] not in ("risky", "other") and path.exists() and not a["run_id"]:
                res = self.submit(a["filename"], path.read_bytes())
                a = a | {"run_id": res["run_id"], "existing": res["existing"]}
                runs.append(res["run_id"])
            atts.append(a)
        self.store.update_mail(mid, status="released", attachments=atts, run_ids=runs,
                               note=f"Released by {reviewer}: {note}")
        return self.store.mail(mid)

    def dismiss(self, mid: str, reviewer: str, note: str) -> dict:
        self._held(mid)
        self.store.update_mail(mid, status="dismissed", note=f"Dismissed by {reviewer}: {note}")
        return self.store.mail(mid)

    def _held(self, mid: str) -> dict:
        row = self.store.mail(mid)
        if row is None:
            raise KeyError(mid)
        if row["status"] != "held":
            raise ValueError(f"Email is {row['status']}; only held emails can be released or dismissed.")
        return row
