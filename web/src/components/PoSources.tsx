import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type ConnectResult, type Connections } from "../api";
import { ServiceRow, StatusPill } from "./Connections";
import { Acc, Icon } from "./ui";
import { when } from "../lib/format";

const COLUMNS: [string, string, string][] = [
  ["Vendors", "vendor_id, vendor_name, tax_id", "status, billing_email, email_domain, bank_account"],
  ["PurchaseOrders", "po_number, vendor_id, currency", "vendor_name, buyer_company, status, issued_on, tax_rate_pct, requester"],
  ["POLines", "po_number, line_no, description, qty_ordered, unit_price", "sku, qty_received"],
];

/** The ways to load purchase orders (Connections tab). Only one is in use at a time. */
export function PoSources({ c }: { c: Connections }) {
  const qc = useQueryClient();
  const [result, setResult] = useState<ConnectResult | null>(null);
  const after = (r: ConnectResult) => {
    setResult(r);
    if (r.connections) qc.setQueryData(["connections"], r.connections);
    qc.invalidateQueries({ queryKey: ["records"] });
  };
  const input = useRef<HTMLInputElement>(null);
  const upload = useMutation({ mutationFn: api.uploadBook, onSuccess: after, onError: (e) => setResult({ ok: false, message: (e as Error).message }) });
  const use = useMutation({ mutationFn: api.setSource, onSuccess: after });
  const o = c.po_source.options;
  const inUse = (on: boolean) => (on ? <StatusPill on text="In use" /> : undefined);

  return (
    <>
      {result && (
        <div className={`acc-note ${result.ok ? "" : "acc-note-bad"}`}>
          {result.ok ? <Icon.check /> : <Icon.cross />}
          <div>{result.message} {result.ok && <Link to="/po-book">See what was loaded</Link>}
            {result.issues && result.issues.length > 0 && <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{result.issues.slice(0, 8).map((i) => <li key={i}>{i}</li>)}</ul>}</div>
        </div>
      )}
      <Acc title="Upload an Excel file" sub={o?.uploaded.exists ? `Last upload: ${o.uploaded.filename ?? "spreadsheet"}${o.uploaded.uploaded_at ? `, ${when(o.uploaded.uploaded_at)}` : ""}` : "Fill in the template and upload it"}
           right={inUse(o?.in_use === "uploaded")}>
        <div className="acc-stack">
          <p>Download the blank template, fill in your vendors, purchase orders and lines in Excel, then upload it. It is checked before it is used.</p>
          <div className="card-actions">
            <button className="btn btn-sm btn-primary" disabled={upload.isPending} onClick={() => input.current?.click()}>{upload.isPending ? <><span className="spinner" /> Checking…</> : "Upload spreadsheet"}</button>
            {o?.uploaded.exists && o.in_use !== "uploaded" && <button className="btn btn-sm" onClick={() => use.mutate("uploaded")}>Use the last upload</button>}
            <a className="btn btn-sm btn-ghost" href={api.templateUrl()}>Download blank template</a>
          </div>
          <input ref={input} type="file" accept=".xlsx" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
        </div>
      </Acc>
      <ServiceRow service="sheets" c={c} />
      {o?.sheets.connected && o.in_use !== "sheets" && (
        <div className="acc-note">A Google Sheet is connected but not in use. <button className="btn btn-sm" onClick={() => use.mutate("sheets")}>Use the Google Sheet</button></div>
      )}
      <Acc title="Sample data" sub="11 example purchase orders from 4 vendors, for trying the product" right={inUse(o?.in_use === "sample" || o?.in_use === "builtin")}>
        <div className="acc-stack">
          <p>The purchase orders the sample invoices are written against.</p>
          <div className="card-actions">
            {o && o.in_use !== "sample" && o.in_use !== "builtin" && <button className="btn btn-sm" onClick={() => use.mutate("sample")}>Use sample data</button>}
            <a className="btn btn-sm btn-ghost" href={api.templateUrl(true)}>Download it</a>
          </div>
        </div>
      </Acc>
      <Acc title="What the spreadsheet must contain" sub="Three tabs, the required columns, and the formats">
        <div className="acc-stack needs">
          <table className="t">
            <thead><tr><th>Tab</th><th>Required</th><th>Optional</th></tr></thead>
            <tbody>{COLUMNS.map(([t, r, op]) => <tr key={t}><td><b>{t}</b></td><td className="mono">{r}</td><td className="mono faint">{op}</td></tr>)}</tbody>
          </table>
          <ul>
            <li>Keep the tab names and the header row exactly as in the template.</li>
            <li>Money as plain numbers (100.00), dates as YYYY-MM-DD, tax as a percent (8.25).</li>
            <li>qty_received is what has been delivered; invoices are paid only up to it.</li>
            <li>billing_email is where correction emails go; email_domain matches invoice emails to the vendor.</li>
            <li>A row with a problem is skipped and listed on the PO book tab; it is never guessed.</li>
          </ul>
        </div>
      </Acc>
    </>
  );
}
