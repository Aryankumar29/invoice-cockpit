"""Mail intake: pull emails from a mailbox, triage each one (triage.py), send invoice attachments into the
pipeline, and hold suspicious or unclear emails for a person. Idempotent on Message-ID."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .. import sources
from ..store import Store
from .connectors import MailboxError, get, status
from .message import MailMessage, parse_eml

__all__ = ["MailIntake", "MailboxError", "parse_eml", "status"]

class MailIntake:
    def __init__(self, store: Store, submit: Callable[[str, bytes], dict], records=sources.records,
                 on_held: Callable[[dict], None] | None = None):
        self.store, self.submit, self.records, self.on_held = store, submit, records, on_held
        self.dir = Path(store.dir) / "mail"
        self.dir.mkdir(parents=True, exist_ok=True)
        from ..graph.mail import build
        self.graph = build(self)

    def ingest(self, msg: MailMessage) -> tuple[dict, bool]:
        """Triage one email through the mail graph (cockpit/graph/mail.py). Returns (mail row, is_new)."""
        out = self.graph.invoke({"msg": msg})
        return (out["existing"], False) if out.get("existing") else (out["row"], True)

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
