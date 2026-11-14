import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type ConnectionField, type ConnectResult, type Connections } from "../api";
import { PoSources } from "./PoSources";
import { Acc, Icon } from "./ui";

export type Service = "gmail" | "outlook" | "slack" | "sheets";

export const INFO: Record<Service, { name: string; does: string; need: string[] }> = {
  gmail: {
    name: "Gmail", does: "Reads invoice emails from your inbox and sends correction emails to vendors.",
    need: ["The Gmail address invoices arrive at", "An app password (Google account > Security > 2-Step Verification > App passwords). Your normal password will not work."],
  },
  outlook: {
    name: "Outlook / Microsoft 365", does: "Reads invoice emails from your inbox and sends correction emails to vendors.",
    need: ["The mailbox address invoices arrive at", "Tenant ID, client ID and client secret of an app registration in Microsoft Entra", "Permissions Mail.Read, and Mail.Send to send replies. Your IT admin can set this up."],
  },
  sheets: {
    name: "Google Sheets", does: "Reads your purchase orders, goods received and vendor list from a Google Sheet.",
    need: ["A sheet with the three tabs of the template (Vendors, PurchaseOrders, POLines)", "Shared as \"anyone with the link can view\", or a service account key"],
  },
  slack: {
    name: "Slack", does: "Posts an alert when an invoice or email needs a person, and a summary at the end of each day.",
    need: ["An incoming webhook URL for the channel (Slack > Apps > Incoming Webhooks > Add to a channel)"],
  },
};

export function isConnected(c: Connections, s: Service): boolean {
  if (s === "slack") return c.slack.configured;
  if (s === "sheets") return !!c.po_source.options?.sheets.connected;
  return !!c.mailboxes.find((m) => m.name === s)?.configured;
}

function detail(c: Connections, s: Service): string {
  const f = c.fields[s] ?? [];
  if (s === "gmail") return f.find((x) => x.key === "GMAIL_ADDRESS")?.value ?? "Connected";
  if (s === "outlook") return f.find((x) => x.key === "OUTLOOK_MAILBOX")?.value ?? "Connected";
  if (s === "slack") return `Alerts on, daily summary at ${c.slack.digest_time ?? "(off)"}`;
  return c.po_source.options?.in_use === "sheets" ? `${c.po_source.counts?.purchase_orders ?? 0} purchase orders` : "Connected, not in use";
}

export function StatusPill({ on, text }: { on: boolean; text?: string }) {
  return <span className={`pill ${on ? "pill-on" : "pill-off"}`}>{on ? <Icon.check /> : <Icon.minus />}{text ?? (on ? "Connected" : "Not connected")}</span>;
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="acc-group">
      <h2>{title}</h2>
      <div className="acc-list">{children}</div>
    </section>
  );
}

function Fixed({ title, sub, pill, children }: { title: string; sub: string; pill: ReactNode; children: ReactNode }) {
  return <Acc title={title} sub={sub} right={pill}><div className="acc-stack">{children}</div></Acc>;
}

/** The Connections tab: one line per connection, details on click. */
export function ConnectionsBoard({ extra }: { extra?: ReactNode }) {
  const conn = useQuery({ queryKey: ["connections"], queryFn: api.connections, refetchInterval: 20000 });
  const c = conn.data;
  if (conn.isError) return <div className="banner o-rejected"><Icon.cross />Could not load connections: {(conn.error as Error).message}</div>;
  if (!c) return <div className="empty">Loading…</div>;
  const services: Service[] = ["gmail", "outlook", "sheets", "slack"];
  const on = services.filter((s) => isConnected(c, s));
  return (
    <>
      <div className="summary-bar">
        <div><b>{on.length} of {services.length}</b> optional connections set up</div>
      </div>

      <Group title="Where invoices come from">
        <Fixed title="Upload" sub="Drop a PDF, photo or saved email" pill={<StatusPill on text="Always on" />}>
          <p>Anyone can add invoices by hand on the Invoices tab. Nothing to set up.</p>
          <div className="card-actions"><Link className="btn btn-sm" to="/invoices#add">Go to Invoices</Link></div>
        </Fixed>
        <ServiceRow service="gmail" c={c} />
        <ServiceRow service="outlook" c={c} />
        <Fixed title="Demo mailbox" sub="13 sample emails for trying the inbox" pill={<StatusPill on text="Ready" />}>
          <p>Invoices, a statement, a quote, a newsletter and a fake bank-change request. Use it to see how emails are sorted.</p>
          <div className="card-actions"><Link className="btn btn-sm" to="/inbox">Open the inbox</Link></div>
        </Fixed>
      </Group>

      <Group title="Where purchase orders come from">
        <PoSources c={c} />
      </Group>

      <Group title="Where alerts go">
        <ServiceRow service="slack" c={c} />
        <Fixed title="Notifications tab" sub="Every alert and daily summary, sent or not" pill={<StatusPill on text="Always on" />}>
          <p>Without Slack, alerts are only kept here.</p>
          <div className="card-actions"><Link className="btn btn-sm" to="/notifications">Open notifications</Link></div>
        </Fixed>
      </Group>
      {extra}
    </>
  );
}

