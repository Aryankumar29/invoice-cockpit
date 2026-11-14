// In-memory mock of the HTTP API (VITE_MOCK=1). Follows docs/CONTRACT.md types exactly.
// Scenarios mirror cockpit/core/seed.py so the UI can be built without the backend.
// Runs "materialize" over time: each run has a planned event list with offsets, and
// both the stream and the polling endpoint only see events whose time has come.

import type { Api } from "./client";
import { ApiError } from "./client";
import type {
  AuditAction, Fixture, Outcome, RuleRow, RunCreated, RunDetail, RunSummary, Stage, StageEvent, StageStatus, Stats,
} from "./types";

const POLICY_VERSION = "2026.10-demo.2";
const Q = 1000;
const STAGE_GAP_MS = 400;

type ILine = { description: string; qty: number; unit: number; amount?: number; sku?: string };
type PLine = { line_no: number; description: string; qty: number; unit: number; received?: number; sku?: string };

type Scenario = {
  filename: string;
  kind: "digital" | "scan";
  vendor: string;
  vendor_id: string | null;
  vendor_status?: "matched" | "unknown" | "tax_conflict";
  tax_id: string;
  invoice_number: string | null;
  invoice_date: string;
  currency: string;
  po: string | null;
  po_currency?: string;
  po_status?: "open" | "closed";
  po_lines: PLine[];
  lines: ILine[];
  map?: (number | null)[];
  billed_before?: number;
  prior_qty?: Record<number, number>;
  prior_runs?: string[];
  tax?: number;
  source: "claude" | "local_text" | "local_ocr" | "replay" | "none";
  bank_change?: boolean;
  dup?: { kind: "exact" | "near" | "none"; run_id?: string; invoice_number?: string; score?: number; signals?: string[] };
  readable?: boolean;
  intake_error?: string;
  warnings?: string[];
  explanation?: string;
};

