"""Mailbox connectors. All are read-only: nothing is moved, flagged or deleted in the mailbox; the cockpit
remembers which Message-IDs it has seen.
  folder   .eml files in a folder (COCKPIT_MAILBOX_DIR, default fixtures/mail): the demo inbox
  gmail    IMAP with an app password (GMAIL_ADDRESS, GMAIL_APP_PASSWORD, optional GMAIL_FOLDER, GMAIL_SEARCH)
  outlook  Microsoft Graph, app registration with Mail.Read (OUTLOOK_TENANT_ID, OUTLOOK_CLIENT_ID,
           OUTLOOK_CLIENT_SECRET, OUTLOOK_MAILBOX)"""
from __future__ import annotations

import imaplib
import os
from datetime import date, timedelta
from pathlib import Path

import httpx

from .message import MailMessage, parse_eml

ROOT = Path(__file__).resolve().parents[2]


class MailboxError(Exception):
    pass


def _max() -> int:
    return int(os.environ.get("COCKPIT_MAIL_MAX", "50"))


class FolderConnector:
    name, label = "folder", "Demo mailbox folder"

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("COCKPIT_MAILBOX_DIR") or ROOT / "fixtures" / "mail")

    def configured(self) -> bool:
        return self.path.is_dir()

    def describe(self) -> str:
        return str(self.path)

    def fetch(self) -> list[MailMessage]:
        if not self.path.is_dir():
            raise MailboxError(f"Mailbox folder not found: {self.path}")
        return [parse_eml(f.read_bytes(), self.name) for f in sorted(self.path.glob("*.eml"))[:_max()]]


class GmailConnector:
    name, label = "gmail", "Gmail (IMAP)"
    HOST = "imap.gmail.com"

    def __init__(self, imap_factory=None):
        self.address = os.environ.get("GMAIL_ADDRESS", "")
        self.password = os.environ.get("GMAIL_APP_PASSWORD", "")
        self.folder = os.environ.get("GMAIL_FOLDER", "INBOX")
        self.search = os.environ.get("GMAIL_SEARCH") or f'SINCE {(date.today() - timedelta(days=7)):%d-%b-%Y}'
        self._factory = imap_factory or (lambda: imaplib.IMAP4_SSL(self.HOST, 993))

    def configured(self) -> bool:
        return bool(self.address and self.password)

    def describe(self) -> str:
        return f"{self.address or 'not set'} / {self.folder}"

    def check(self) -> str:
        if not self.configured():
            raise MailboxError("Add the Gmail address and app password first.")
        try:
            box = self._factory()
            box.login(self.address, self.password)
            ok, data = box.select(f'"{self.folder}"', readonly=True)
            box.logout()
            return f"Signed in to {self.address}; {self.folder} has {int(data[0]) if ok == 'OK' else 0} emails."
        except imaplib.IMAP4.error:
            raise MailboxError("Gmail refused the login. Check the address and use an app password, not your "
                               "normal password.")
        except OSError as e:
            raise MailboxError(f"Could not reach Gmail: {type(e).__name__}")

    def fetch(self) -> list[MailMessage]:
        if not self.configured():
            raise MailboxError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (Google account > Security > App passwords).")
        try:
            box = self._factory()
            box.login(self.address, self.password)
            box.select(f'"{self.folder}"', readonly=True)
            ok, data = box.search(None, self.search)
            ids = (data[0] or b"").split()[-_max():] if ok == "OK" else []
            out = []
            for i in ids:
                ok, parts = box.fetch(i, "(BODY.PEEK[])")  # PEEK: do not mark as read
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None) if ok == "OK" else None
                if raw:
                    out.append(parse_eml(raw, self.name))
            box.logout()
            return out
        except imaplib.IMAP4.error as e:
            raise MailboxError(f"Gmail refused the login or search: {e}")
        except OSError as e:
            raise MailboxError(f"Could not reach Gmail: {type(e).__name__}")


