"""Build the demo inbox: fixtures/mail/*.eml, real MIME emails that reuse the fixture PDFs.
Each one exercises a triage path; EXPECTED is what the triage must say. Usage: python fixtures/generate_mail.py"""
from __future__ import annotations

import io
import sys
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
OUT = HERE / "mail"
PDF = HERE / "pdf"
AP = "ap@demo-buyer.example"
T0 = datetime(2026, 10, 26, 8, 5, tzinfo=timezone.utc)

# file -> (label, runs started)
EXPECTED = {
    "01_northwind_invoice.eml": ("invoice", 1),
    "02_globex_two_invoices.eml": ("invoice", 2),
    "03_northwind_price_variance.eml": ("invoice", 1),
    "04_initech_tax_inclusive.eml": ("invoice", 1),
    "05_northwind_newsletter.eml": ("not_invoice", 0),
    "06_acme_statement.eml": ("not_invoice", 0),
    "07_globex_quotation.eml": ("not_invoice", 0),
    "08_lookalike_bank_change.eml": ("suspicious", 0),
    "09_acme_link_only.eml": ("unsure", 0),
    "10_internal_lunch.eml": ("not_invoice", 0),
    "11_northwind_resent_duplicate.eml": ("invoice", 1),
    "12_contoso_unknown_vendor.eml": ("invoice", 1),
    "13_acme_scanned_invoice.eml": ("invoice", 1),
}


def doc_pdf(title: str, lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 22)
    c.drawString(54, h - 72, title)
    c.setFont("Helvetica", 10)
    y = h - 110
    for s in lines:
        c.drawString(54, y, s)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


STATEMENT = doc_pdf("STATEMENT OF ACCOUNT", [
    "Acme Industrial Inc.  |  invoices@acme-industrial.example", "Statement date: October 25, 2026",
    "Customer: Demo Buyer Corp, 500 Market Street, Floor 9, San Francisco, CA 94105", "",
    "Opening balance                                   $0.00",
    "AC-2026-288   Oct 02  Invoice   PO-1046            $3,000.00",
    "AC-2026-301   Oct 15  Payment                      -$3,000.00",
    "Closing balance                                   $0.00", "",
    "This statement is for your information. Please do not pay from this statement."])
QUOTE = doc_pdf("QUOTATION", [
    "Globex Services LLC  |  ar@globex-services.example", "Quote no: Q-2231     Date: October 24, 2026",
    "Prepared for: Demo Buyer Corp", "",
    "Implementation consulting, phase 3 (8 days)     8 x $1,000.00     $8,000.00",
    "Valid until: November 30, 2026", "", "To accept, please issue a purchase order quoting Q-2231."])


def mail(n: int, sender: str, subject: str, body: str, *, html: str | None = None,
         files: list[tuple[str, bytes, str]] = (), reply_to: str | None = None, to: str = AP) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, to, subject
    m["Date"] = format_datetime(T0 + timedelta(minutes=37 * n))
    m["Message-ID"] = make_msgid(idstring=f"demo{n:02d}", domain=sender.split("@")[-1].strip(">"))
    if reply_to:
        m["Reply-To"] = reply_to
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    for name, data, ctype in files:
        main, sub = ctype.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return m


def pdf(name: str) -> bytes:
    return (PDF / name).read_bytes()


