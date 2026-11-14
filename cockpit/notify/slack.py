"""Slack through an incoming webhook (SLACK_WEBHOOK_URL). Without one, every message is still built and kept as a
'preview' on the Notifications page, so the demo shows exactly what Slack would receive."""
from __future__ import annotations

import os

import httpx

from ..store import Store


class Slack:
    def __init__(self, store: Store, client: httpx.Client | None = None):
        self.store, self._client = store, client

    @property
    def webhook(self) -> str:
        return os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    @property
    def configured(self) -> bool:
        return self.webhook.startswith("https://hooks.slack.com/")

    def post(self, kind: str, title: str, text: str, blocks: list[dict] | None = None,
             run_id: str | None = None) -> dict:
        payload = {"text": f"{title}\n{text}"} | ({"blocks": blocks} if blocks else {})
        if not self.configured:
            return self.store.add_notification("slack", kind, title, text, payload, "preview", run_id)
        client = self._client or httpx.Client(timeout=10)
        try:
            r = client.post(self.webhook, json=payload)
            ok = r.status_code == 200
            return self.store.add_notification("slack", kind, title, text, payload, "sent" if ok else "failed",
                                                run_id, None if ok else f"Slack returned {r.status_code}: {r.text[:120]}")
        except httpx.HTTPError as e:
            return self.store.add_notification("slack", kind, title, text, payload, "failed", run_id,
                                                f"{type(e).__name__}")
        finally:
            if self._client is None:
                client.close()


def section(md: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": md[:2900]}}


def header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150]}}


def context(md: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": md[:2900]}]}