export function ServiceRow({ service, c, open = false }: { service: Service; c: Connections; open?: boolean }) {
  const qc = useQueryClient();
  const on = isConnected(c, service);
  const [editing, setEditing] = useState(false);
  const [result, setResult] = useState<ConnectResult | null>(null);
  const done = (r: ConnectResult) => {
    setResult(r);
    if (r.connections) qc.setQueryData(["connections"], r.connections);
    qc.invalidateQueries({ queryKey: ["mail"] });
    qc.invalidateQueries({ queryKey: ["records"] });
    if (r.ok) setEditing(false);
  };
  const test = useMutation({ mutationFn: () => api.testConnection(service), onSuccess: setResult });
  const off = useMutation({ mutationFn: () => api.disconnect(service), onSuccess: done });
  const info = INFO[service];
  return (
    <Acc title={info.name} sub={on ? detail(c, service) : info.does} right={<StatusPill on={on} />} open={open}>
      <div className="acc-stack">
        {on && <p>{info.does}</p>}
        {!on && !editing && (
          <div className="card-need">
            <span>You need:</span>
            <ul>{info.need.map((n) => <li key={n}>{n}</li>)}</ul>
          </div>
        )}
        {result && <div className={`conn-result ${result.ok ? "ok" : "bad"}`}>{result.ok ? <Icon.check /> : <Icon.cross />}{result.message}</div>}
        {editing ? (
          <ConnectForm service={service} fields={c.fields[service] ?? []} onDone={done} onCancel={() => setEditing(false)} />
        ) : (
          <div className="card-actions">
            {on ? (
              <>
                <button className="btn btn-sm" disabled={test.isPending} onClick={() => test.mutate()}>{test.isPending ? <span className="spinner" /> : "Test connection"}</button>
                <button className="btn btn-sm btn-ghost" onClick={() => setEditing(true)}>Change details</button>
                <button className="btn btn-sm btn-ghost btn-danger" disabled={off.isPending} onClick={() => off.mutate()}>Disconnect</button>
              </>
            ) : (
              <button className="btn btn-sm btn-primary" onClick={() => setEditing(true)}>Connect {service === "sheets" ? "Google Sheets" : info.name.split(" ")[0]}</button>
            )}
          </div>
        )}
      </div>
    </Acc>
  );
}

function ConnectForm({ service, fields, onDone, onCancel }: {
  service: Service; fields: ConnectionField[]; onDone: (r: ConnectResult) => void; onCancel: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.key, f.secret ? "" : f.value ?? ""])));
  const save = useMutation({ mutationFn: () => api.connect(service, values), onSuccess: onDone });
  return (
    <form className="conn-form" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      {fields.map((f) => (
        <label key={f.key}>
          <span>{f.label}</span>
          {f.key === "GOOGLE_SERVICE_ACCOUNT_JSON" ? (
            <textarea className="input" rows={3} placeholder={f.set ? "Saved. Paste a new key to replace it." : f.hint}
                      value={values[f.key] ?? ""} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          ) : (
            <input className="input" type={f.secret ? "password" : "text"} autoComplete="off"
                   placeholder={f.secret && f.set ? "Saved. Leave empty to keep it." : f.hint}
                   value={values[f.key] ?? ""} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          )}
        </label>
      ))}
      <div className="card-actions">
        <button className="btn btn-sm btn-primary" type="submit" disabled={save.isPending}>{save.isPending ? <><span className="spinner" /> Checking…</> : "Save and test"}</button>
        <button className="btn btn-sm btn-ghost" type="button" onClick={onCancel}>Cancel</button>
      </div>
      {save.isError && <p className="err" style={{ margin: 0 }}>{(save.error as Error).message}</p>}
      <p className="faint" style={{ margin: 0, fontSize: 11.5 }}>Saved on this server only. Passwords and keys are never shown again.</p>
    </form>
  );
}

/** One line for Today: what is set up and what is not. */
export function SetupLine() {
  const conn = useQuery({ queryKey: ["connections"], queryFn: api.connections, refetchInterval: 20000 });
  const c = conn.data;
  if (!c) return null;
  const items: [string, boolean][] = [
    ["Purchase orders", !c.po_source.error],
    ["Email inbox", c.mailboxes.some((m) => m.name !== "folder" && m.configured)],
    ["Email to vendors", !!c.send_via],
    ["Slack", c.slack.configured],
  ];
  const missing = items.filter(([, ok]) => !ok).length;
  return (
    <div className="setup-line">
      <span className="setup-line-label">Setup</span>
      {items.map(([n, ok]) => <span key={n} className={`chip ${ok ? "chip-on" : ""}`}>{ok ? <Icon.check /> : <Icon.minus />}{n}</span>)}
      <Link to="/connections" className="setup-line-link">{missing ? `Connect ${missing} more` : "Manage"}</Link>
    </div>
  );
}
