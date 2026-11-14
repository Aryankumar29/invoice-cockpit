// Defensive readers for the RunDetail fields the contract types as `unknown`.
import type {
  AllocationShape, DuplicatesShape, GroundingShape, InvoiceLineShape, LineMatchShape, PriceLineShape, RuleRow,
} from "../api/types";

const isObj = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
const num = (x: unknown): number | null => (typeof x === "number" && isFinite(x) ? x : null);
const str = (x: unknown): string | null => (typeof x === "string" ? x : null);

export function asDuplicates(x: unknown): DuplicatesShape | null {
  if (!isObj(x)) return null;
  const hits = Array.isArray(x.hits) ? x.hits.filter(isObj).map((h) => ({
    run_id: str(h.run_id) ?? "", invoice_number: str(h.invoice_number), score: num(h.score) ?? 0,
    signals: Array.isArray(h.signals) ? h.signals.map(String) : [],
  })) : [];
  return { kind: (str(x.kind) ?? "none") as DuplicatesShape["kind"], ref_check_ran: x.ref_check_ran !== false, hits };
}

export type MatchView = { status: string; ref: string | null; score: number; method: string; candidates: [string, number][] };

/** VendorMatch / POMatch. `vendor` or `po` may be an id string or a nested object. */
export function asMatch(x: unknown, key: "vendor" | "po"): MatchView | null {
  if (!isObj(x)) return null;
  const inner = x[key];
  const ref = isObj(inner) ? str(inner.name) ?? str(inner.id) ?? str(inner.po_number) : str(inner);
  return {
    status: str(x.status) ?? "unknown", ref, score: num(x.score) ?? 0, method: str(x.method) ?? "",
    candidates: Array.isArray(x.candidates)
      ? x.candidates.filter(Array.isArray).map((c) => [String(c[0]), Number(c[1])] as [string, number])
      : [],
  };
}

export function asGrounding(x: unknown): GroundingShape | null {
  if (!isObj(x)) return null;
  return {
    checked: num(x.checked) ?? 0, grounded: num(x.grounded) ?? 0,
    ungrounded: Array.isArray(x.ungrounded) ? x.ungrounded.map(String) : [], arithmetic_ok: x.arithmetic_ok !== false,
  };
}

export function asLineMatches(x: unknown[] | null | undefined): LineMatchShape[] {
  const ints = (v: unknown) => (Array.isArray(v) ? v.map(Number).filter(Number.isFinite) : []);
  return (x ?? []).filter(isObj).map((m) => ({
    inv_index: num(m.inv_index) ?? 0, po_line_no: num(m.po_line_no), score: num(m.score) ?? 0, bundle: ints(m.bundle),
    bundle_options: Array.isArray(m.bundle_options) ? m.bundle_options.map(ints) : [],
  }));
}

export function asAllocations(x: unknown[] | null | undefined): AllocationShape[] {
  return (x ?? []).filter(isObj).map((a) => ({ po_line_no: num(a.po_line_no) ?? 0, qty_milli: num(a.qty_milli) ?? 0, amount_cents: num(a.amount_cents) ?? 0 }));
}

export function invoiceLines(inv: Record<string, unknown> | null | undefined): InvoiceLineShape[] {
  const ls = inv?.lines;
  if (!Array.isArray(ls)) return [];
  return ls.filter(isObj).map((l) => ({
    description: str(l.description) ?? "", qty_milli: num(l.qty_milli) ?? 0, unit_price_cents: num(l.unit_price_cents) ?? 0,
    amount_cents: num(l.amount_cents) ?? 0, sku: str(l.sku),
  }));
}

/** Line comparison rows come from R10 evidence (expected vs billed, variance, allowed). */
export function priceLines(rules: RuleRow[]): PriceLineShape[] {
  const ls = rules.find((r) => r.rule === "R10")?.evidence?.lines;
  if (!Array.isArray(ls)) return [];
  return ls.filter(isObj).map((l) => ({
    invoice_line: num(l.invoice_line) ?? 0, po_line: num(l.po_line) ?? 0, invoice_unit: num(l.invoice_unit) ?? 0,
    po_unit: num(l.po_unit) ?? 0, expected: num(l.expected) ?? 0, billed: num(l.billed) ?? 0,
    variance: num(l.variance) ?? 0, allowed: num(l.allowed) ?? 0, ok: l.ok !== false,
    po_lines: Array.isArray(l.po_lines) ? l.po_lines.map(Number) : [],
  }));
}

/** PO balance from R12 evidence. */
export function ledger(rules: RuleRow[]) {
  const e = rules.find((r) => r.rule === "R12")?.evidence;
  if (!e || num(e.po_total) == null) return null;
  return {
    po_total: num(e.po_total)!, billed_before: num(e.billed_before) ?? 0, remaining_before: num(e.remaining_before) ?? 0,
    this_invoice: num(e.this_invoice) ?? 0, remaining_after: num(e.remaining_after) ?? 0, allowance: num(e.allowance) ?? 0,
    prior_runs: Array.isArray(e.prior_runs) ? e.prior_runs.map(String) : [],
  };
}

export function needsDualApproval(rules: RuleRow[], total: number | null): boolean {
  return rules.some((r) => r.evidence?.dual_control === true) || (total ?? 0) > 5_000_000;
}

export { isObj };
