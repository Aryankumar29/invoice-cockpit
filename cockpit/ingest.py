"""Intake normalization: whatever arrived becomes one clean PDF, or a plain reason why it cannot.

Images (phone photos, scanner TIFFs) become a PDF; leading junk before the PDF header is stripped; PDFs that
open with an empty user password (owner-password "protected" invoices are common) are decrypted. Nothing about
the content changes: this is transport, not correction.
"""
from __future__ import annotations

import io

MAX_PIXELS = 40_000_000      # per image frame; larger is a decompression-bomb risk, not an invoice
MAX_SIDE = 3500              # ~300 DPI on US Letter; larger photos are downscaled before embedding
_HEADER_WINDOW = 1024        # the PDF spec allows the header anywhere in the first 1024 bytes

_IMAGE_MAGIC = {b"\x89PNG\r\n\x1a\n": "PNG", b"\xff\xd8\xff": "JPEG", b"II*\x00": "TIFF", b"MM\x00*": "TIFF",
                b"BM": "BMP", b"GIF8": "GIF"}


class IngestError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


def sniff(data: bytes) -> str:
    if b"%PDF-" in data[:_HEADER_WINDOW]:
        return "PDF"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "HEIC"
    for magic, kind in _IMAGE_MAGIC.items():
        if data.startswith(magic):
            return kind
    return "UNKNOWN"


def _image_to_pdf(data: bytes, kind: str) -> tuple[bytes, list[str]]:
    from PIL import Image, ImageOps, ImageSequence
    try:
        im = Image.open(io.BytesIO(data))
        frames = []
        for f in ImageSequence.Iterator(im):
            if f.width * f.height > MAX_PIXELS:
                raise IngestError("IMAGE_TOO_LARGE", f"Image is {f.width}x{f.height} pixels; the limit is "
                                                     f"{MAX_PIXELS // 1_000_000} megapixels.")
            f = ImageOps.exif_transpose(f.copy())  # phone photos: honour the camera's rotation tag
            f = f.convert("L" if f.mode in ("1", "L", "LA", "I;16") else "RGB")
            if max(f.size) > MAX_SIDE:
                f.thumbnail((MAX_SIDE, MAX_SIDE))
            frames.append(f)
            if len(frames) > 10:
                break
    except IngestError:
        raise
    except Exception as e:
        raise IngestError("IMAGE_CORRUPT", f"The {kind} image could not be opened ({type(e).__name__}).")
    if not frames:
        raise IngestError("IMAGE_CORRUPT", f"The {kind} image has no frames.")
    out = io.BytesIO()
    frames[0].save(out, "PDF", resolution=300.0, save_all=True, append_images=frames[1:])
    w, h = frames[0].size
    return out.getvalue(), [f"Converted {kind} image to PDF ({len(frames)} page(s), {w}x{h} px)."]


def _decrypt_if_open(data: bytes) -> tuple[bytes, list[str]]:
    from pypdf import PdfReader, PdfWriter
    try:
        reader = PdfReader(io.BytesIO(data))
        if not reader.is_encrypted:
            return data, []
        if not reader.decrypt(""):
            return data, []  # a real user password: intake reports it as protected
        buf = io.BytesIO()
        PdfWriter(clone_from=reader).write(buf)
        return buf.getvalue(), ["PDF had owner-password restrictions only; opened without a password."]
    except Exception:
        return data, []  # leave it to the intake checks to report a corrupt file


def normalize(data: bytes) -> tuple[bytes, list[str]]:
    """Returns (pdf_bytes, notes). Unknown types are returned unchanged so intake can reject them visibly."""
    kind = sniff(data)
    if kind == "PDF":
        notes = []
        start = data.find(b"%PDF-", 0, _HEADER_WINDOW)
        if start > 0:
            data = data[start:]
            notes.append(f"Removed {start} byte(s) of junk before the PDF header.")
        data, more = _decrypt_if_open(data)
        return data, notes + more
    if kind == "HEIC":
        raise IngestError("UNSUPPORTED_FORMAT", "HEIC photos are not supported. Export the photo as JPEG or PDF.")
    if kind in ("PNG", "JPEG", "TIFF", "BMP", "GIF", "WEBP"):
        return _image_to_pdf(data, kind)
    return data, []
