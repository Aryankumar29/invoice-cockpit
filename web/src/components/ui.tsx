import { useEffect, useState, type ReactNode } from "react";
import type { Outcome } from "../api/types";
import { humanize, money, outcomeLabel, qty, reasonLabel } from "../lib/format";

const P = { fill: "none", stroke: "currentColor", strokeWidth: 2.4, strokeLinecap: "round", strokeLinejoin: "round" } as const;

export const Icon = {
  check: () => <svg viewBox="0 0 16 16" aria-hidden><path {...P} d="M3.5 8.5l3 3 6-7" /></svg>,
  cross: () => <svg viewBox="0 0 16 16" aria-hidden><path {...P} d="M4 4l8 8M12 4l-8 8" /></svg>,
  bang: () => <svg viewBox="0 0 16 16" aria-hidden><path {...P} d="M8 3.5v5.5M8 12.2v.3" /></svg>,
  minus: () => <svg viewBox="0 0 16 16" aria-hidden><path {...P} d="M4 8h8" /></svg>,
  dot: () => <svg viewBox="0 0 16 16" aria-hidden><circle cx="8" cy="8" r="3" fill="currentColor" /></svg>,
  return: () => <svg viewBox="0 0 16 16" aria-hidden><path {...P} d="M6 4L3 7l3 3M3.5 7H10a3 3 0 010 6H8" /></svg>,
  caret: () => <svg className="caret" viewBox="0 0 16 16" aria-hidden><path {...P} strokeWidth={2} d="M6 4l4 4-4 4" /></svg>,
  upload: () => <svg viewBox="0 0 24 24" aria-hidden><path {...P} strokeWidth={1.8} d="M12 15V4m0 0L7.5 8.5M12 4l4.5 4.5M5 15v3a2 2 0 002 2h10a2 2 0 002-2v-3" /></svg>,
  doc: () => <svg viewBox="0 0 16 16" aria-hidden width="14" height="14"><path {...P} strokeWidth={1.6} d="M4 2h5l3 3v9H4zM9 2v3h3" /></svg>,
};

const OUTCOME_ICON: Record<Outcome, () => JSX.Element> = {
  auto_approved: Icon.check, approved: Icon.check, needs_review: Icon.bang, rejected: Icon.cross,
  returned: Icon.return, failed: Icon.minus, processing: Icon.dot,
};

export function OutcomeBadge({ outcome }: { outcome: Outcome | string }) {
  const I = OUTCOME_ICON[outcome as Outcome] ?? Icon.dot;
  return (
    <span className={`badge o-${outcome}`}>
      {outcome === "processing" ? <span className="pulse" /> : <I />}
      {outcomeLabel(outcome)}
    </span>
  );
}

const STATUS_ICON: Record<string, () => JSX.Element> = {
  pass: Icon.check, ok: Icon.check, fail: Icon.cross, failed: Icon.cross, error: Icon.cross, warn: Icon.bang,
  cannot_run: Icon.minus, not_applicable: Icon.minus, stopped: Icon.minus, started: Icon.dot,
};
const STATUS_LABEL: Record<string, string> = { pass: "Pass", fail: "Fail", cannot_run: "Cannot run", not_applicable: "Not applicable", error: "Error" };

export function StatusChip({ status }: { status: string }) {
  const I = STATUS_ICON[status] ?? Icon.dot;
  return <span className={`badge s-${status}`}><I />{STATUS_LABEL[status] ?? humanize(status)}</span>;
}

export function ReasonChips({ reasons }: { reasons: string[] }) {
  if (!reasons.length) return null;
  return (
    <span className="chips">
      {reasons.map((r) => <span key={r} className="chip" title={r}>{reasonLabel(r)}</span>)}
    </span>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

/** Key/value rendering of an arbitrary record. Nested values fall back to compact JSON. */
export function KV({ data, omit = [] }: { data: Record<string, unknown>; omit?: string[] }) {
  const entries = Object.entries(data).filter(([k, v]) => !omit.includes(k) && v !== undefined);
  if (!entries.length) return <p className="faint">No details recorded.</p>;
  return (
    <dl className="kv">
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt>{humanize(k)}</dt>
          <dd>{render(v, k)}</dd>
        </div>
      ))}
    </dl>
  );
}

