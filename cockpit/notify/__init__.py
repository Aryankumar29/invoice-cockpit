"""Notifications: a Slack alert when an invoice needs a person, a daily digest for the team, and vendor email
drafts. Settings:
  SLACK_WEBHOOK_URL       incoming webhook; without it messages are kept as previews only
  COCKPIT_SLACK_ALERTS    which events alert (default needs_review,rejected,failed,held_mail; 'off' for none)
  COCKPIT_DIGEST_TIME     local HH:MM for the daily digest (default 18:00; 'off' to disable)
  COCKPIT_PUBLIC_URL      base URL used in links (default http://localhost:8000)"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

from ..core.rules import money
from ..store import Store
from .slack import Slack, context, header, section
from .vendor_email import draft as vendor_draft

__all__ = ["Notifier", "DigestScheduler", "vendor_draft", "digest_window"]

_OUTCOME = {"auto_approved": "Auto-approved", "approved": "Approved", "needs_review": "Needs review",
            "returned": "Returned to vendor", "rejected": "Rejected", "failed": "Failed"}


def _reason(code: str) -> str:
    return code.replace("_", " ").capitalize()


def public_url() -> str:
    return os.environ.get("COCKPIT_PUBLIC_URL", "http://localhost:8000").rstrip("/")


def alerts_enabled() -> set[str]:
    v = os.environ.get("COCKPIT_SLACK_ALERTS", "needs_review,rejected,failed,held_mail")
    return set() if v.strip().lower() == "off" else {x.strip() for x in v.split(",") if x.strip()}


def digest_window(day: datetime | None = None) -> tuple[str, str, datetime]:
    """UTC ISO bounds of a local calendar day."""
    local = (day or datetime.now()).astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    iso = lambda d: d.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return iso(start), iso(end), start


class Notifier:
    def __init__(self, store: Store, slack: Slack | None = None):
        self.store = store
        self.slack = slack or Slack(store)

    # per-event alerts -------------------------------------------------------
    def run_alert(self, run: dict, mail: dict | None = None) -> dict | None:
        outcome = run.get("outcome")
        if outcome not in alerts_enabled() or outcome not in ("needs_review", "rejected", "failed"):
            return None
        link = f"{public_url()}/runs/{run['id']}/review"
        who = run.get("vendor_name") or "Unknown vendor"
        amount = money(run["total_cents"]) if run.get("total_cents") is not None else "amount unread"
        reasons = ", ".join(_reason(r) for r in run.get("reasons") or []) or "see run"
        title = f"{_OUTCOME.get(outcome, outcome)}: {who} {run.get('invoice_number') or ''} {amount}".strip()
        lines = [f"*{title}*", f"Why: {reasons}"]
        if run.get("verdict"):
            lines.append(f"> {run['verdict'][:400]}")
        if run.get("recommended_action"):
            lines.append(f"Next step: {run['recommended_action']}")
        d = vendor_draft(run, None, "")
        if d and d.get("available"):
            lines.append("A correction email to the vendor is drafted and waiting for review.")
        if mail:
            lines.append(f"Arrived by email from {mail['sender']} ({mail['subject'][:80]}).")
        text = "\n".join(lines)
        blocks = [section(text + f"\n<{link}|Open in Invoice Cockpit>"),
                  context(f"PO {run.get('po_number') or 'none'} · run {run['id']} · policy {run.get('policy_version')}")]
        return self.slack.post(f"run_{outcome}", title, text, blocks, run["id"])

    def mail_alert(self, mail: dict) -> dict | None:
        if "held_mail" not in alerts_enabled():
            return None
        kind = "Suspicious email held" if mail["label"] == "suspicious" else "Email needs a look"
        title = f"{kind}: {mail['sender']}"
        text = "\n".join([f"*{title}*", f"Subject: {mail['subject'][:120]}",
                          *[f"• {r}" for r in mail["reasons"][:4]]])
        blocks = [section(text + f"\n<{public_url()}/inbox|Open the inbox>")]
        return self.slack.post(f"mail_{mail['label']}", title, text, blocks)

    # daily digest -----------------------------------------------------------
    def build_digest(self, day: datetime | None = None, usage: dict | None = None) -> dict:
        since, until, start = digest_window(day)
        runs = [r for r in self.store.runs_since(since, until) if r["status"] in ("done", "failed")]
        by: dict[str, list] = {}
        for r in runs:
            by.setdefault(r["outcome"], []).append(r)
        value = lambda rs: money(sum(r["total_cents"] or 0 for r in rs))
        mails = self.store.mail_stats(since)
        mails_today = [m for m in self.store.mails(limit=500) if since <= m["created_at"] < until]
        open_all = self.store.summaries("needs_review", None, 500)
        title = f"Invoice Cockpit daily summary, {start:%a %d %b %Y}"
        lines = [f"*{title}*"]
        if not runs and not mails_today:
            lines.append("No invoices or emails today.")
        else:
            stp = len(by.get("auto_approved", [])) / len(runs) if runs else 0
            lines.append(f"*{len(runs)} invoice(s) processed, {value(runs)}.* Straight-through rate {stp:.0%}.")
            for o in ("auto_approved", "approved", "needs_review", "returned", "rejected", "failed"):
                if by.get(o):
                    lines.append(f"• {_OUTCOME[o]}: {len(by[o])} ({value(by[o])})")
            if mails_today:
                lines.append(f"*Inbox:* {len(mails_today)} email(s): {mails.get('invoice', 0)} with invoices, "
                             f"{mails.get('not_invoice', 0)} not invoices, {mails.get('suspicious', 0)} suspicious "
                             f"held, {mails.get('unsure', 0)} unclear held.")
        attention = by.get("needs_review", []) + by.get("rejected", []) + by.get("failed", [])
        if attention:
            lines.append("*Needs attention today:*")
            for r in attention[:8]:
                why = ", ".join(_reason(x) for x in r["reasons"][:2])
                lines.append(f"• <{public_url()}/runs/{r['id']}/review|{r['vendor_name'] or 'Unknown'} "
                             f"{r['invoice_number'] or ''}> {money(r['total_cents']) if r['total_cents'] is not None else ''}"
                             f" · {_OUTCOME.get(r['outcome'])} · {why}")
            if len(attention) > 8:
                lines.append(f"…and {len(attention) - 8} more.")
        held = [m for m in mails_today if m["status"] == "held"]
        for m in held[:5]:
            lines.append(f"• Held email from {m['sender']}: {m['subject'][:70]}")
        if open_all:
            oldest = min(r["created_at"] for r in open_all)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).days
            lines.append(f"*Review queue (all days):* {len(open_all)} open, oldest {age} day(s).")
        if usage:
            lines.append(f"Model use today: {usage.get('today_calls', 0)} call(s), {usage.get('today_tokens', 0):,} "
                         f"tokens.")
        text = "\n".join(lines)
        return {"title": title, "text": text, "blocks": [header(title), section("\n".join(lines[1:]) or " "),
                                                          context(f"<{public_url()}|Open the dashboard>")],
                "counts": {o: len(v) for o, v in by.items()}, "invoices": len(runs), "emails": len(mails_today),
                "since": since, "until": until}

    def send_digest(self, day: datetime | None = None, usage: dict | None = None) -> dict:
        d = self.build_digest(day, usage)
        return self.slack.post("digest", d["title"], d["text"], d["blocks"]) | {"digest": d}


class DigestScheduler:
    """Sends the digest once a day at COCKPIT_DIGEST_TIME (local). Remembers the last day sent across restarts."""

    def __init__(self, notifier: Notifier, usage_fn=None, poll: float = 60):
        self.notifier, self.usage_fn, self.poll = notifier, usage_fn, poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def at() -> tuple[int, int] | None:
        v = os.environ.get("COCKPIT_DIGEST_TIME", "18:00").strip().lower()
        if v in ("", "off", "none"):
            return None
        h, _, m = v.partition(":")
        return int(h), int(m or 0)

    def due(self, now: datetime | None = None) -> bool:
        at = self.at()
        if at is None:
            return False
        now = (now or datetime.now()).astimezone()
        today = now.date().isoformat()
        return (now.hour, now.minute) >= at and self.notifier.store.kv_get("digest_last") != today

    def tick(self, now: datetime | None = None) -> dict | None:
        if not self.due(now):
            return None
        usage = self.usage_fn() if self.usage_fn else None
        row = self.notifier.send_digest(None, usage)
        self.notifier.store.kv_set("digest_last", (now or datetime.now()).astimezone().date().isoformat())
        return row

    def start(self) -> None:
        if self.at() is None:
            return
        self._thread = threading.Thread(target=self._loop, name="cockpit-digest", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.tick()
            except Exception:
                pass
