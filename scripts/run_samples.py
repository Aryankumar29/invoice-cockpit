"""Run every invoice in samples/ through the running app and print one line per file.

    .venv/Scripts/python scripts/run_samples.py            # all files in samples/
    .venv/Scripts/python scripts/run_samples.py --reset    # reset demo data first (keeps API usage)
    .venv/Scripts/python scripts/run_samples.py file.pdf   # selected files

The runs also appear on the dashboard (http://localhost:8000). Files are uploaded exactly as the UI does it, so
the same intake, extraction and rules apply. Re-running an unchanged file links to its earlier run.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic"}


def call(base: str, path: str, method: str = "GET", body: bytes | None = None, ctype: str | None = None):
    req = urllib.request.Request(base + path, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def upload(base: str, f: Path) -> dict:
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{f.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + f.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return call(base, "/runs", "POST", body, f"multipart/form-data; boundary={boundary}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--dir", type=Path, default=ROOT / "samples")
    ap.add_argument("--base", default="http://127.0.0.1:8000/api")
    ap.add_argument("--reset", action="store_true", help="reset runs before starting")
    a = ap.parse_args()

    files = a.files or sorted(p for p in a.dir.iterdir() if p.suffix.lower() in TYPES)
    if not files:
        print(f"No invoices in {a.dir}. Supported: {', '.join(sorted(TYPES))}")
        return 1
    try:
        h = call(a.base, "/health")
    except OSError as e:
        print(f"App not reachable at {a.base} ({e}). Start it: .venv/Scripts/python -m cockpit.api")
        return 1
    print(f"Extraction: {h['extraction_mode']} {h.get('llm_model') or ''} | {len(files)} file(s)\n")
    if a.reset:
        call(a.base, "/reset", "POST")
    before = call(a.base, "/stats")["api_usage"]["total_tokens"]

    for f in files:
        t0 = time.time()
        try:
            created = upload(a.base, f)
        except Exception as e:
            print(f"{f.name:40} UPLOAD FAILED  {e}")
            continue
        while (d := call(a.base, f"/runs/{created['run_id']}"))["status"] not in ("done", "failed"):
            time.sleep(0.4)
        total = f"{d['total_cents'] / 100:,.2f} {d['currency'] or ''}" if d["total_cents"] is not None else "-"
        print(f"{f.name[:40]:40} {d['outcome']:13} {', '.join(d['reasons'])[:48]:48} "
              f"{(d['vendor_name'] or '-')[:24]:24} {d['invoice_number'] or '-':14} {d['po_number'] or '-':9} "
              f"{total:>14}  {d['extraction_source'] or '-':10} {time.time() - t0:5.1f}s"
              + ("  (already run)" if created.get("existing") else ""))
        for w in d.get("warnings", [])[:3]:
            print(f"{'':40}   ! {w[:130]}")
    used = call(a.base, "/stats")["api_usage"]["total_tokens"] - before
    print(f"\nModel tokens used by this batch: {used:,}. Open http://localhost:8000 for the full detail of each run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