// Evidence keys follow the core's conventions: money in cents, quantities in thousandths.
const QTY_KEY = /qty|received|ordered|cumulative/;
const MONEY_KEY = /cents|total|billed|expected|variance|allowed|unit|remaining|allowance|tax|freight|line_sum|difference|tolerance|this_invoice/;

function scalar(v: unknown, key = ""): ReactNode {
  if (v === null || v === undefined) return <span className="faint">none</span>;
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? humanize(x) : String(x))).join(", ");
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    if (QTY_KEY.test(key)) return qty(v);
    if (MONEY_KEY.test(key) && Number.isInteger(v)) return money(v, "USD", { signed: key === "variance" || key === "difference" });
    return String(v);
  }
  return String(v);
}

const isPrimitive = (x: unknown) => x === null || ["string", "number", "boolean"].includes(typeof x);
const isCell = (x: unknown) => isPrimitive(x) || (Array.isArray(x) && x.every(isPrimitive));
const isFlatObj = (x: unknown): x is Record<string, unknown> =>
  typeof x === "object" && x !== null && !Array.isArray(x) && Object.values(x).every(isCell);

function render(v: unknown, key = ""): ReactNode {
  if (isPrimitive(v)) return scalar(v, key);
  if (Array.isArray(v) && v.every((x) => typeof x === "string" || typeof x === "number")) return v.length ? v.join(", ") : <span className="faint">none</span>;
  if (Array.isArray(v) && v.length && v.every(isFlatObj)) {
    const cols = [...new Set(v.flatMap((r) => Object.keys(r)))];
    return (
      <div className="table-wrap">
        <table className="t mini">
          <thead><tr>{cols.map((c) => <th key={c}>{humanize(c)}</th>)}</tr></thead>
          <tbody>{v.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c} className="num">{scalar(r[c], c)}</td>)}</tr>)}</tbody>
        </table>
      </div>
    );
  }
  if (isFlatObj(v)) {
    return (
      <span className="chips">
        {Object.entries(v).map(([k, x]) => <span key={k} className="chip">{humanize(k)}: {scalar(x, k)}</span>)}
      </span>
    );
  }
  return <pre className="json" style={{ maxHeight: 200 }}>{JSON.stringify(v, null, 2)}</pre>;
}

export function Toast({ children, onClose, ms = 4000 }: { children: ReactNode; onClose: () => void; ms?: number }) {
  useEffect(() => {
    const t = setTimeout(onClose, ms);
    return () => clearTimeout(t);
  }, [onClose, ms]);
  return <div className="toast" role="status">{children}</div>;
}

type Theme = "system" | "light" | "dark";
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => {
    try { return (localStorage.getItem("cockpit-theme") as Theme) || "system"; } catch { return "system"; }
  });
  useEffect(() => {
    const el = document.documentElement;
    if (theme === "system") el.removeAttribute("data-theme");
    else el.setAttribute("data-theme", theme);
    try { localStorage.setItem("cockpit-theme", theme); } catch { /* storage blocked */ }
  }, [theme]);
  const next: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };
  return (
    <button className="theme-btn" onClick={() => setTheme(next[theme])} title="Switch colour theme">
      Theme: {theme}
    </button>
  );
}

/** One collapsible row: a single summary line; the details open on click (native <details>, keyboard-ready). */
export function Acc({ title, sub, right, open = false, id, children }: {
  title: ReactNode; sub?: ReactNode; right?: ReactNode; open?: boolean; id?: string; children: ReactNode;
}) {
  return (
    <details className="acc" open={open} id={id}>
      <summary>
        <span className="acc-title"><b>{title}</b>{sub && <span className="acc-sub">{sub}</span>}</span>
        <span className="acc-right">{right}</span>
        <span className="acc-caret" aria-hidden><Icon.caret /></span>
      </summary>
      <div className="acc-body">{children}</div>
    </details>
  );
}
