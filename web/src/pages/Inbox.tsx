import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Mail, type MailKey, type MailLabel, type SyncResult } from "../api";
import { Icon } from "../components/ui";
import { when } from "../lib/format";

export const LABEL_TEXT: Record<MailLabel, string> = {
  invoice: "Invoice", not_invoice: "Not an invoice", suspicious: "Suspicious", unsure: "Not sure",
};
const LABEL_CLASS: Record<MailLabel, string> = {
  invoice: "o-auto_approved", not_invoice: "o-failed", suspicious: "o-rejected", unsure: "o-needs_review",
};
const LABEL_ICON: Record<MailLabel, () => JSX.Element> = {
  invoice: Icon.check, not_invoice: Icon.minus, suspicious: Icon.cross, unsure: Icon.bang,
};
const STATUS_TEXT: Record<string, string> = {
  processed: "Checked", held: "Held for a person", archived: "Archived", released: "Released", dismissed: "Dismissed",
};
const FILTERS: (MailLabel | "")[] = ["", "invoice", "suspicious", "unsure", "not_invoice"];

export function MailLabelBadge({ label }: { label: MailLabel }) {
  const I = LABEL_ICON[label];
  return <span className={`badge ${LABEL_CLASS[label]}`}><I />{LABEL_TEXT[label]}</span>;
}

function KeyChip({ name, k }: { name: string; k: MailKey }) {
  return (
    <span className={`chip key ${k.ok ? "key-ok" : "key-no"}`} title={k.note}>
      {k.ok ? <Icon.check /> : <Icon.cross />}{name}{k.value ? `: ${k.value}` : ""}
    </span>
  );
}

export function KeyChips({ keys }: { keys: Mail["keys"] }) {
  return (
    <span className="chips" aria-label={`Matching keys ${keys.matched} of 3`}>
      <KeyChip name="PO" k={keys.po_number} />
      <KeyChip name="Vendor" k={keys.vendor} />
      <KeyChip name="Buyer" k={keys.buyer} />
    </span>
  );
}

export default function Inbox() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<MailLabel | "">("");
  const [open, setOpen] = useState<string | null>(null);
  const [last, setLast] = useState<SyncResult | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const list = useQuery({ queryKey: ["mail"], queryFn: api.mails, refetchInterval: 4000 });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["mail"] }); qc.invalidateQueries({ queryKey: ["runs"] }); qc.invalidateQueries({ queryKey: ["stats"] }); };
  const sync = useMutation({ mutationFn: api.syncMail, onSuccess: (r) => { setLast(r); refresh(); } });
  const upload = useMutation({ mutationFn: api.uploadMail, onSuccess: (m) => { setOpen(m.id); refresh(); } });

  const mails = (list.data?.mails ?? []).filter((m) => !filter || m.label === filter);
  const stats = list.data?.stats ?? {};
  const total = Object.values(stats).reduce((a, n) => a + (n ?? 0), 0);

  return (
    <>
      <div className="dash-head">
        <div>
          <h1>Inbox</h1>
          <p>Emails with invoices are checked automatically. Suspicious or unclear ones wait here for you.</p>
        </div>
      </div>
      <div className="dash-grid">
        <section className="panel" aria-label="Emails">
          <div className="panel-head">
            <h2>Emails</h2>
          </div>
          <div className="filters">
            <div className="tabs" role="tablist" aria-label="Filter by label">
              {FILTERS.map((f) => (
                <button key={f || "all"} role="tab" className="tab" aria-selected={filter === f} onClick={() => setFilter(f)}>
                  {f ? LABEL_TEXT[f] : "All emails"}<span className="count">{f ? stats[f] ?? 0 : total}</span>
                </button>
              ))}
            </div>
          </div>
          {list.isError ? (
            <div className="panel-body err">Could not load emails: {(list.error as Error).message}</div>
          ) : !list.data ? (
            <div className="empty">Loading…</div>
          ) : mails.length === 0 ? (
            <div className="empty"><p>{total ? "No emails with this label." : "No emails yet. Check the demo mailbox or drop an .eml file."}</p></div>
          ) : (
            <ul className="mail-list">
              {mails.map((m) => <MailRow key={m.id} m={m} open={open === m.id} toggle={() => setOpen(open === m.id ? null : m.id)} onChange={refresh} />)}
            </ul>
          )}
        </section>
        <aside className="dash-side">
          <section className="panel">
            <div className="panel-head"><h2>Check a mailbox</h2></div>
            <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {(list.data?.mailboxes ?? []).filter((b) => b.configured).map((b) => (
                <div key={b.name} className="box-row">
                  <div className="cell-2">
                    <b>{b.label}</b>
                    <span className="sub">{b.name === "folder" ? "Sample emails" : b.detail}</span>
                  </div>
                  <button className="btn btn-sm" disabled={sync.isPending} onClick={() => sync.mutate(b.name)}>
                    {sync.isPending && sync.variables === b.name ? <span className="spinner" /> : "Check now"}
                  </button>
                </div>
              ))}
              {sync.isError && <p className="err" style={{ margin: 0 }}>{(sync.error as Error).message}</p>}
              {last && (
                <p className="faint" style={{ margin: 0, fontSize: 12.5 }}>
                  {last.fetched} email(s) read, {last.new} new{last.new ? ": " + Object.entries(last.by_label).map(([k, n]) => `${n} ${LABEL_TEXT[k as MailLabel].toLowerCase()}`).join(", ") : ""}. {last.runs_started} invoice(s) sent for checking.
                </p>
              )}
              <button className="btn btn-sm btn-ghost" style={{ alignSelf: "flex-start" }} disabled={upload.isPending} onClick={() => file.current?.click()}>
                {upload.isPending ? <span className="spinner" /> : <Icon.upload />} Drop in one email (.eml)
              </button>
              <input ref={file} type="file" accept=".eml,message/rfc822" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
              {upload.isError && <p className="err" style={{ margin: 0 }}>{(upload.error as Error).message}</p>}
              <Link to="/connections" style={{ fontSize: 13 }}>Connect Gmail or Outlook</Link>
            </div>
          </section>
        </aside>
      </div>
    </>
  );
}