const usd = (c: number) =>
  (c < 0 ? "-$" : "$") + (Math.abs(c) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const hex = (n: number) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join("");

// ---------------------------------------------------------------- scenarios

const SCENARIOS: Record<string, Scenario> = {
  H1: {
    filename: "H1_northwind_INV-H-001.pdf", kind: "digital", vendor: "Northwind Supply Co.", vendor_id: "V-NW",
    tax_id: "94-1234567", invoice_number: "INV-H-001", invoice_date: "2026-10-20", currency: "USD", po: "PO-1042",
    po_lines: [
      { line_no: 1, description: "Widget A, steel, 10 mm", qty: 20, unit: 10_000, sku: "WA-10" },
      { line_no: 2, description: "Mounting bracket B", qty: 40, unit: 4_500, sku: "MB-02" },
      { line_no: 3, description: "Fastener kit, 100 pc", qty: 10, unit: 10_000, sku: "FK-100" },
    ],
    lines: [
      { description: "Widget A, steel, 10 mm", qty: 20, unit: 10_000, sku: "WA-10" },
      { description: "Mounting bracket B", qty: 40, unit: 4_500, sku: "MB-02" },
      { description: "Fastener kit, 100 pc", qty: 10, unit: 10_000, sku: "FK-100" },
    ],
    source: "claude",
    explanation:
      "Northwind Supply Co. billed $4,800.00 against PO-1042. All three lines match the PO at the agreed prices and within received quantities, the totals add up, and nothing resembles an earlier invoice. The invoice is within the $10,000.00 auto-approval cap, so it was released to the payment queue without a human touch.",
  },
  S1: {
    filename: "S1_globex_INV-S-001.pdf", kind: "digital", vendor: "Globex Services LLC", vendor_id: "V-GX",
    tax_id: "36-7654321", invoice_number: "INV-S-001", invoice_date: "2026-10-10", currency: "USD", po: "PO-1047",
    po_lines: [{ line_no: 1, description: "Implementation consulting (day rate)", qty: 10, unit: 100_000, sku: "SVC-DAY" }],
    lines: [{ description: "Implementation consulting - phase 1 (6 days)", qty: 6, unit: 100_000, sku: "SVC-DAY" }],
    source: "claude",
    explanation:
      "Globex billed 6 of the 10 consulting days on PO-1047 at the PO day rate. This is a partial invoice, so the system checked it against the PO balance rather than the PO total: $4,000.00 remains for later invoices. Auto-approved.",
  },
  S2: {
    filename: "S2_globex_INV-S-002.pdf", kind: "digital", vendor: "Globex Services LLC", vendor_id: "V-GX",
    tax_id: "36-7654321", invoice_number: "INV-S-002", invoice_date: "2026-10-22", currency: "USD", po: "PO-1047",
    po_lines: [{ line_no: 1, description: "Implementation consulting (day rate)", qty: 10, unit: 100_000, sku: "SVC-DAY" }],
    lines: [{ description: "Implementation consulting - phase 2 (4 days)", qty: 4, unit: 100_000, sku: "SVC-DAY" }],
    billed_before: 600_000, prior_qty: { 1: 6 * Q }, prior_runs: ["S1"],
    source: "claude",
    explanation:
      "Globex billed the remaining 4 consulting days on PO-1047. Together with INV-S-001 the PO is now fully billed ($0.00 remaining), and no line exceeds what was received. Auto-approved.",
  },
  D1: {
    filename: "D1_northwind_INV-D-001.pdf", kind: "digital", vendor: "Northwind Supply Co.", vendor_id: "V-NW",
    tax_id: "94-1234567", invoice_number: "INV-D-001", invoice_date: "2026-10-20", currency: "USD", po: "PO-1051",
    po_lines: [
      { line_no: 1, description: "Widget A, steel, 10 mm", qty: 40, unit: 10_000, sku: "WA-10" },
      { line_no: 2, description: "Widget B, aluminium, 12 mm", qty: 20, unit: 10_000, sku: "WB-12" },
    ],
    lines: [
      { description: "Widget A, steel, 10 mm", qty: 40, unit: 10_000, sku: "WA-10" },
      { description: "Widget B, aluminium, 12 mm", qty: 20, unit: 10_300, sku: "WB-12" },
    ],
    source: "claude",
    explanation:
      "Line 2 (Widget B) is billed at $103.00 a unit against a PO price of $100.00, which is $60.00 over on the line. The line tolerance is the smaller of 2% and $50.00, so $40.00 is allowed. The invoice as a whole is within the PO allowance, which is why a header-only check would have passed it. A reviewer should request a $60.00 credit note or approve the variance with a reason.",
  },
  E1: {
    filename: "E1_northwind_INV-E-001.pdf", kind: "digital", vendor: "Northwind Supply Co", vendor_id: "V-NW",
    tax_id: "94-1234567", invoice_number: "NW-2026-001", invoice_date: "2026-10-09", currency: "USD", po: "PO-1038",
    po_lines: [
      { line_no: 1, description: "Widget A, steel, 10 mm", qty: 30, unit: 10_000, sku: "WA-10" },
      { line_no: 2, description: "Fastener kit, 100 pc", qty: 18, unit: 10_000, sku: "FK-100" },
    ],
    lines: [
      { description: "Widget A - steel 10mm", qty: 30, unit: 10_000, sku: "WA-10" },
      { description: "Fastener kit (100 pcs)", qty: 18, unit: 10_000, sku: "FK-100" },
    ],
    billed_before: 480_000, prior_qty: { 1: 30 * Q, 2: 18 * Q }, prior_runs: ["run-0007"],
    dup: { kind: "exact", run_id: "run-0007", invoice_number: "NW-2026-001", score: 1, signals: ["same_reference_digits", "same_vendor", "same_amount", "same_date"] },
    source: "claude",
    explanation:
      "This is a second copy of invoice NW-2026-001, which was approved on 12 Oct and paid on 18 Oct. The layout differs, so the file hash is new, but the vendor, invoice number, amount and date are the same. PO-1038 is already fully billed. Rejected so the same obligation is not paid twice.",
  },
  E2: {
    filename: "E2_acme_scan.pdf", kind: "scan", vendor: "Acme Industrial Inc.", vendor_id: "V-AC",
    tax_id: "27-1111111", invoice_number: null, invoice_date: "2026-10-21", currency: "USD", po: "PO-1046",
    po_lines: [{ line_no: 1, description: "Hydraulic hose assembly 1/2in", qty: 30, unit: 10_000, sku: "HH-050" }],
    lines: [{ description: "Hydraulic hose assembly 1/2in", qty: 30, unit: 10_000, sku: "HH-050" }],
    source: "claude",
    warnings: ["Scanned page: no text layer, amounts checked by arithmetic only", "Invoice number not found on the document"],
    explanation:
      "The scanned Acme invoice has no invoice number. Vendor, PO-1046 and the $3,000.00 total were read and reconcile, but without a number the system cannot rule out that this invoice was already paid under another reference. It will not invent a number. Return it to the vendor for a numbered copy, or enter the number from the original.",
  },
  BANK: {
    filename: "R5_globex_bank_change.pdf", kind: "digital", vendor: "Globex Services LLC", vendor_id: "V-GX",
    tax_id: "36-7654321", invoice_number: "INV-G-2210", invoice_date: "2026-10-19", currency: "USD", po: "PO-1080",
    po_lines: [{ line_no: 1, description: "Implementation consulting (day rate)", qty: 15, unit: 100_000, sku: "SVC-DAY" }],
    lines: [{ description: "Implementation consulting (15 days)", qty: 15, unit: 100_000, sku: "SVC-DAY" }],
    bank_change: true, source: "local_text",
    warnings: ["No API key: local extractor used"],
    explanation:
      "The PO match is clean, but the remittance account on this invoice differs from the one on file for Globex. A changed bank account on a $15,000.00 invoice is the classic payment-fraud pattern, so two different reviewers must approve after the change is confirmed by phone with a known Globex contact.",
  },
  CURR: {
    filename: "R8_acme_eur_invoice.pdf", kind: "digital", vendor: "Acme Industrial Inc.", vendor_id: "V-AC",
    tax_id: "27-1111111", invoice_number: "AC-5102", invoice_date: "2026-10-14", currency: "USD", po: "PO-1070", po_currency: "EUR",
    po_lines: [{ line_no: 1, description: "Hydraulic hose assembly 1/2in", qty: 10, unit: 10_000, sku: "HH-050" }],
    lines: [{ description: "Hydraulic hose assembly 1/2in", qty: 10, unit: 10_000, sku: "HH-050" }],
    source: "replay",
    warnings: ["Replayed: extraction served from the replay cache"],
    explanation: "The invoice is in USD but PO-1070 was raised in EUR. Paying it would settle the obligation in the wrong currency. Rejected.",
  },
  NW007: {
    filename: "NW-2026-001.pdf", kind: "digital", vendor: "Northwind Supply Co.", vendor_id: "V-NW",
    tax_id: "94-1234567", invoice_number: "NW-2026-001", invoice_date: "2026-10-09", currency: "USD", po: "PO-1038",
    po_lines: [
      { line_no: 1, description: "Widget A, steel, 10 mm", qty: 30, unit: 10_000, sku: "WA-10" },
      { line_no: 2, description: "Fastener kit, 100 pc", qty: 18, unit: 10_000, sku: "FK-100" },
    ],
    lines: [
      { description: "Widget A, steel, 10 mm", qty: 30, unit: 10_000, sku: "WA-10" },
      { description: "Fastener kit, 100 pc", qty: 18, unit: 10_000, sku: "FK-100" },
      { description: "Expedited freight", qty: 1, unit: 0, amount: 0 },
    ],
    map: [1, 2, null],
    source: "claude",
    explanation: "Line 3 (expedited freight, $0.00) does not appear on PO-1038. Everything else matches. A reviewer confirmed the line is a zero-value courtesy note and approved.",
  },
  RET: {
    filename: "acme_AC-4471.pdf", kind: "digital", vendor: "Acme Industrial Inc.", vendor_id: "V-AC",
    tax_id: "27-1111111", invoice_number: "AC-4471", invoice_date: "2026-10-16", currency: "USD", po: null,
    po_lines: [], lines: [{ description: "Emergency pump repair (call-out)", qty: 1, unit: 85_000, sku: "SVC-CALL" }],
    source: "claude",
    explanation: "No purchase order is referenced and no open Acme PO fits this call-out repair. Non-PO invoices need a budget owner. The reviewer returned it to Acme to quote a PO number.",
  },
  GX5: {
    filename: "globex_INV-G-2190.pdf", kind: "digital", vendor: "Globex Services LLC", vendor_id: "V-GX",
    tax_id: "36-7654321", invoice_number: "INV-G-2190", invoice_date: "2026-10-12", currency: "USD", po: "PO-1080",
    po_lines: [{ line_no: 1, description: "Implementation consulting (day rate)", qty: 15, unit: 100_000, sku: "SVC-DAY" }],
    lines: [{ description: "Implementation consulting (5 days)", qty: 5, unit: 100_000, sku: "SVC-DAY" }],
    source: "claude",
    explanation: "Globex billed 5 of the 15 consulting days on PO-1080 at the PO rate. All 16 checks passed and $10,000.00 remains on the PO. Auto-approved.",
  },
  ENC: {
    filename: "scan_0193_locked.pdf", kind: "digital", vendor: "", vendor_id: null, tax_id: "",
    invoice_number: null, invoice_date: "", currency: "", po: null, po_lines: [], lines: [],
    source: "none", readable: false, intake_error: "The PDF is password-protected, so its pages cannot be read.",
  },
};

const DEMO_FIXTURES: Fixture[] = [
  { key: "H1", file: "pdf/H1_northwind_INV-H-001.pdf", kind: "digital", demo: true, order: 1, title: "Happy path",
    description: "Northwind INV-H-001, $4,800 against PO-1042. Three lines, exact match.",
    expected: { outcome: "auto_approved", reason: "STP_CLEAN" }, prerequisites: [] },
  { key: "S1", file: "pdf/S1_globex_INV-S-001.pdf", kind: "digital", demo: true, order: 2, title: "Split PO, first invoice",
    description: "Globex bills 6 of 10 consulting days on PO-1047. $4,000 should remain.",
    expected: { outcome: "auto_approved", reason: "STP_CLEAN" }, prerequisites: [] },
  { key: "S2", file: "pdf/S2_globex_INV-S-002.pdf", kind: "digital", demo: true, order: 3, title: "Split PO, final invoice",
    description: "The remaining 4 days. Checked against the PO balance, not the PO total.",
    expected: { outcome: "auto_approved", reason: "STP_CLEAN" }, prerequisites: ["S1"] },
  { key: "D1", file: "pdf/D1_northwind_INV-D-001.pdf", kind: "digital", demo: true, order: 4, title: "Price variance on one line",
    description: "Line 2 is $60 over the PO price. The invoice total is within PO tolerance.",
    expected: { outcome: "needs_review", reason: "PRICE_VARIANCE" }, prerequisites: [] },
  { key: "E1", file: "pdf/E1_northwind_INV-E-001.pdf", kind: "digital", demo: true, order: 5, title: "Duplicate on a new template",
    description: "A re-sent copy of NW-2026-001 (already paid) with a different layout and file hash.",
    expected: { outcome: "rejected", reason: "DUPLICATE_INVOICE" }, prerequisites: [] },
  { key: "E2", file: "pdf/E2_acme_scan.pdf", kind: "scan", demo: true, order: 6, title: "Scanned, no invoice number",
    description: "Acme scan with vendor, PO and total readable but no invoice number.",
    expected: { outcome: "needs_review", reason: "MISSING_INVOICE_NUMBER" }, prerequisites: [] },
  { key: "BANK", file: "pdf/R5_globex_bank_change.pdf", kind: "digital", demo: false, order: null, title: "Bank account changed",
    description: "Clean PO match, but the remittance account differs from the vendor master.",
    expected: { outcome: "needs_review", reason: "BANK_CHANGE" }, prerequisites: [] },
  { key: "CURR", file: "pdf/R8_acme_eur_invoice.pdf", kind: "digital", demo: false, order: null, title: "Currency mismatch",
    description: "USD invoice against a EUR purchase order.",
    expected: { outcome: "rejected", reason: "CURRENCY_MISMATCH" }, prerequisites: [] },
  { key: "ENC", file: "pdf/R1_encrypted.pdf", kind: "digital", demo: false, order: null, title: "Password-protected PDF",
    description: "Intake cannot open the file.",
    expected: { outcome: "failed", reason: "DOCUMENT_UNREADABLE" }, prerequisites: [] },
];

// ---------------------------------------------------------------- rule engine (mirror of cockpit/core)

const SEVERITY: Record<string, number> = { auto_approved: 0, approved: 1, needs_review: 2, rejected: 3, failed: 4 };
const MANDATORY = new Set(["R01", "R02", "R03", "R04", "R06", "R09", "R10", "R11", "R12", "R13"]);
const ACTIONS: Record<string, string> = {
  STP_CLEAN: "Release to payment queue", PRICE_VARIANCE: "Request a credit note",
  DUPLICATE_INVOICE: "Reject and link the original", MISSING_INVOICE_NUMBER: "Return to vendor",
  BANK_CHANGE: "Verify bank change out-of-band (dual control)", CURRENCY_MISMATCH: "Reject: currency differs from PO",
  LINE_UNVERIFIED: "Map the unmatched line or request a credit", PO_MISSING: "Route to budget owner (non-PO)",
  DOCUMENT_UNREADABLE: "Return to vendor", INCOMPLETE_EVIDENCE: "Review manually",
};

const row = (rule: string, title: string, status: RuleRow["status"], message: string, extra: Partial<RuleRow> = {}): RuleRow =>
  ({ rule, title, status, message, tier: 0, outcome: null, reason: null, evidence: {}, ...extra });
const fail = (rule: string, title: string, message: string, tier: number, outcome: string, reason: string, evidence: Record<string, unknown> = {}) =>
  row(rule, title, "fail", message, { tier, outcome, reason, evidence });

function evaluate(s: Scenario) {
  const inv = s.lines.map((l) => ({ ...l, amount: l.amount ?? l.qty * l.unit }));
  const sub = inv.reduce((a, l) => a + l.amount, 0);
  const tax = s.tax ?? 0;
  const total = sub + tax;
  const po = s.po ? s.po_lines : null;
  const poTotal = (s.po_lines ?? []).reduce((a, l) => a + l.qty * l.unit, 0);
  const map = s.map ?? inv.map((_, i) => (po && i < po.length ? po[i].line_no : null));
  const rules: RuleRow[] = [];

  if (s.readable === false) {
    rules.push(fail("R01", "Document readable", "The document could not be read by any extraction route.", 0, "needs_review", "DOCUMENT_UNREADABLE"));
    const titles = ["Required fields present", "Totals reconcile", "Vendor approved", "Bank details verified", "PO exists and is open",
      "PO vendor matches", "Currency matches", "Lines map to PO", "Price within line tolerance", "Quantity within receipt",
      "Within PO remaining", "Not a duplicate", "Tax recalculates", "Within approval authority", "Due date valid"];
    titles.forEach((t, i) => rules.push(row(`R${String(i + 2).padStart(2, "0")}`, t, "cannot_run", "Document unreadable.")));
    return { rules, total: null as number | null, sub, tax, map, lineDetails: [], poTotal };
  }
  rules.push(row("R01", "Document readable", "pass", "Document text was extracted."));
  rules.push(s.invoice_number
    ? row("R02", "Required fields present", "pass", "All required header fields are present.")
    : fail("R02", "Required fields present", "Missing: invoice_number. The system will not invent a value.", 3, "needs_review", "MISSING_INVOICE_NUMBER", { missing: ["invoice_number"] }));
  rules.push(row("R03", "Totals reconcile", "pass", `Lines + tax + freight = ${usd(total)}, matches the stated total.`,
    { evidence: { line_sum: sub, tax, freight: 0, computed_total: total, stated_total: total, difference: 0, tolerance: 2, bad_lines: [] } }));
  rules.push(row("R04", "Vendor approved", "pass", `${s.vendor.replace(/ Co$/, " Co.")} matched by tax_id; status active.`,
    { evidence: { vendor_id: s.vendor_id, method: "tax_id", score: 100 } }));
  rules.push(s.bank_change
    ? fail("R05", "Bank details verified", "Remittance account differs from the vendor master. Verify out-of-band before payment.", 1, "needs_review", "BANK_CHANGE", { dual_control: true })
    : row("R05", "Bank details verified", "pass", "Remittance account matches the vendor master."));
  if (!po) {
    rules.push(fail("R06", "PO exists and is open", "No purchase order could be matched safely; routed to non-PO review.", 2, "needs_review", "PO_MISSING"));
    ["R07:PO vendor matches:Needs both a resolved vendor and a PO.", "R08:Currency matches:Needs a PO and an invoice currency.",
     "R09:Lines map to PO:No PO to map lines against.", "R10:Price within line tolerance:No mapped lines.",
     "R11:Quantity within receipt:No mapped lines.", "R12:Within PO remaining:No PO."].forEach((x) => {
      const [r, t, m] = x.split(":");
      rules.push(row(r, t, "not_applicable", m));
    });
  } else {
    rules.push(row("R06", "PO exists and is open", "pass", `${s.po} found by exact reference; status open.`, { evidence: { po_number: s.po } }));
    rules.push(row("R07", "PO vendor matches", "pass", `${s.po} was issued to ${s.vendor}.`));
    rules.push(s.po_currency && s.po_currency !== s.currency
      ? fail("R08", "Currency matches", `Invoice is in ${s.currency}, PO is in ${s.po_currency}.`, 1, "rejected", "CURRENCY_MISMATCH")
      : row("R08", "Currency matches", "pass", `Both in ${s.currency}.`));
    const unmatched = map.map((m, i) => (m == null ? i + 1 : null)).filter((x) => x != null);
    rules.push(unmatched.length
      ? fail("R09", "Lines map to PO", `Invoice line(s) [${unmatched.join(", ")}] do not correspond to any PO line.`, 3, "needs_review", "LINE_UNVERIFIED", { unmatched })
      : row("R09", "Lines map to PO", "pass", `All ${inv.length} invoice lines mapped to PO lines.`));
  }
  const lineDetails = po ? inv.flatMap((il, i) => {
    const pl = po.find((p) => p.line_no === map[i]);
    if (!pl) return [];
    const expected = il.qty * pl.unit;
    const variance = il.amount - expected;
    const allowed = Math.min(Math.floor((Math.abs(expected) * 2) / 100), 5_000);
    return [{ invoice_line: i + 1, po_line: pl.line_no, invoice_unit: il.unit, po_unit: pl.unit, expected, billed: il.amount, variance, allowed, ok: Math.abs(variance) <= allowed }];
  }) : [];
  if (po) {
    const bad = lineDetails.filter((d) => !d.ok).sort((a, b) => (Math.abs(b.variance) - b.allowed) - (Math.abs(a.variance) - a.allowed))[0];
    rules.push(bad
      ? fail("R10", "Price within line tolerance", `Line ${bad.invoice_line} is ${usd(Math.abs(bad.variance))} ${bad.variance > 0 ? "above" : "below"} the PO line; the allowed variance is ${usd(bad.allowed)}.`,
          4, "needs_review", "PRICE_VARIANCE", { lines: lineDetails, worst: bad, formula: "min(2% of expected line amount, $50.00)" })
      : row("R10", "Price within line tolerance", "pass", "Every line is within min(2%, $50) of the PO price.", { evidence: { lines: lineDetails } }));
    const qtyRows = po.filter((pl) => map.includes(pl.line_no)).map((pl) => {
      const thisQty = inv.reduce((a, il, i) => a + (map[i] === pl.line_no ? il.qty * Q : 0), 0);
      const prior = s.prior_qty?.[pl.line_no] ?? 0;
      return { po_line: pl.line_no, prior_billed_qty: prior, this_qty: thisQty, cumulative: prior + thisQty, received: (pl.received ?? pl.qty) * Q, ordered: pl.qty * Q };
    });
    const over = qtyRows.find((r) => r.cumulative > r.received);
    rules.push(over
      ? fail("R11", "Quantity within receipt", `PO line ${over.po_line}: cumulative billed ${over.cumulative / Q} exceeds received ${over.received / Q}.`, 2, "needs_review", "QTY_VARIANCE", { lines: qtyRows })
      : row("R11", "Quantity within receipt", "pass", "Cumulative billed quantity is within received quantity on every line.", { evidence: { lines: qtyRows } }));
    const billed = s.billed_before ?? 0;
    const remaining = poTotal - billed;
    const allowance = Math.max(Math.floor((poTotal * 2) / 100), 5_000);
    const after = remaining - sub;
    const ev = { po_total: poTotal, billed_before: billed, remaining_before: remaining, this_invoice: sub, remaining_after: after, allowance, prior_runs: s.prior_runs ?? [], formula: "max(2% of PO total, $50.00)" };
    rules.push(sub > remaining + allowance
      ? fail("R12", "Within PO remaining", `Invoice ${usd(sub)} exceeds the ${usd(remaining)} remaining on ${s.po} by ${usd(sub - remaining)}; allowance is ${usd(allowance)}.`, 2, "rejected", "OVER_PO_REMAINING", ev)
      : row("R12", "Within PO remaining", "pass", `${usd(sub)} against ${usd(remaining)} remaining; ${usd(Math.max(after, 0))} left after.${remaining > 0 && after <= 0 ? " This is the final amount against the PO." : ""}`, { evidence: ev }));
  }
  const d = s.dup;
  if (d?.kind === "exact") {
    rules.push(fail("R13", "Not a duplicate", `Same obligation as ${d.invoice_number} (run ${d.run_id}); matched on ${d.signals?.join(", ")}.`, 1, "rejected", "DUPLICATE_INVOICE",
      { hits: [{ run_id: d.run_id, invoice_number: d.invoice_number, score: d.score, signals: d.signals }] }));
  } else if (!s.invoice_number) {
    rules.push(row("R13", "Not a duplicate", "cannot_run", "Invoice-number check cannot run without a number; file-hash and amount/date checks passed."));
  } else {
    rules.push(row("R13", "Not a duplicate", "pass", "No prior invoice matches by file, number, or amount/date/lines."));
  }
  rules.push(row("R14", "Tax recalculates", "not_applicable", "No PO tax rate to recalculate against."));
  rules.push(total > 1_000_000
    ? fail("R15", "Within approval authority", `${usd(total)} is above the $10,000.00 auto-approval cap; a named approver must sign off.`, 5, "needs_review", "APPROVAL_REQUIRED", { dual_control: total > 5_000_000 })
    : row("R15", "Within approval authority", "pass", `${usd(total)} is within the auto-approval cap.`));
  rules.push(row("R16", "Due date valid", "pass", `Invoice dated ${Math.max(1, Math.round((Date.parse("2026-10-26") - Date.parse(s.invoice_date)) / 864e5))} days ago; due date consistent.`));
  return { rules, total, sub, tax, map, lineDetails, poTotal };
}

function adjudicate(rules: RuleRow[]): { outcome: Outcome; reasons: string[]; deciding: RuleRow[] } {
  const fails = rules.filter((r) => r.status === "fail" || r.status === "error");
  if (fails.length) {
    const outcome = fails.map((r) => r.outcome!).sort((a, b) => SEVERITY[b] - SEVERITY[a])[0] as Outcome;
    const deciding = fails.filter((r) => r.outcome === outcome).sort((a, b) => a.tier - b.tier || a.rule.localeCompare(b.rule));
    return { outcome, reasons: [...new Set(deciding.map((r) => r.reason!))], deciding };
  }
  const gaps = rules.filter((r) => MANDATORY.has(r.rule) && r.status === "cannot_run");
  if (gaps.length) return { outcome: "needs_review", reasons: ["INCOMPLETE_EVIDENCE"], deciding: gaps };
  return { outcome: "auto_approved", reasons: ["STP_CLEAN"], deciding: [] };
}

// ---------------------------------------------------------------- run construction

type Planned = { at: number; stage: Stage; status: StageStatus; title: string; message: string; data: Record<string, unknown> };

type MockRun = {
  detail: RunDetail;
  start: number;
  plan: Planned[];
  fileUrl: string;
  memoUrl: string;
  fixtureKey?: string;
  dual: boolean;
};

const STAGE_TITLES: Record<Stage, string> = {
  intake: "Received", extraction: "Read the document", validation: "Check fields and totals", vendor: "Check the vendor",
  po: "Find the purchase order", matching: "Match lines", policy: "Apply policy", decision: "Decide", explanation: "Explain the decision",
};

function buildRun(id: string, s: Scenario, start: number, fixtureKey?: string): MockRun {
  const ev = evaluate(s);
  const unreadable = s.readable === false;
  const { outcome, reasons, deciding } = unreadable
    ? { outcome: "failed" as Outcome, reasons: ["DOCUMENT_UNREADABLE"], deciding: [] as RuleRow[] }
    : adjudicate(ev.rules);
  const r12 = ev.rules.find((r) => r.rule === "R12")?.evidence as { remaining_after?: number } | undefined;
  const verdict = unreadable ? s.intake_error!
    : outcome === "auto_approved"
      ? `Auto-approved: all 16 checks passed.${s.po && r12?.remaining_after != null ? ` ${usd(r12.remaining_after)} remains on ${s.po}.` : ""}`
      : deciding.map((r) => r.message).join(" ");
  const pages = s.kind === "scan" ? 1 : 2;
  const size = 38 + Math.floor(Math.random() * 300);
  const fileHash = hex(64);
  const invoice = unreadable ? {} : {
    invoice_number: s.invoice_number, invoice_date: s.invoice_date, vendor_name: s.vendor, currency: s.currency,
    total_cents: ev.total, subtotal_cents: ev.sub, tax_cents: ev.tax, freight_cents: 0, due_date: "2026-11-19",
    vendor_tax_id: s.tax_id, po_reference: s.po, bank_hash: s.bank_change ? hex(16) : hex(16), file_hash: fileHash, readable: true,
    lines: s.lines.map((l) => ({ description: l.description, qty_milli: l.qty * Q, unit_price_cents: l.unit, amount_cents: l.amount ?? l.qty * l.unit, sku: l.sku ?? null })),
  };
  const fieldsFound = unreadable ? 0 : 11 - (s.invoice_number ? 0 : 1);
  const grounding = unreadable ? { checked: 0, grounded: 0, ungrounded: [], arithmetic_ok: false }
    : s.kind === "scan" ? { checked: 0, grounded: 0, ungrounded: [], arithmetic_ok: true }
    : { checked: 4 + s.lines.length * 2, grounded: 4 + s.lines.length * 2, ungrounded: [], arithmetic_ok: true };
  const warnings = [...(s.warnings ?? [])];
  const dup = s.dup?.kind === "exact"
    ? { kind: "exact", ref_check_ran: true, hits: [{ run_id: s.dup.run_id, invoice_number: s.dup.invoice_number, score: s.dup.score, signals: s.dup.signals }] }
    : { kind: "none", ref_check_ran: !!s.invoice_number, hits: [] };
  const vendorMatch = unreadable ? { status: "unknown", vendor: null, score: 0, method: "", candidates: [] }
    : { status: "matched", vendor: s.vendor_id, score: 100, method: "tax_id", candidates: [[s.vendor_id, 100]] };
  const poMatch = !s.po ? { status: "missing", po: null, score: 0.41, method: "vendor_open_po", candidates: [] }
    : { status: "exact", po: s.po, score: 1, method: "exact_ref", candidates: [[s.po, 1]] };
  const lineMatches = s.po ? ev.map.map((m, i) => ({ inv_index: i, po_line_no: m, score: m == null ? 0 : 0.96 })) : [];
  const allocations = s.po ? ev.map.flatMap((m, i) => (m == null ? [] : [{ po_line_no: m, qty_milli: s.lines[i].qty * Q, amount_cents: s.lines[i].amount ?? s.lines[i].qty * s.lines[i].unit }])) : [];

  // Planned stage events
  const plan: Planned[] = [];
  let t = 0;
  const add = (stage: Stage, status: StageStatus, message: string, data: Record<string, unknown> = {}, dur = 180) => {
    plan.push({ at: t, stage, status: "started", title: STAGE_TITLES[stage], message: "", data: {} });
    t += dur;
    plan.push({ at: t, stage, status, title: STAGE_TITLES[stage], message, data: { ...data, ms: dur } });
    t += STAGE_GAP_MS - 180;
  };
  if (unreadable) {
    add("intake", "failed", s.intake_error!, { filename: s.filename, size_kb: size, sha256: fileHash, error: "PDF_ENCRYPTED" }, 120);
    (["extraction", "validation", "vendor", "po", "matching", "policy", "decision", "explanation"] as Stage[]).forEach((st) => {
      plan.push({ at: t, stage: st, status: "stopped", title: STAGE_TITLES[st], message: "Not run: intake failed.", data: {} });
    });
  } else {
    const failing = (ids: string[]) => ev.rules.filter((r) => ids.includes(r.rule) && r.status === "fail");
    add("intake", "ok", `${pages} page${pages > 1 ? "s" : ""} · ${size} KB · ${s.kind === "scan" ? "scanned image" : "PDF with text layer"}`,
      { filename: s.filename, pages, size_kb: size, sha256: fileHash, text_layer: s.kind !== "scan" }, 90);
    const attempts = s.source === "local_text"
      ? [{ step: "claude", ok: false, detail: "skipped: no API key", ms: 0 }, { step: "local_text", ok: true, detail: "pdfplumber text + table parser", ms: 640 }]
      : s.source === "replay"
        ? [{ step: "claude", ok: false, detail: "timeout after 20 s", ms: 20000 }, { step: "replay", ok: true, detail: "cache hit by file hash", ms: 4 }]
        : [{ step: "claude", ok: true, detail: "claude-opus-5-5, structured output", ms: s.kind === "scan" ? 2310 : 1620 }];
    add("extraction", warnings.length || !s.invoice_number ? "warn" : "ok",
      `${fieldsFound} fields · ${s.lines.length} line item${s.lines.length > 1 ? "s" : ""} · ${s.kind === "scan" ? "from page image" : "grounded in text layer"}`,
      { source: s.source, fields: fieldsFound, lines: s.lines.length, grounding, attempts, warnings }, s.kind === "scan" ? 1500 : 1100);
    const v = failing(["R02", "R03"]);
    add("validation", v.length ? "warn" : "ok", v.length ? v.map((r) => r.message).join(" ") : `Required fields present · totals reconcile to ${usd(ev.total!)}`,
      { R02: ev.rules[1].status, R03: ev.rules[2].status, total_cents: ev.total });
    const vf = failing(["R04", "R05"]);
    add("vendor", vf.length ? "warn" : "ok", vf.length ? vf.map((r) => r.message).join(" ") : `${s.vendor} · matched by tax ID · bank unchanged`, vendorMatch, 120);
    if (s.po) {
      const r12e = ev.rules.find((r) => r.rule === "R12")!.evidence as { remaining_before: number };
      const pf = failing(["R06", "R07", "R08"]);
      add("po", pf.length ? "warn" : "ok", pf.length ? pf.map((r) => r.message).join(" ") : `${s.po} · open · ${usd(r12e.remaining_before)} remaining before this invoice`, { ...poMatch, remaining_before: r12e.remaining_before }, 160);
      const mf = failing(["R09", "R10", "R11"]);
      const matched = ev.map.filter((m) => m != null).length;
      add("matching", mf.length ? "warn" : "ok", mf.length ? mf.map((r) => r.message).join(" ") : `${matched} of ${s.lines.length} lines match · prices within tolerance`, { line_matches: lineMatches, lines: ev.lineDetails }, 210);
    } else {
      add("po", "warn", "No purchase order referenced, and no open PO for this vendor fits.", poMatch, 160);
      plan.push({ at: t, stage: "matching", status: "stopped", title: STAGE_TITLES.matching, message: "Skipped: no PO to match lines against.", data: {} });
      t += 60;
    }
    const counts = ev.rules.reduce<Record<string, number>>((a, r) => ({ ...a, [r.status]: (a[r.status] ?? 0) + 1 }), {});
    add("policy", counts.fail ? "warn" : "ok",
      `${counts.pass ?? 0} passed · ${counts.fail ?? 0} failed · ${(counts.cannot_run ?? 0) + (counts.not_applicable ?? 0)} not run`, { ...counts, policy_version: POLICY_VERSION }, 90);
    add("decision", outcome === "auto_approved" ? "ok" : "warn", verdict, { outcome, reasons, recommended_action: ACTIONS[reasons[0]] ?? "Review manually" }, 40);
    add("explanation", "ok", "Plain-English explanation and decision memo ready.", { mode: "template", invariants_ok: true }, 120);
  }

  const total_ms = t;
  const detail: RunDetail = {
    id, created_at: new Date(start).toISOString(), filename: s.filename, status: "queued", outcome: "processing", reasons: [],
    vendor_name: unreadable ? null : s.vendor, invoice_number: s.invoice_number, total_cents: ev.total, currency: unreadable ? null : s.currency,
    po_number: s.po, extraction_source: unreadable ? null : s.source, duration_ms: total_ms,
    stages: [], verdict, recommended_action: ACTIONS[reasons[0]] ?? "Review manually", explanation: s.explanation ?? verdict,
    rules: ev.rules, invoice, line_matches: lineMatches, allocations, duplicates: dup, vendor_match: vendorMatch, po_match: poMatch,
    grounding, warnings, packet_hash: unreadable ? "" : hex(64), policy_version: POLICY_VERSION, actions: [],
    allowed_actions: outcome === "needs_review" ? ["approve", "reject", "return_to_vendor", "escalate"] : [],
  };
  // Final values are stored apart and revealed when the run completes.
  (detail as RunDetail & { _final?: unknown })._final = { outcome, reasons };
  const dual = ev.rules.some((r) => r.status === "fail" && r.evidence.dual_control === true) || (ev.total ?? 0) > 5_000_000;
  return { detail, start, plan, fileUrl: makePdf(s, ev.total), memoUrl: "", fixtureKey, dual };
}

// ---------------------------------------------------------------- tiny PDF writer (mock file view)

function makePdf(s: Scenario, total: number | null): string {
  if (s.readable === false) return "";
  const lines: [number, number, string, number][] = []; // x, y, text, size
  let y = 780;
  lines.push([50, y, s.vendor, 16]); y -= 18;
  lines.push([50, y, `Tax ID ${s.tax_id}`, 9]); y -= 36;
  lines.push([50, y, "INVOICE", 20]); y -= 26;
  lines.push([50, y, `Invoice no.  ${s.invoice_number ?? ""}`, 10]); y -= 14;
  lines.push([50, y, `Date         ${s.invoice_date}`, 10]); y -= 14;
  lines.push([50, y, `PO           ${s.po ?? "-"}`, 10]); y -= 14;
  lines.push([50, y, "Bill to      Ledgerline Manufacturing, Accounts Payable", 10]); y -= 30;
  lines.push([50, y, "Description", 10]); lines.push([330, y, "Qty", 10]); lines.push([390, y, "Unit", 10]); lines.push([480, y, "Amount", 10]); y -= 16;
  for (const l of s.lines) {
    lines.push([50, y, l.description, 10]); lines.push([330, y, String(l.qty), 10]);
    lines.push([390, y, usd(l.unit), 10]); lines.push([480, y, usd(l.amount ?? l.qty * l.unit), 10]); y -= 14;
  }
  y -= 16;
  lines.push([390, y, "Total", 12]); lines.push([480, y, total != null ? usd(total) : "", 12]); y -= 40;
  lines.push([50, y, "Remit to account ending " + (s.bank_change ? "9921" : "7781") + ". Net 30.", 9]);
  if (s.kind === "scan") lines.push([50, 60, "[scanned copy]", 8]);
  const esc = (x: string) => x.replace(/[^\x20-\x7e]/g, "-").replace(/([()\\])/g, "\\$1");
  const content = lines.map(([x, yy, txt, sz]) => `BT /F1 ${sz} Tf ${x} ${yy} Td (${esc(txt)}) Tj ET`).join("\n");
  const objs = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let out = "%PDF-1.4\n";
  const offsets: number[] = [];
  objs.forEach((o, i) => { offsets.push(out.length); out += `${i + 1} 0 obj\n${o}\nendobj\n`; });
  const xref = out.length;
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n` + offsets.map((o) => `${String(o).padStart(10, "0")} 00000 n \n`).join("");
  out += `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return URL.createObjectURL(new Blob([out], { type: "application/pdf" }));
}

function memoHtml(d: RunDetail): string {
  const rows = d.rules.map((r) => `<tr><td>${r.rule}</td><td>${r.title}</td><td>${r.status}</td><td>${r.message}</td></tr>`).join("");
  const html = `<!doctype html><meta charset="utf-8"><title>Decision memo ${d.invoice_number ?? d.id}</title>
<style>body{font:14px/1.5 system-ui;max-width:760px;margin:40px auto;color:#14213D}table{border-collapse:collapse;width:100%}td{border-top:1px solid #ddd;padding:4px 6px;vertical-align:top}</style>
<h1>Decision memo (mock)</h1><p><b>${d.vendor_name ?? "Unknown vendor"}</b> · ${d.invoice_number ?? "no number"} · ${d.total_cents != null ? usd(d.total_cents) : "-"}</p>
<p><b>Outcome:</b> ${d.outcome} (${d.reasons.join(", ")})</p><p>${d.explanation}</p><table>${rows}</table>
<p style="color:#777">Packet ${d.packet_hash} · policy ${d.policy_version}</p>`;
  return URL.createObjectURL(new Blob([html], { type: "text/html" }));
}

// ---------------------------------------------------------------- store

let runs: MockRun[] = [];
let counter = 7;

function seed() {
  runs = [];
  const now = Date.now();
  const past = (h: number) => now - h * 3600_000;
  const mk = (id: string, key: string, hoursAgo: number, actions?: { action: string; reviewer: string; note: string; after: Outcome }[]) => {
    const r = buildRun(id, SCENARIOS[key], past(hoursAgo));
    const fin = (r.detail as RunDetail & { _final: { outcome: Outcome } })._final;
    let outcome = fin.outcome;
    for (const a of actions ?? []) {
      r.detail.actions.push({ action: a.action, reviewer: a.reviewer, note: a.note, ts: new Date(past(hoursAgo - 1)).toISOString(), outcome_before: outcome, outcome_after: a.after });
      outcome = a.after;
    }
    (r.detail as RunDetail & { _final: { outcome: Outcome } })._final.outcome = outcome;
    runs.push(r);
  };
  mk("run-0007", "NW007", 400, [{ action: "approve", reviewer: "Dana Whitfield", note: "Freight line is a $0 courtesy note from Northwind. Approved.", after: "approved" }]);
  mk("run-0003", "RET", 220, [{ action: "return_to_vendor", reviewer: "Sam Ortega", note: "Asked Acme for the PO number on the call-out; resubmit with PO.", after: "returned" }]);
  mk("run-0004", "ENC", 150);
  mk("run-0005", "CURR", 96);
  mk("run-0006", "GX5", 30);
  mk("run-0008", "BANK", 5);
  counter = 8;
}
seed();

function materialize(r: MockRun): RunDetail {
  const elapsed = Date.now() - r.start;
  const due = r.plan.filter((p) => p.at <= elapsed);
  const stages: StageEvent[] = due.map((p, i) => ({
    seq: i + 1, run_id: r.detail.id, stage: p.stage, status: p.status, title: p.title, message: p.message, data: p.data,
    ts: new Date(r.start + p.at).toISOString(),
  }));
  const done = due.length === r.plan.length;
  const fin = (r.detail as RunDetail & { _final: { outcome: Outcome; reasons: string[] } })._final;
  const d = { ...r.detail, stages };
  delete (d as { _final?: unknown })._final;
  if (!done) {
    d.status = due.length ? "running" : "queued";
    d.outcome = "processing";
    d.reasons = [];
    d.allowed_actions = [];
    d.duration_ms = null;
    if (due.length < 3) { d.vendor_name = null; d.invoice_number = null; d.total_cents = null; d.po_number = null; d.extraction_source = null; }
  } else {
    d.status = fin.outcome === "failed" ? "failed" : "done";
    d.outcome = fin.outcome;
    d.reasons = fin.reasons;
    if (fin.outcome !== "needs_review") d.allowed_actions = [];
  }
  return d;
}

const summary = (d: RunDetail): RunSummary => ({
  id: d.id, created_at: d.created_at, filename: d.filename, status: d.status, outcome: d.outcome, reasons: d.reasons,
  vendor_name: d.vendor_name, invoice_number: d.invoice_number, total_cents: d.total_cents, currency: d.currency,
  po_number: d.po_number, extraction_source: d.extraction_source, duration_ms: d.duration_ms,
});

const find = (id: string) => {
  const r = runs.find((x) => x.detail.id === id);
  if (!r) throw new ApiError(404, `Run ${id} not found`);
  return r;
};

const delay = <T,>(v: T, ms = 120) => new Promise<T>((res) => setTimeout(() => res(v), ms));

function newRun(s: Scenario, fixtureKey?: string): RunCreated {
  const prior = fixtureKey && runs.find((r) => r.fixtureKey === fixtureKey);
  if (prior) return { run_id: prior.detail.id, existing: true };
  counter += 1;
  const id = `run-${String(counter).padStart(4, "0")}`;
  runs.push(buildRun(id, s, Date.now() + 250, fixtureKey));
  return { run_id: id, existing: false };
}

const MOCK_SOURCE = { kind: "seed" as const, location: "mock", issues: [], counts: { vendors: 4, purchase_orders: 11, po_lines: 18 } };
const MOCK_BOXES = [{ name: "folder", label: "Demo mailbox folder", configured: true, detail: "mock" },
  { name: "gmail", label: "Gmail (IMAP)", configured: false, detail: "not set" },
  { name: "outlook", label: "Outlook / Microsoft 365 (Graph)", configured: false, detail: "not set" }];
const noBackend = () => Promise.reject(new ApiError(501, "Not available in mock mode"));

export const mockApi: Api = {
  connections: () => delay({ po_source: MOCK_SOURCE, mailboxes: MOCK_BOXES, send_via: null,
    fields: { gmail: [], outlook: [], slack: [], sheets: [] },
    slack: { configured: false, alerts: ["needs_review", "rejected", "failed", "held_mail"], digest_time: "18:00", digest_last: null } }),
  records: () => delay({ source: MOCK_SOURCE, vendors: [], purchase_orders: [] }),
  reloadRecords: () => delay({ source: MOCK_SOURCE, vendors: [], purchase_orders: [] }),
  sheet: () => delay({ source: MOCK_SOURCE, tabs: [] }),
  policy: () => delay({ version: POLICY_VERSION, values: {} }),
  mails: () => delay({ mails: [], stats: {}, mailboxes: MOCK_BOXES }),
  syncMail: noBackend,
  uploadMail: noBackend,
  releaseMail: noBackend,
  dismissMail: noBackend,
  notifications: () => delay({ slack_configured: false, items: [] }),
  digestPreview: () => delay({ title: "Daily summary", text: "Mock mode: no digest.", invoices: 0, emails: 0, counts: {} }),
  sendDigest: noBackend,
  vendorDraft: () => delay({ available: false, internal_notes: [], why: "Mock mode." }),
  sendVendorEmail: noBackend,
  connect: noBackend,
  testConnection: noBackend,
  disconnect: noBackend,
  bookUrl: () => "#",
  uploadBook: noBackend,
  setSource: noBackend,
  templateUrl: () => "#",
  health: () => delay({ ok: true, extraction_mode: "claude", api_key_present: true, policy_version: POLICY_VERSION, db: { ok: true, path: "mock" } }),
  stats: () => {
    const all = runs.map(materialize);
    const by: Partial<Record<Outcome, number>> = {};
    const val: Partial<Record<Outcome, number>> = {};
    for (const d of all) {
      by[d.outcome] = (by[d.outcome] ?? 0) + 1;
      val[d.outcome] = (val[d.outcome] ?? 0) + (d.total_cents ?? 0);
    }
    const decided = all.filter((d) => d.status === "done" || d.status === "failed");
    const s: Stats = {
      total: all.length, by_outcome: by, stp_rate: decided.length ? (by.auto_approved ?? 0) / decided.length : 0,
      avg_seconds: decided.length ? decided.reduce((a, d) => a + (d.duration_ms ?? 0), 0) / decided.length / 1000 : 0,
      open_reviews: by.needs_review ?? 0, value_cents_by_outcome: val,
    };
    return delay(s);
  },
  fixtures: () => delay(DEMO_FIXTURES),
  runFixture: (key) => {
    const s = SCENARIOS[key];
    if (!s) return Promise.reject(new ApiError(404, `Unknown fixture ${key}`));
    return delay(newRun(s, key), 200);
  },
  upload: (file) => {
    const key = Object.keys(SCENARIOS).find((k) => file.name.toUpperCase().startsWith(k));
    const base = key ? SCENARIOS[key] : SCENARIOS.H1;
    const res = newRun({ ...base, filename: file.name });
    if (!res.existing) find(res.run_id).fileUrl = URL.createObjectURL(file);
    return delay(res, 300);
  },
  runs: ({ outcome, q, limit = 50 }) => {
    let list = runs.map(materialize).map(summary).sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (outcome) list = list.filter((r) => r.outcome === outcome);
    if (q) {
      const n = q.toLowerCase();
      list = list.filter((r) => [r.vendor_name, r.invoice_number, r.po_number, r.filename, r.id].some((x) => x?.toLowerCase().includes(n)));
    }
    return delay(list.slice(0, limit));
  },
  run: (id) => { try { return delay(materialize(find(id))); } catch (e) { return Promise.reject(e); } },
  events: (id, after) => { try { return delay(materialize(find(id)).stages.filter((e) => e.seq > after), 60); } catch (e) { return Promise.reject(e); } },
  investigate: () => Promise.reject(new ApiError(501, "The investigation agent needs the real API.")),
  act: (id, body) => {
    try {
      const r = find(id);
      const cur = materialize(r);
      if (cur.outcome !== "needs_review" || !cur.allowed_actions.includes(body.action)) throw new ApiError(409, `Action ${body.action} is not allowed on a ${cur.outcome} run.`);
      if (!body.reviewer.trim() || !body.note.trim()) throw new ApiError(422, "Reviewer and note are required.");
      const fin = (r.detail as RunDetail & { _final: { outcome: Outcome } })._final;
      let after: Outcome = cur.outcome;
      let note = body.note;
      if (body.action === "approve") {
        const approvals = r.detail.actions.filter((a) => a.action === "approve");
        if (r.dual && approvals.some((a) => a.reviewer.trim().toLowerCase() === body.reviewer.trim().toLowerCase()))
          throw new ApiError(409, "A second approval must come from a different reviewer.");
        if (r.dual && approvals.length === 0) { after = "needs_review"; note = `${body.note} (1 of 2 approvals)`; r.detail.allowed_actions = ["approve"]; }
        else after = "approved";
      } else if (body.action === "reject") after = "rejected";
      else if (body.action === "return_to_vendor") after = "returned";
      const a: AuditAction = { action: body.action, reviewer: body.reviewer.trim(), note, ts: new Date().toISOString(), outcome_before: cur.outcome, outcome_after: after };
      r.detail.actions.push(a);
      fin.outcome = after;
      if (after !== "needs_review") r.detail.allowed_actions = [];
      return delay(materialize(r), 250);
    } catch (e) { return Promise.reject(e); }
  },
  reset: () => { seed(); return delay({ ok: true }, 300); },
  fileUrl: (id) => runs.find((r) => r.detail.id === id)?.fileUrl ?? "",
  dataUrl: () => "",
  memoUrl: (id) => {
    const r = runs.find((x) => x.detail.id === id);
    if (!r) return "";
    if (r.memoUrl) URL.revokeObjectURL(r.memoUrl);
    r.memoUrl = memoHtml(materialize(r));
    return r.memoUrl;
  },
  stream: (id, after, h) => {
    let last = after;
    let stopped = false;
    const tick = () => {
      if (stopped) return;
      let r: MockRun;
      try { r = find(id); } catch { h.onError(); return; }
      const d = materialize(r);
      for (const e of d.stages) if (e.seq > last) { last = e.seq; h.onEvent(e); }
      if (d.status === "done" || d.status === "failed") { stopped = true; h.onDone(); return; }
      setTimeout(tick, 80);
    };
    setTimeout(tick, 50);
    return () => { stopped = true; };
  },
};
