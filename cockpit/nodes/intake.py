"""Node 1 - Intake: size, type sniffing, image-to-PDF normalization, encryption and page-count checks."""
from __future__ import annotations

import io
from pathlib import Path

from .. import ingest
from ._shared import StageStop

STAGE, TITLE = "intake", "Intake"
MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 10


def _stop(message: str, data: dict, reason: str) -> StageStop:
    return StageStop(STAGE, TITLE, message, data | {"reason": reason})


def run(pdf: bytes, run: dict):
    from pypdf import PdfReader
    data = {"bytes": len(pdf), "sha256": run["file_hash"], "filename": run["filename"],
            "detected_type": ingest.sniff(pdf)}
    if len(pdf) > MAX_BYTES:
        raise _stop("File is larger than 10 MB.", data, "FILE_TOO_LARGE")
    try:
        pdf, notes = ingest.normalize(pdf)
    except ingest.IngestError as e:
        raise _stop(e.message, data, e.reason)
    data["normalized"] = notes
    if not pdf.startswith(b"%PDF"):
        raise _stop("This file is not a PDF or a supported image (JPG, PNG, TIFF, BMP, GIF, WEBP).", data, "NOT_A_PDF")
    if notes:  # keep the original for audit; the stored, viewed and extracted file is the normalized PDF
        path = Path(run["file_path"])
        orig = path.with_suffix(".orig")
        if not orig.exists():
            orig.write_bytes(path.read_bytes())
        path.write_bytes(pdf)
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted:
            raise _stop("PDF is password-protected.", data, "PDF_ENCRYPTED")
        pages = len(reader.pages)
    except StageStop:
        raise
    except Exception as e:
        raise _stop(f"PDF could not be opened ({type(e).__name__}).", data, "PDF_CORRUPT")
    if pages == 0 or pages > MAX_PAGES:
        raise _stop(f"PDF has {pages} pages (allowed 1-{MAX_PAGES}).", data, "PAGE_LIMIT")
    msg = f"PDF accepted: {pages} page(s), {len(pdf) / 1024:.0f} KB." + "".join(f" {n}" for n in notes)
    return "ok", msg, data | {"pages": pages}, pdf
