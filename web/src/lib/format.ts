import type { Outcome } from "../api/types";

export function money(cents: number | null | undefined, currency: string | null = "USD", opts: { whole?: boolean; signed?: boolean } = {}): string {
  if (cents == null) return "—";
  const v = cents / 100;
  const digits = opts.whole ? 0 : 2;
  let s: string;
  try {
    s = new Intl.NumberFormat("en-US", {
      style: "currency", currency: currency || "USD", minimumFractionDigits: digits, maximumFractionDigits: digits,
    }).format(Math.abs(v));
  } catch {
    s = `${currency ?? ""} ${Math.abs(v).toFixed(digits)}`.trim();
  }
  if (v < 0) return "-" + s;
  return opts.signed && v > 0 ? "+" + s : s;
}

export function qty(milli: number | null | undefined): string {
  if (milli == null) return "—";
  return (milli / 1000).toLocaleString("en-US", { maximumFractionDigits: 3 });
}

export function duration(ms: number | null | undefined): string {
  if (ms == null || !isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} ms`;
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return d.toLocaleDateString("en-US", { day: "numeric", month: "short" }) + ", " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
}

export function stamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const OUTCOME_LABEL: Record<Outcome, string> = {
  processing: "Processing",
  auto_approved: "Auto-approved",
  approved: "Approved",
  needs_review: "Needs review",
  rejected: "Rejected",
  returned: "Returned",
  failed: "Failed",
};

export const OUTCOMES: Outcome[] = ["needs_review", "auto_approved", "approved", "rejected", "returned", "failed", "processing"];

export function outcomeLabel(o: string): string {
  return (OUTCOME_LABEL as Record<string, string>)[o] ?? humanize(o);
}

const REASON_LABEL: Record<string, string> = {
  STP_CLEAN: "Clean match",
  PRICE_VARIANCE: "Price variance",
  DUPLICATE_INVOICE: "Duplicate invoice",
  DUPLICATE_SUSPECTED: "Possible duplicate",
  MISSING_INVOICE_NUMBER: "No invoice number",
  MISSING_FIELD: "Missing field",
  ARITHMETIC_MISMATCH: "Totals do not add up",
  DOCUMENT_UNREADABLE: "Unreadable document",
  BANK_CHANGE: "Bank account changed",
  TAX_ID_CONFLICT: "Tax ID conflict",
  VENDOR_INACTIVE: "Vendor inactive",
  UNKNOWN_VENDOR: "Unknown vendor",
  PO_MISSING: "No PO",
  PO_AMBIGUOUS: "PO ambiguous",
  PO_CLOSED: "PO closed",
  VENDOR_PO_MISMATCH: "PO of another vendor",
  CURRENCY_MISMATCH: "Currency mismatch",
  OVER_PO_REMAINING: "Over PO balance",
  QTY_VARIANCE: "Quantity over receipt",
  LINE_UNVERIFIED: "Unmatched line",
  TAX_VARIANCE: "Tax variance",
  APPROVAL_REQUIRED: "Approval required",
  DUE_DATE_INVALID: "Date problem",
  INCOMPLETE_EVIDENCE: "Incomplete evidence",
  RULE_ERROR: "Rule error",
};

export function reasonLabel(code: string): string {
  return REASON_LABEL[code] ?? humanize(code);
}

export function humanize(s: string): string {
  const t = s.replace(/_/g, " ").toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

const SOURCE_LABEL: Record<string, string> = {
  claude: "Claude",
  openai: "OpenAI",
  local_text: "Local text parser",
  local_ocr: "Local OCR",
  replay: "Replayed",
  none: "None",
};

export function sourceLabel(s: string | null | undefined): string {
  if (!s) return "—";
  return SOURCE_LABEL[s] ?? humanize(s);
}

/** Accepts a 0..1 fraction or a 0..100 percentage. */
export function percent(x: number | null | undefined): string {
  if (x == null || !isFinite(x)) return "—";
  const v = x > 1 ? x : x * 100;
  return `${v.toFixed(1)}%`;
}

export const ACTION_LABEL: Record<string, string> = {
  approve: "Approve",
  reject: "Reject",
  return_to_vendor: "Return to vendor",
  escalate: "Escalate",
};
