import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Acc } from "../components/ui";
import { money } from "../lib/format";

type Check = [string, string, string, string, string]; // id, check, what it looks at, fails when, then

const GROUPS: { name: string; sub: string; checks: Check[] }[] = [
  {
    name: "The document", sub: "Can it be read, and is everything there?",
    checks: [
      ["R01", "Document readable", "Whether any reading method could get the text out", "Nothing on the page could be read", "Needs review"],
      ["R02", "Required fields present", "Invoice number, invoice date, vendor name, total, currency", "Any of them is missing (nothing is ever invented)", "Needs review"],
      ["R03", "Totals add up", "Each line is quantity × price; lines − discount + tax + freight = total", "The arithmetic is off by more than the rounding allowance", "Needs review"],
    ],
  },
  {
    name: "The vendor", sub: "Is this a real, approved supplier, and is it really them?",
    checks: [
      ["R04", "Vendor approved", "The vendor list in the PO book: name, tax ID, status", "Vendor unknown, inactive, or the tax ID belongs to someone else", "Needs review"],
      ["R05", "Bank details verified", "The bank account printed on the invoice against the one on file", "The account is different (a classic fraud pattern)", "Needs review by two people"],
    ],
  },
  {
    name: "The purchase order", sub: "Was this ordered, from this vendor, in this currency?",
    checks: [
      ["R06", "PO exists and is open", "The PO number on the invoice, or the best matching open PO", "No PO, more than one possible PO, or the PO is closed", "Needs review (closed PO: rejected)"],
      ["R07", "PO belongs to this vendor", "The vendor the PO was issued to", "The PO was issued to a different vendor", "Rejected"],
      ["R08", "Currency matches", "Invoice currency against PO currency", "They differ", "Rejected"],
    ],
  },
  {
    name: "Lines, quantities and money", sub: "Is each line something we ordered, received, and at the agreed price?",
    checks: [
      ["R09", "Lines map to the PO", "Each invoice line against the PO lines (item code, description, price); a 'kit' line can cover several PO lines", "A line matches nothing on the PO, or could match in more than one way", "Needs review"],
      ["R10", "Price within tolerance", "Billed amount per line against quantity × PO price", "A line is above the line allowance", "Needs review"],
      ["R11", "Quantity within what was received", "Everything billed so far on a PO line against the quantity delivered", "More is billed than was delivered", "Needs review"],
      ["R12", "Within the PO's remaining amount", "This invoice against what is left on the PO after earlier invoices", "It goes over what is left, beyond the PO allowance", "Rejected"],
      ["R14", "Tax is right", "Tax on the invoice against the PO's tax rate; handles prices that already include tax", "The tax differs by more than the tax allowance", "Needs review"],
    ],
  },
  {
    name: "Duplicates", sub: "Has this been billed before?",
    checks: [
      ["R13", "Not a duplicate", "Same file, same invoice number from the vendor, or same amount, date and lines in a different layout", "It is the same invoice again (rejected) or looks very similar (review)", "Rejected or needs review"],
    ],
  },
  {
    name: "Approval and dates", sub: "Who may approve it, and is it current?",
    checks: [
      ["R15", "Within approval authority", "The invoice total against the auto-approval limit", "It is above the limit (one approver), or above the two-approver limit", "Needs review"],
      ["R16", "Dates make sense", "Invoice date, due date and the age of the invoice", "Due before invoiced, dated in the future, or older than the age limit", "Needs review"],
    ],
  },
];

const STEPS: [string, string][] = [
  ["Arrives", "By upload, or from a connected mailbox where the email is first sorted (see below)."],
  ["Opened", "File type, size (10 MB), pages (10), password protection. Photos and scans are turned into a PDF."],
  ["Read", "Every field and line is read from the page. Each amount is checked to really appear on the page."],
  ["Tidied", "Dates, amounts and names are put in one format; required fields and arithmetic are checked."],
  ["Vendor found", "Matched to the vendor list by tax ID first, then by name."],
  ["PO found", "Linked to one purchase order by the number on the invoice, or by the best match."],
  ["Lines matched", "Each invoice line paired with a PO line; what was already billed on the PO is counted."],
  ["16 checks", "All 16 checks below run on every invoice, every time."],
  ["Decided", "Auto-approved, needs review, or rejected, with the reason in plain words and a link to the exact line."],
];