def build() -> dict[str, EmailMessage]:
    P = "application/pdf"
    sig_nw = "\n\nKind regards,\nBilling team\nNorthwind Supply Co. | (510) 555-0142"
    return {
        "01_northwind_invoice.eml": mail(
            1, "Northwind Supply Co. Billing <billing@northwind-supply.example>", "Invoice INV-H-001 for PO-1042",
            "Hello Accounts Payable,\n\nPlease find attached invoice INV-H-001 for purchase order PO-1042 "
            "(widgets, brackets and fastener kits), delivered on 15 October." + sig_nw,
            files=[("INV-H-001.pdf", pdf("H1_northwind_INV-H-001.pdf"), P)]),
        "02_globex_two_invoices.eml": mail(
            2, "Globex Services AR <ar@globex-services.example>", "Invoices INV-S-001 and INV-S-002 (PO-1047)",
            "Hi team,\n\nAttached are both invoices for the implementation project under PO 1047: phase 1 "
            "(6 days) and phase 2 (4 days).\n\nThanks,\nGlobex Services LLC",
            files=[("INV-S-001.pdf", pdf("S1_globex_INV-S-001.pdf"), P),
                   ("INV-S-002.pdf", pdf("S2_globex_INV-S-002.pdf"), P)]),
        "03_northwind_price_variance.eml": mail(
            3, "Northwind Supply Co. Billing <billing@northwind-supply.example>", "Invoice INV-D-001 / PO-1051",
            "Hello,\n\nInvoice INV-D-001 for PO-1051 is attached. Note our updated price list for Widget B."
            + sig_nw, files=[("INV-D-001.pdf", pdf("D1_northwind_INV-D-001.pdf"), P)]),
        "04_initech_tax_inclusive.eml": mail(
            4, "Initech Billing <billing@initech-office.example>", "Invoice IN-5610 - office chairs",
            "Good morning,\n\nAttached is invoice IN-5610 for the 12 ergonomic chairs (PO-1090). All prices "
            "include sales tax.\n\nInitech Office Supplies",
            files=[("IN-5610.pdf", pdf("R-INCL_initech_IN-5610.pdf"), P)]),
        "05_northwind_newsletter.eml": mail(
            5, "Northwind Supply Co. <news@northwind-supply.example>", "October product news: new fastener range",
            "Our new stainless fastener range is here. Read more on our website.\n\nUnsubscribe: "
            "https://northwind-supply.example/unsubscribe",
            html="<html><body><h1>New fastener range</h1><p>Our new stainless range is here.</p>"
                 "<img src='https://northwind-supply.example/banner.png'>"
                 "<p><a href='https://northwind-supply.example/unsubscribe'>Unsubscribe</a></p></body></html>"),
        "06_acme_statement.eml": mail(
            6, "Acme Industrial Accounts <invoices@acme-industrial.example>", "Statement of account - October 2026",
            "Dear customer,\n\nPlease find your monthly statement attached. Your account is fully paid.\n\n"
            "Acme Industrial Inc.", files=[("Acme-statement-2026-10.pdf", STATEMENT, P)]),
        "07_globex_quotation.eml": mail(
            7, "Globex Services AR <ar@globex-services.example>", "Quotation Q-2231 for phase 3",
            "Hi,\n\nAs discussed, here is our quotation for phase 3. Happy to walk through it.\n\nGlobex Services",
            files=[("Q-2231.pdf", QUOTE, P)]),
        "08_lookalike_bank_change.eml": mail(
            8, "Northwind Supply Co. <accounts@northwind-supp1y.example>",
            "URGENT: updated bank details for invoice INV-B-001",
            "Hello,\n\nPlease note we have changed our bank. Kindly update your records and pay invoice INV-B-001 "
            "(PO-1042) and all future invoices to the new account shown on the attached invoice. Payment is "
            "needed today.\n\nRegards,\nNorthwind Supply Co. accounts",
            files=[("INV-B-001.pdf", pdf("R-BANK_northwind_INV-B-001.pdf"), P)],
            reply_to="northwind.accounts@freemail.example"),
        "09_acme_link_only.eml": mail(
            9, "Acme Industrial Accounts <invoices@acme-industrial.example>", "Your invoice AC-2026-311 is ready",
            "Hello,\n\nYour invoice AC-2026-311 for PO-1046 is ready. View and download your invoice here:\n"
            "https://portal.acme-industrial.example/inv/AC-2026-311\n\nAcme Industrial Inc."),
        "10_internal_lunch.eml": mail(
            10, "Maya Ortiz <maya.ortiz@demo-buyer.example>", "Team lunch on Friday?",
            "Hi all, shall we do lunch on Friday at 12:30? Reply by Thursday.\n\nMaya"),
        "11_northwind_resent_duplicate.eml": mail(
            11, "Northwind Supply Co. Billing <billing@northwind-supply.example>", "Re: Invoice NW-2026-001 (PO-1038)",
            "Hello,\n\nWe have not received payment for the attached invoice yet. Could you please check?" + sig_nw,
            files=[("NW-2026-001.pdf", pdf("E1_northwind_INV-E-001.pdf"), P)]),
        "12_contoso_unknown_vendor.eml": mail(
            12, "Contoso Fabrication <accounts@contoso-fab.example>", "Invoice CF-88017",
            "Dear Sir or Madam,\n\nPlease find our invoice CF-88017 attached for immediate payment.\n\n"
            "Contoso Fabrication Ltd, Leeds",
            files=[("CF-88017.pdf", pdf("R-UNK_contoso_CF-88017.pdf"), P)]),
        "13_acme_scanned_invoice.eml": mail(
            13, "Acme Industrial Accounts <invoices@acme-industrial.example>", "Invoice for PO-1046 (scan)",
            "Hello,\n\nAttached is the scanned invoice for the hydraulic hose assemblies, PO-1046.\n\n"
            "Acme Industrial Inc.",
            files=[("acme-scan.pdf", pdf("E2_acme_scan_no-invoice-number.pdf"), P)]),
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*.eml"):
        f.unlink()
    for name, m in build().items():
        (OUT / name).write_bytes(bytes(m))
    print(f"Wrote {len(EXPECTED)} emails to {OUT}")


if __name__ == "__main__":
    sys.exit(main())
