"""Send one email through the connected mailbox: Gmail (SMTP, same app password as reading) or Outlook
(Graph sendMail; the app registration needs Mail.Send). Used for the reviewer-approved email back to a vendor."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from .connectors import GmailConnector, MailboxError, OutlookConnector


def available() -> str | None:
    if GmailConnector().configured():
        return "gmail"
    if OutlookConnector().configured():
        return "outlook"
    return None


def send(to: str, subject: str, body: str, via: str | None = None, smtp_factory=None,
         client: httpx.Client | None = None) -> dict:
    via = via or available()
    if via is None:
        raise MailboxError("Connect Gmail or Outlook on the dashboard to send emails.")
    if via == "gmail":
        g = GmailConnector()
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = g.address, to, subject
        msg.set_content(body)
        try:
            with (smtp_factory or (lambda: smtplib.SMTP("smtp.gmail.com", 587, timeout=20)))() as s:
                s.starttls()
                s.login(g.address, g.password)
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise MailboxError("Gmail refused the app password.")
        except (smtplib.SMTPException, OSError) as e:
            raise MailboxError(f"Gmail could not send the email: {type(e).__name__}")
        return {"via": "gmail", "from": g.address}
    o = OutlookConnector()
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.post(f"https://login.microsoftonline.com/{o.tenant}/oauth2/v2.0/token",
                        data={"grant_type": "client_credentials", "client_id": o.client_id,
                              "client_secret": o.secret, "scope": "https://graph.microsoft.com/.default"})
        if r.status_code != 200:
            raise MailboxError(f"Microsoft sign-in failed ({r.status_code}).")
        r = client.post(f"{o.GRAPH}/users/{o.mailbox}/sendMail",
                        headers={"Authorization": f"Bearer {r.json()['access_token']}"},
                        json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                                          "toRecipients": [{"emailAddress": {"address": to}}]},
                              "saveToSentItems": True})
        if r.status_code not in (200, 202):
            raise MailboxError(f"Outlook did not send the email ({r.status_code}). The app needs Mail.Send.")
        return {"via": "outlook", "from": o.mailbox}
    except httpx.HTTPError as e:
        raise MailboxError(f"Could not reach Microsoft Graph: {type(e).__name__}")
    finally:
        if own:
            client.close()


def test(service: str) -> str:
    """Check a mailbox connection without changing anything."""
    if service == "gmail":
        return GmailConnector().check()
    if service == "outlook":
        return OutlookConnector().check()
    raise MailboxError(f"Unknown mailbox {service}")