export default function HowItWorks() {
  const fixtures = useQuery({ queryKey: ["fixtures"], queryFn: api.fixtures, staleTime: 60_000 });
  const pol = useQuery({ queryKey: ["policy"], queryFn: api.policy, staleTime: 60_000 });
  const v = pol.data?.values;
  const usd = (c?: number) => (c == null ? "…" : money(c, "USD", { whole: c % 100 === 0 }));
  const limits: [string, string, string][] = v ? [
    ["Line price allowance", `${v.LINE_TOL_PCT}% of the line, at most ${usd(v.LINE_TOL_CAP_CENTS)}`, "R10"],
    ["PO overrun allowance", `${v.PO_TOL_PCT}% of the PO, at least ${usd(v.PO_TOL_FLOOR_CENTS)}`, "R12"],
    ["Rounding allowance on totals", usd(v.ARITH_TOL_CENTS), "R03"],
    ["Tax allowance", `${(v.TAX_TOL_PERMILLE / 10).toFixed(1)}% of the total, at least ${usd(v.TAX_TOL_FLOOR_CENTS)}`, "R14"],
    ["Extra quantity allowed over what was received", v.RECEIPT_TOL_MILLI ? String(v.RECEIPT_TOL_MILLI / 1000) : "None", "R11"],
    ["Approved automatically up to", usd(v.AUTO_APPROVE_CAP_CENTS), "R15"],
    ["Two approvers needed above", usd(v.DUAL_APPROVAL_CENTS), "R15"],
    ["Invoice too old after", `${v.STALE_INVOICE_DAYS} days`, "R16"],
  ] : [];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>About Invoice Cockpit</h1>
          <p>What the product is for, what it does, and how it decides.</p>
        </div>
        {pol.data && <span className="faint" style={{ fontSize: 13 }}>Policy {pol.data.version}</span>}
      </div>

      <section className="about-hero">
        <p className="about-lede">Invoice Cockpit checks every vendor invoice against its purchase order before anyone pays it. Clean invoices are approved on their own; the rest go to a person with the exact reason and the next step.</p>
        <p className="about-motto">The model reads. The rules decide. A person is accountable.</p>
      </section>

      <section className="acc-group">
        <h2>Behind the scenes</h2>
        <div className="acc-list">
          <Acc title="How an invoice is read" sub="Cheapest way first; the AI model only when needed">
            <ol className="acc-stack how-list">
              <li><b>Seen before?</b> Accepted formats: PDF (digital or scanned) and images (JPG, PNG, TIFF, BMP, GIF, WEBP); images are turned into a PDF first, and anything else is rejected with a clear reason. Every file gets a fingerprint (a SHA-256 hash of its bytes) when it arrives. This takes milliseconds and does not read the invoice. Each AI reading is saved under that fingerprint, so if the exact same file comes again, the saved reading is reused and re-checked. No AI cost.</li>
              <li><b>Read locally:</b> clean digital PDFs from known vendors are read on this server, for free. pdfplumber pulls the text from the page, and our own parser (Python patterns, with RapidFuzz for near-matches) finds the fields and line items. If every field is found and the totals add up, the AI is skipped.</li>
              <li><b>Read by AI:</b> everything else goes to Claude Opus 5.5 through Anthropic's API (OpenAI can be used instead). The AI only copies what is printed into a fixed form; it never calculates or decides.</li>
              <li><b>Double-checked:</b> every amount must appear on the page and the totals must add up. If not, the AI gets one more try; scans and photos are also read by OCR as a backup (Tesseract via pytesseract, after Pillow straightens and cleans the image).</li>
              <li><b>Still unclear?</b> The invoice goes to a person as unreadable. Nothing is guessed, and text inside an invoice that tries to give instructions is ignored.</li>
            </ol>
          </Acc>
          <Acc title="How emails are sorted" sub="Invoice, not an invoice, suspicious, or not sure; no AI used">
            <ul className="acc-stack how-list">
              <li><b>Invoice:</b> an invoice PDF or scan is attached. Each one is checked like an upload.</li>
              <li><b>Suspicious:</b> sender looks like a vendor but is not its address, asks to change bank details, replies go elsewhere, or a risky file is attached. Held; nothing is checked until a person releases it.</li>
              <li><b>Not sure:</b> mentions an invoice but none is attached (links are never opened), or the file is unclear. Held.</li>
              <li><b>Not an invoice:</b> statements, quotes, newsletters, internal mail. Archived.</li>
              <li><b>Matching keys:</b> PO number, vendor (sender's domain or name) and buyer (our company as bill-to). Two must agree, and one of them must be the vendor, for an email to count as confirmed.</li>
            </ul>
          </Acc>
          <Acc title="What a person always decides" sub="Things the system never does on its own">
            <ul className="acc-stack how-list">
              <li>Approving anything that failed a check, and anything above the auto-approval limit.</li>
              <li>Bank detail changes: verified by phone on the number on file, by two people.</li>
              <li>Sending emails to vendors: drafted for you, sent only when you click Send, and only to the address on the vendor list.</li>
              <li>Releasing a held email into the checks.</li>
              <li>The optional investigation assistant can look things up and suggest an action, but cannot approve or change anything.</li>
            </ul>
          </Acc>
          <Acc title="Alerts" sub="Slack, or the Notifications tab">
            <p className="acc-stack" style={{ margin: 0 }}>An alert is sent when an invoice needs review, is rejected or fails, and when an email is held. A summary of the day goes out at the set time. Without Slack, both are kept on the Notifications tab.</p>
          </Acc>
        </div>
      </section>

      <h2 className="about-divider">How it works</h2>

      <section className="acc-group">
        <h2>What happens to every invoice</h2>
        <ol className="flow">
          {STEPS.map(([t, d], i) => <li key={t}><span className="flow-n">{i + 1}</span><div><b>{t}</b><span>{d}</span></div></li>)}
        </ol>
      </section>

      <section className="acc-group">
        <h2>The 16 checks</h2>
        <div className="acc-list">
          {GROUPS.map((g) => (
            <Acc key={g.name} title={g.name} sub={g.sub} right={<span className="chip">{g.checks.length} check{g.checks.length > 1 ? "s" : ""}</span>}>
              <div className="table-wrap">
                <table className="t how-table">
                  <thead><tr><th>#</th><th>Check</th><th>Looks at</th><th>Fails when</th><th>Then</th></tr></thead>
                  <tbody>{g.checks.map(([id, name, looks, fails, then]) => (
                    <tr key={id}><td className="mono">{id}</td><td><b>{name}</b></td><td>{looks}</td><td>{fails}</td>
                      <td><span className={`chip ${then.startsWith("Rejected") ? "chip-bad" : "chip-review"}`}>{then}</span></td></tr>
                  ))}</tbody>
                </table>
              </div>
            </Acc>
          ))}
        </div>
      </section>

      <section className="acc-group">
        <h2>How the 16 results become one decision</h2>
        <div className="acc-list">
          <Acc title="The rules for deciding" sub="Approved only when every check passes; the most serious failure decides" open>
            <ul className="acc-stack how-list">
              <li><b>Auto-approved</b> only if all 16 checks pass and the total is within the auto-approval limit.</li>
              <li><b>Rejected</b> if any check that rejects fails (closed PO, PO of another vendor, wrong currency, over the PO, duplicate). A review result can never soften a rejection.</li>
              <li><b>Needs review</b> for everything else that fails, with the most important reason first.</li>
              <li>If a check that must run could not run (for example, nothing to compare), the invoice goes to review. Nothing is guessed.</li>
              <li>Every decision is saved with the exact numbers used, so the same invoice always gives the same answer.</li>
            </ul>
          </Acc>
          <Acc title="Purchase order balance" sub="Invoices waiting for review hold their amount on the PO">
            <p className="acc-stack" style={{ margin: 0 }}>Approved invoices and invoices waiting for review both count against the PO. That stops two people approving the same remaining amount twice. A rejected or returned invoice frees its amount.</p>
          </Acc>
        </div>
      </section>

      <section className="acc-group">
        <h2>Limits and allowances</h2>
        <div className="acc-list">
          <Acc title="The numbers the checks use" sub={v ? `Auto-approve up to ${usd(v.AUTO_APPROVE_CAP_CENTS)}, line allowance ${v.LINE_TOL_PCT}% (max ${usd(v.LINE_TOL_CAP_CENTS)})` : "…"}>
            <div className="acc-stack">
              <table className="t">
                <thead><tr><th>Limit</th><th>Value</th><th>Used by</th></tr></thead>
                <tbody>{limits.map(([n, val, r]) => <tr key={n}><td>{n}</td><td><b>{val}</b></td><td className="mono">{r}</td></tr>)}</tbody>
              </table>
              <p className="faint" style={{ margin: 0 }}>These are set in the policy file (cockpit/core/policy.py). Every decision records which policy version it used.</p>
            </div>
          </Acc>
        </div>
      </section>

      <section className="acc-group">
        <h2>Messy invoices it handles</h2>
        <div className="about-chips">
          {["Scanned and photographed invoices", "Missing invoice number or date", "The same invoice re-sent in a new layout", "Prices higher than agreed", "More billed than delivered", "One PO paid over several invoices", "Prices that already include tax", "One line covering several PO lines", "Wrong currency", "A PO from another vendor", "Unknown vendors", "Changed bank details", "Look-alike sender addresses", "Statements and quotes that are not invoices", "Instructions hidden inside a PDF"].map((x) => <span key={x} className="chip">{x}</span>)}
        </div>
        <p className="faint" style={{ margin: "10px 0 0", fontSize: 13 }}>{fixtures.data ? `${fixtures.data.length} sample invoices and 13 sample emails cover these cases; try them on the Invoices and Inbox tabs.` : ""}</p>
      </section>
    </>
  );
}
