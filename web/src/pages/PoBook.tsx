import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { StatusPill } from "../components/Connections";
import { Acc, Icon } from "../components/ui";
import { money, qty, when } from "../lib/format";

const IN_USE: Record<string, string> = { uploaded: "Uploaded spreadsheet", sheets: "Google Sheet", sample: "Sample spreadsheet", builtin: "Built-in demo data" };

export default function PoBook() {
  const qc = useQueryClient();
  const rec = useQuery({ queryKey: ["records"], queryFn: api.records, refetchInterval: 15000 });
  const conn = useQuery({ queryKey: ["connections"], queryFn: api.connections, refetchInterval: 20000 });
  const reload = useMutation({ mutationFn: api.reloadRecords, onSuccess: (d) => { qc.setQueryData(["records"], d); qc.invalidateQueries({ queryKey: ["sheet"] }); } });
  const [view, setView] = useState<"sheet" | "orders">("sheet");
  const s = rec.data?.source;
  const o = conn.data?.po_source.options;
  const file = o?.in_use === "uploaded" ? o.uploaded.filename : o?.in_use === "sheets" ? "Google Sheet" : o?.in_use === "sample" ? "po_book.xlsx" : null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>PO book</h1>
          <p>The purchase orders and vendors every invoice is checked against.</p>
        </div>
      </div>

      {s && (
        <div className="summary-bar">
          <div>
            <b>In use: {o ? IN_USE[o.in_use] : "…"}</b>
            {file && <span> ({o?.in_use === "sheets" && o.sheets.url ? <a href={o.sheets.url} target="_blank" rel="noreferrer">{file}</a> : <a href={api.bookUrl()}>{file}</a>})</span>}
            <span className="faint"> {s.counts?.vendors ?? 0} vendors, {s.counts?.purchase_orders ?? 0} purchase orders, {s.counts?.po_lines ?? 0} lines. Loaded {when(s.loaded_at)}.</span>
          </div>
          <div className="summary-chips">
            {s.error ? <StatusPill on={false} text="Could not reload" /> : s.issues.length ? <StatusPill on={false} text={`${s.issues.length} rows to fix`} /> : <StatusPill on text="All rows valid" />}
            <button className="btn btn-sm btn-ghost" disabled={reload.isPending} onClick={() => reload.mutate()}>{reload.isPending ? <span className="spinner" /> : "Reload"}</button>
            <Link className="btn btn-sm" to="/connections">Change source</Link>
          </div>
        </div>
      )}
      {s?.error && <div className="banner o-rejected" style={{ marginBottom: 16 }}><Icon.cross />{s.error} The last good copy is still in use.</div>}

      <div className="view-switch" role="tablist" aria-label="View">
        <button role="tab" aria-selected={view === "sheet"} onClick={() => setView("sheet")}>Sheet</button>
        <button role="tab" aria-selected={view === "orders"} onClick={() => setView("orders")}>By purchase order</button>
      </div>
      {view === "sheet" ? <SheetView /> : <>

      {s && s.issues.length > 0 && (
        <section className="acc-group">
          <h2>Rows to fix</h2>
          <div className="acc-list">
            <Acc title={`${s.issues.length} row(s) were skipped`} sub="Fix them in the spreadsheet and upload or reload" open>
              <ul className="acc-stack" style={{ paddingLeft: 18, margin: 0 }}>{s.issues.map((i) => <li key={i}>{i}</li>)}</ul>
            </Acc>
          </div>
        </section>
      )}

      <section className="acc-group">
        <h2>Purchase orders {rec.data ? <span className="faint">({rec.data.purchase_orders.length})</span> : null}</h2>
        {rec.isError && <div className="banner o-rejected"><Icon.cross />{(rec.error as Error).message}</div>}
        <div className="acc-list">
          {(rec.data?.purchase_orders ?? []).map((p) => {
            const short = p.lines.some((l) => l.received_qty_milli < l.qty_milli);
            return (
              <Acc key={p.po_number} title={<><span className="mono">{p.po_number}</span> {p.vendor_name}</>}
                   sub={`${p.lines.length} line${p.lines.length === 1 ? "" : "s"}, issued ${p.issued_on ?? "—"}${short ? ", not all received" : ""}`}
                   right={<><span className="acc-amount">{money(p.total_cents, p.currency)}</span><span className={`chip ${p.status === "open" ? "chip-on" : ""}`}>{p.status}</span></>}>
                <div className="acc-stack">
                  <dl className="kv">
                    <dt>Buyer</dt><dd>{p.buyer_company ?? "—"}</dd>
                    <dt>Tax rate</dt><dd>{(p.tax_rate_bp / 100).toFixed(2)}%</dd>
                  </dl>
                  <div className="table-wrap">
                    <table className="t">
                      <thead><tr><th>Line</th><th>Item code</th><th>Description</th><th className="r">Ordered</th><th className="r">Received</th><th className="r">Unit price</th></tr></thead>
                      <tbody>{p.lines.map((l) => (
                        <tr key={l.line_no}><td>{l.line_no}</td><td className="mono">{l.sku ?? "—"}</td><td>{l.description}</td>
                          <td className="r">{qty(l.qty_milli)}</td>
                          <td className="r" style={{ color: l.received_qty_milli < l.qty_milli ? "var(--review)" : undefined }}>{qty(l.received_qty_milli)}</td>
                          <td className="r">{money(l.unit_price_cents, p.currency)}</td></tr>
                      ))}</tbody>
                    </table>
                  </div>
                </div>
              </Acc>
            );
          })}
        </div>
      </section>

      <section className="acc-group">
        <h2>Vendors {rec.data ? <span className="faint">({rec.data.vendors.length})</span> : null}</h2>
        <div className="acc-list">
          {(rec.data?.vendors ?? []).map((v) => (
            <Acc key={v.vendor_id} title={v.vendor_name} sub={v.email ?? "No billing email"}
                 right={<span className={`chip ${v.status === "active" ? "chip-on" : ""}`}>{v.status}</span>}>
              <dl className="kv acc-stack">
                <dt>Vendor ID</dt><dd>{v.vendor_id}</dd>
                <dt>Tax ID</dt><dd>{v.tax_id}</dd>
                <dt>Billing email</dt><dd>{v.email ?? "—"}</dd>
                <dt>Email domain</dt><dd>{v.domain ? `@${v.domain}` : "—"}</dd>
                <dt>Bank account</dt><dd>{v.bank_on_file ? "On file (only a fingerprint is stored)" : "Not on file"}</dd>
              </dl>
            </Acc>
          ))}
        </div>
      </section>
      </>}
    </>
  );
}

