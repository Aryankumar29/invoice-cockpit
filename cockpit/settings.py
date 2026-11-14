"""Connections made from the dashboard. Saved to <data dir>/settings.json (kept out of git and Docker, like .env)
and applied to the process environment, which every connector reads. Values saved here win over .env.
Secrets are never sent back to the browser: the UI only sees whether each field is set."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

# service -> [(env var, label, secret, hint)]
SERVICES: dict[str, list[tuple[str, str, bool, str]]] = {
    "gmail": [("GMAIL_ADDRESS", "Gmail address", False, "ap@yourcompany.com"),
              ("GMAIL_APP_PASSWORD", "App password", True, "16 characters, from Google account > Security")],
    "outlook": [("OUTLOOK_MAILBOX", "Mailbox address", False, "ap@yourcompany.com"),
                ("OUTLOOK_TENANT_ID", "Tenant ID", False, "Directory (tenant) ID of the app registration"),
                ("OUTLOOK_CLIENT_ID", "Client ID", False, "Application (client) ID"),
                ("OUTLOOK_CLIENT_SECRET", "Client secret", True, "Needs Mail.Read, and Mail.Send to send")],
    "slack": [("SLACK_WEBHOOK_URL", "Incoming webhook URL", True, "https://hooks.slack.com/services/...")],
    "sheets": [("GOOGLE_SHEET_ID", "Google Sheet link or ID", False, "https://docs.google.com/spreadsheets/d/..."),
               ("GOOGLE_SERVICE_ACCOUNT_JSON", "Service account key (optional)", True,
                "Paste the JSON key, or leave empty if the sheet is shared by link")],
}
_lock = threading.Lock()


def _sheet_id(v: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", v)
    return m.group(1) if m else v.strip()


class Settings:
    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir)
        self.path = self.dir / "settings.json"
        self.apply()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def apply(self) -> None:
        for k, v in self._read().items():
            if v:
                os.environ[k] = v

    def save(self, service: str, values: dict[str, str]) -> None:
        if service not in SERVICES:
            raise KeyError(service)
        with _lock:
            data = self._read()
            for key, label, secret, _ in SERVICES[service]:
                v = (values.get(key) or "").strip()
                if not v:
                    if secret and (data.get(key) or os.environ.get(key)):
                        continue  # empty secret field = keep the one already saved
                    data.pop(key, None)
                    os.environ.pop(key, None)
                    continue
                if key == "GOOGLE_SHEET_ID":
                    v = _sheet_id(v)
                if key == "GOOGLE_SERVICE_ACCOUNT_JSON":
                    try:
                        info = json.loads(v)
                        assert info.get("client_email") and info.get("private_key")
                    except (ValueError, AssertionError, AttributeError):
                        raise ValueError("The service account key is not a valid Google JSON key file.")
                    key_file = self.dir / "google-service-account.json"
                    key_file.write_text(v, encoding="utf-8")
                    data["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(key_file)
                    os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(key_file)
                    continue
                data[key] = v
                os.environ[key] = v
            if service == "sheets" and data.get("GOOGLE_SHEET_ID"):
                data["COCKPIT_PO_SOURCE"] = os.environ["COCKPIT_PO_SOURCE"] = "sheets"  # connecting a sheet uses it
            self._write(data)

    def set_vars(self, values: dict[str, str | None]) -> None:
        """Internal settings chosen in the dashboard (which PO book is in use). None removes the key."""
        with _lock:
            data = self._read()
            for k, v in values.items():
                if v:
                    data[k] = v
                    os.environ[k] = v
                else:
                    data.pop(k, None)
                    os.environ.pop(k, None)
            self._write(data)

    def disconnect(self, service: str) -> None:
        if service not in SERVICES:
            raise KeyError(service)
        with _lock:
            data = self._read()
            keys = [k for k, *_ in SERVICES[service]] + (["GOOGLE_SERVICE_ACCOUNT_FILE"] if service == "sheets" else [])
            for k in keys:
                data.pop(k, None)
                os.environ.pop(k, None)
            if service == "sheets":
                (self.dir / "google-service-account.json").unlink(missing_ok=True)
                if data.get("COCKPIT_PO_SOURCE") == "sheets":
                    data.pop("COCKPIT_PO_SOURCE")
                    os.environ.pop("COCKPIT_PO_SOURCE", None)
            self._write(data)

    def describe(self) -> dict:
        """Per service: each field's label and whether it is set; non-secret values are shown."""
        saved = self._read()
        out = {}
        for service, fields in SERVICES.items():
            rows = []
            for key, label, secret, hint in fields:
                env_key = "GOOGLE_SERVICE_ACCOUNT_FILE" if key == "GOOGLE_SERVICE_ACCOUNT_JSON" else key
                val = os.environ.get(env_key, "")
                rows.append({"key": key, "label": label, "secret": secret, "hint": hint, "set": bool(val),
                             "value": None if secret else val, "from": "dashboard" if env_key in saved
                             else ("env" if val else None)})
            out[service] = rows
        return out