function MailRow({ m, open, toggle, onChange }: { m: Mail; open: boolean; toggle: () => void; onChange: () => void }) {
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const release = useMutation({ mutationFn: () => api.releaseMail(m.id, reviewer, note), onSuccess: onChange });
  const dismiss = useMutation({ mutationFn: () => api.dismissMail(m.id, reviewer, note), onSuccess: onChange });
  const err = (release.error ?? dismiss.error) as Error | null;
  return (
    <li className={`mail ${open ? "open" : ""} ${m.status === "held" ? "held" : ""}`}>
      <button className="mail-head" onClick={toggle} aria-expanded={open}>
        <MailLabelBadge label={m.label} />
        <div className="cell-2" style={{ minWidth: 0 }}>
          <span className="mail-subject">{m.subject}</span>
          <span className="sub">{m.sender_name ? `${m.sender_name} <${m.sender}>` : m.sender} · {when(m.received_at ?? m.created_at)}</span>
        </div>
        <span className="mail-status faint">{STATUS_TEXT[m.status] ?? m.status}{m.run_ids.length ? `, ${m.run_ids.length} invoice${m.run_ids.length > 1 ? "s" : ""}` : ""}</span>
      </button>
      {open && (
        <div className="mail-body">
          <ul className="mail-reasons">{m.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="faint" style={{ fontSize: 12 }}>Matching keys {m.keys.matched}/3{m.keys.confirmed ? ", confirmed" : ""}:</span>
            <KeyChips keys={m.keys} />
          </div>
          {m.attachments.length > 0 && (
            <table className="t" style={{ marginTop: 6 }}>
              <thead><tr><th>Attachment</th><th>Reads as</th><th>Run</th></tr></thead>
              <tbody>
                {m.attachments.map((a) => (
                  <tr key={a.sha256 + a.filename}>
                    <td>{a.filename} <span className="faint">· {(a.size / 1024).toFixed(0)} KB</span></td>
                    <td>{a.kind_label}</td>
                    <td>{a.run_id ? <Link to={`/runs/${a.run_id}/review`}>{a.existing ? "Already processed" : "Open decision"}</Link> : <span className="faint">Not sent</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {m.snippet && <p className="mail-snippet">{m.snippet}</p>}
          {m.note && <p className="faint" style={{ margin: 0, fontSize: 12.5 }}>{m.note}</p>}
          {m.status === "held" && (
            <div className="mail-decide">
              <input className="input" placeholder="Your name" value={reviewer} onChange={(e) => setReviewer(e.target.value)} aria-label="Reviewer name" />
              <input className="input" placeholder="Why (required)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Note" style={{ flex: 1 }} />
              <button className="btn btn-sm" disabled={release.isPending || !reviewer.trim() || !note.trim()} onClick={() => release.mutate()}
                      title="Send its documents to the pipeline. Every rule still applies.">Release to pipeline</button>
              <button className="btn btn-sm btn-ghost" disabled={dismiss.isPending || !reviewer.trim() || !note.trim()} onClick={() => dismiss.mutate()}>Dismiss</button>
              {err && <p className="err" style={{ margin: 0, width: "100%" }}>{err.message}</p>}
            </div>
          )}
        </div>
      )}
    </li>
  );
}
