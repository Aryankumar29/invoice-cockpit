"""One email, whatever mailbox it came from. Every connector hands over raw MIME, so parsing is shared."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

DOC_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/tiff", "image/bmp", "image/gif", "image/webp")
DOC_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp")
RISKY_EXT = (".exe", ".js", ".vbs", ".scr", ".bat", ".cmd", ".jar", ".docm", ".xlsm", ".html", ".htm", ".iso",
             ".zip", ".rar", ".7z")


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes
    inline: bool = False

    @property
    def is_document(self) -> bool:
        if self.inline and len(self.data) < 30_000:  # signature logos, tracking pixels
            return False
        name = self.filename.lower()
        return self.content_type in DOC_TYPES or name.endswith(DOC_EXT)

    @property
    def is_risky(self) -> bool:
        return self.filename.lower().endswith(RISKY_EXT)


@dataclass
class MailMessage:
    message_id: str
    sender: str
    sender_name: str
    subject: str
    received_at: str | None
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    connector: str = "folder"
    reply_to: str | None = None

    @property
    def domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower() if "@" in self.sender else ""


def _html_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def parse_eml(raw: bytes, connector: str = "folder") -> MailMessage:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    name, addr = parseaddr(str(msg.get("From", "")))
    reply = parseaddr(str(msg.get("Reply-To", "")))[1] or None
    try:
        received = parsedate_to_datetime(str(msg["Date"])).isoformat() if msg.get("Date") else None
    except (TypeError, ValueError):
        received = None
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = _html_text(text)
    atts = []
    for part in msg.walk():
        if part.is_multipart() or part is body:
            continue
        disp = part.get_content_disposition()
        fname = part.get_filename()
        if disp is None and not fname:
            continue
        data = part.get_payload(decode=True) or b""
        atts.append(Attachment(fname or f"part-{len(atts) + 1}", part.get_content_type(), data,
                               inline=disp == "inline" or bool(part.get("Content-ID"))))
    mid = str(msg.get("Message-ID") or "").strip() or f"<sha-{hashlib.sha256(raw).hexdigest()[:24]}>"
    return MailMessage(mid, addr.lower(), name, str(msg.get("Subject", "")).strip(), received, text.strip(), atts,
                       connector, reply)