class OutlookConnector:
    name, label = "outlook", "Outlook / Microsoft 365 (Graph)"
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, client: httpx.Client | None = None):
        self.tenant = os.environ.get("OUTLOOK_TENANT_ID", "")
        self.client_id = os.environ.get("OUTLOOK_CLIENT_ID", "")
        self.secret = os.environ.get("OUTLOOK_CLIENT_SECRET", "")
        self.mailbox = os.environ.get("OUTLOOK_MAILBOX", "")
        self._client = client

    def configured(self) -> bool:
        return all((self.tenant, self.client_id, self.secret, self.mailbox))

    def describe(self) -> str:
        return f"{self.mailbox or 'not set'} / Inbox"

    def check(self) -> str:
        if not self.configured():
            raise MailboxError("Add the mailbox, tenant ID, client ID and client secret first.")
        client = self._client or httpx.Client(timeout=30)
        try:
            r = client.post(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                            data={"grant_type": "client_credentials", "client_id": self.client_id,
                                  "client_secret": self.secret, "scope": "https://graph.microsoft.com/.default"})
            if r.status_code != 200:
                raise MailboxError(f"Microsoft sign-in failed ({r.status_code}). Check the tenant, client ID and secret.")
            r = client.get(f"{self.GRAPH}/users/{self.mailbox}/mailFolders/inbox",
                           headers={"Authorization": f"Bearer {r.json()['access_token']}"})
            if r.status_code != 200:
                raise MailboxError(f"Signed in, but the inbox of {self.mailbox} is not readable ({r.status_code}). "
                                   f"The app needs the Mail.Read permission.")
            return f"Signed in; {self.mailbox} inbox has {r.json().get('totalItemCount', 0)} emails."
        except httpx.HTTPError as e:
            raise MailboxError(f"Could not reach Microsoft Graph: {type(e).__name__}")
        finally:
            if self._client is None:
                client.close()

    def fetch(self) -> list[MailMessage]:
        if not self.configured():
            raise MailboxError("Set OUTLOOK_TENANT_ID, OUTLOOK_CLIENT_ID, OUTLOOK_CLIENT_SECRET and OUTLOOK_MAILBOX "
                               "(Entra app registration with the Mail.Read application permission).")
        client = self._client or httpx.Client(timeout=30)
        try:
            r = client.post(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                            data={"grant_type": "client_credentials", "client_id": self.client_id,
                                  "client_secret": self.secret, "scope": "https://graph.microsoft.com/.default"})
            if r.status_code != 200:
                raise MailboxError(f"Microsoft sign-in failed ({r.status_code}).")
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}
            base = f"{self.GRAPH}/users/{self.mailbox}/mailFolders/inbox/messages"
            r = client.get(base, headers=h, params={"$top": str(_max()), "$select": "id",
                                                    "$orderby": "receivedDateTime desc"})
            if r.status_code != 200:
                raise MailboxError(f"Graph returned {r.status_code} listing the inbox.")
            out = []
            for m in r.json().get("value", []):
                raw = client.get(f"{self.GRAPH}/users/{self.mailbox}/messages/{m['id']}/$value", headers=h)
                if raw.status_code == 200:
                    out.append(parse_eml(raw.content, self.name))
            return out
        except httpx.HTTPError as e:
            raise MailboxError(f"Could not reach Microsoft Graph: {type(e).__name__}")
        finally:
            if self._client is None:
                client.close()


CONNECTORS = {c.name: c for c in (FolderConnector, GmailConnector, OutlookConnector)}


def get(name: str):
    if name not in CONNECTORS:
        raise MailboxError(f"Unknown mailbox '{name}'. Use one of: {', '.join(CONNECTORS)}.")
    return CONNECTORS[name]()


def status() -> list[dict]:
    out = []
    for cls in CONNECTORS.values():
        c = cls()
        out.append({"name": c.name, "label": c.label, "configured": c.configured(), "detail": c.describe()})
    return out
