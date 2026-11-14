"""Google Sheets PO book reader. Read-only, two ways in:
- service account (GOOGLE_SERVICE_ACCOUNT_FILE): Sheets API v4, the sheet is shared with the service account's email;
- no credentials: the sheet is shared "anyone with the link can view" and each tab is read as CSV."""
from __future__ import annotations

import base64
import csv
import io
import json
import time
from pathlib import Path

import httpx

from .book import TABS, BookError

SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
API = "https://sheets.googleapis.com/v4/spreadsheets"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def service_account_token(key_file: str | Path, client: httpx.Client) -> str:
    """OAuth2 JWT-bearer flow for a service account, signed locally (RS256)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    info = json.loads(Path(key_file).read_text(encoding="utf-8"))
    token_uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": info["client_email"], "scope": SCOPE, "aud": token_uri,
                              "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    sig = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    r = client.post(token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                     "assertion": f"{header}.{claims}.{_b64(sig)}"})
    if r.status_code != 200:
        raise BookError(f"Google sign-in failed ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def _rows(values: list[list]) -> list[dict]:
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in values[1:]]


def read_sheet(sheet_id: str, key_file: str | None = None, client: httpx.Client | None = None) -> dict[str, list[dict]]:
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True)
    try:
        if key_file:
            token = service_account_token(key_file, client)
            params = [("ranges", tab) for tab in TABS] + [("valueRenderOption", "FORMATTED_VALUE")]
            r = client.get(f"{API}/{sheet_id}/values:batchGet", params=params,
                           headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                raise BookError(f"Google Sheets returned {r.status_code}: {r.text[:200]}")
            ranges = r.json().get("valueRanges", [])
            return {tab: _rows(vr.get("values", [])) for tab, vr in zip(TABS, ranges)}
        out = {}
        for tab in TABS:
            r = client.get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq",
                           params={"tqx": "out:csv", "sheet": tab})
            if r.status_code != 200 or "text/html" in r.headers.get("content-type", ""):
                raise BookError("The Google Sheet is not readable. Share it as 'anyone with the link can view', "
                                "or set GOOGLE_SERVICE_ACCOUNT_FILE and share it with the service account.")
            rows = list(csv.reader(io.StringIO(r.text)))
            if not rows or [h.strip() for h in rows[0]][:2] != TABS[tab][:2]:
                raise BookError(f"Tab '{tab}' is missing or its header row is not the template's.")
            out[tab] = _rows(rows)
        return out
    except httpx.HTTPError as e:
        raise BookError(f"Could not reach Google Sheets: {type(e).__name__}")
    finally:
        if own:
            client.close()