const LABEL: Record<string, string> = {
  vendor_id: "Vendor ID", vendor_name: "Vendor name", tax_id: "Tax ID", status: "Status", billing_email: "Billing email",
  email_domain: "Email domain", bank_account: "Bank account", po_number: "PO number", buyer_company: "Buyer company",
  currency: "Currency", issued_on: "Issued on", tax_rate_pct: "Tax %", requester: "Requester", line_no: "Line",
  sku: "Item code", description: "Description", qty_ordered: "Qty ordered", unit_price: "Unit price", qty_received: "Qty received",
  line_amount: "Line amount", po_total: "PO total",
};
const NUMERIC = new Set(["line_no", "qty_ordered", "unit_price", "qty_received", "tax_rate_pct", "line_amount", "po_total"]);

function SheetView() {
  const sheet = useQuery({ queryKey: ["sheet"], queryFn: api.sheet, refetchInterval: 15000 });
  const [tab, setTab] = useState("PurchaseOrders");
  const [q, setQ] = useState("");
  if (sheet.isError) return <div className="banner o-rejected"><Icon.cross />{(sheet.error as Error).message}</div>;
  if (!sheet.data) return <div className="empty">Loading…</div>;
  const t = sheet.data.tabs.find((x) => x.name === tab) ?? sheet.data.tabs[0];
  const needle = q.trim().toLowerCase();
  const rows = needle ? t.rows.filter((r) => r.cells.some((c) => c.toLowerCase().includes(needle))) : t.rows;
  return (
    <section className="sheet" aria-label="Spreadsheet">
      <div className="sheet-bar">
        <input className="input" type="search" placeholder={`Search ${t.name}`} value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the sheet" />
        <span className="faint">{rows.length} of {t.rows.length} rows</span>
      </div>
      <div className="sheet-scroll">
        <table className="sheet-table">
          <thead>
            <tr><th className="rownum">#</th>{t.columns.map((c) => <th key={c} className={`${NUMERIC.has(c) ? "r" : ""} ${t.computed?.includes(c) ? "calc" : ""}`} title={t.computed?.includes(c) ? "Calculated: quantity x unit price" : c}>{LABEL[c] ?? c}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.row} className={r.problem ? "bad" : ""} title={r.problem ?? undefined}>
                <td className="rownum">{r.row}</td>
                {r.cells.map((c, i) => <td key={i} className={`${NUMERIC.has(t.columns[i]) ? "r" : ""} ${t.computed?.includes(t.columns[i]) ? "calc" : ""}`}>{c || <span className="faint">—</span>}</td>)}
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={t.columns.length + 1} className="faint" style={{ textAlign: "center", padding: 24 }}>No rows match.</td></tr>}
          </tbody>
        </table>
      </div>
      {t.rows.some((r) => r.problem) && (
        <ul className="sheet-problems">
          {t.rows.filter((r) => r.problem).map((r) => <li key={r.row}><b>Row {r.row}:</b> {r.problem}</li>)}
        </ul>
      )}
      <div className="sheet-tabs" role="tablist" aria-label="Sheet tabs">
        {sheet.data.tabs.map((x) => (
          <button key={x.name} role="tab" aria-selected={x.name === t.name} onClick={() => { setTab(x.name); setQ(""); }}>
            {x.name} <span className="faint">{x.rows.length}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
