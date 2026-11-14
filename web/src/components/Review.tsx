import { Fragment, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { api, type Issue, type ReviewAction, type RunDetail } from "../api";
import {
  ACTION_LABEL, humanize, money, outcomeLabel, qty, stamp, when,
} from "../lib/format";
import {
  asDuplicates, asLineMatches, asMatch, invoiceLines, ledger, needsDualApproval, priceLines,
} from "../lib/shapes";
import { KeyChips, MailLabelBadge } from "../pages/Inbox";
import { Icon, KV, OutcomeBadge, ReasonChips, StatusChip, Toast } from "./ui";

// ------------------------------------------------------------------ verdict

export function Verdict({ run, compact = false, animate = false, children }: {
  run: RunDetail; compact?: boolean; animate?: boolean; children?: ReactNode;
}) {
  const label = run.outcome === "auto_approved" ? "Auto-approved" : outcomeLabel(run.outcome);
  return (
    <section className={`verdict o-${run.outcome} ${compact ? "verdict-compact" : ""}`} aria-label="Decision">
      <div className={`stamp ${animate ? "stamp-in" : ""}`}>{label}</div>
      <div className="verdict-text">
        <p className="what">{run.verdict || outcomeLabel(run.outcome)}</p>
        <div className="next">
          {run.recommended_action && <span>Next step: <b>{run.recommended_action}</b></span>}
        </div>
      </div>
      {children && <div className="verdict-side">{children}</div>}
    </section>
  );
}

// ------------------------------------------------------------------ review screen

export function Review({ run }: { run: RunDetail }) {
  const [audit, setAudit] = useState(false);
  const head = useRef<HTMLDivElement>(null);
  const { hash } = useLocation();
  useEffect(() => { if (!hash) head.current?.focus(); }, [run.id, hash]);
  useEffect(() => {
    if (!hash) return;
    const id = decodeURIComponent(hash.slice(1));
    const find = () => document.getElementById(id) ?? (id.startsWith("field-") ? document.getElementById("fields") : null);
    const el = find();
    if (!el) return;
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    // panels that load after this one push it down; aim again once they have settled
    const t = window.setTimeout(() => find()?.scrollIntoView({ behavior: "smooth", block: "center" }), 700);
    return () => window.clearTimeout(t);
  }, [hash, run.id]);

  return (
    <>
      <div ref={head} tabIndex={-1} style={{ outline: "none" }}>
        <Verdict run={run}>
          <button className="btn btn-sm btn-ghost" onClick={() => window.open(api.memoUrl(run.id), "_blank", "noopener")}>
            <Icon.doc /> Decision memo
          </button>
        </Verdict>
      </div>

      <div className="review-grid">
        <div className="review-main">
          <section className="panel">
            <div className="panel-head"><h2>Why</h2></div>
            <div className="panel-body">
              <IssueList run={run} />
              <ReadFields run={run} />
              {(run.issues?.length ?? 0) > 0 ? (
                <details className="more-text"><summary>Full explanation</summary><p className="explain">{run.explanation || run.verdict}</p></details>
              ) : <p className="explain">{run.explanation || run.verdict}</p>}
              {run.warnings.length > 0 && (
                <ul className="warn-list">
                  {run.warnings.map((w) => <li key={w} className="banner o-needs_review"><Icon.bang />{w}</li>)}
                </ul>
              )}
            </div>
          </section>
          <VendorDraftPanel run={run} />
          <InvestigationPanel run={run} />
          <RulesTable run={run} />
          <LineTable run={run} />
          <AllFieldsPanel run={run} />
        </div>
        <aside className="review-side">
          <MailPanel run={run} />
          <DuplicatesPanel run={run} />
          <MatchPanel run={run} />
          <RecordPanel run={run} />
        </aside>
      </div>

      <ActionBar run={run} onAudit={() => setAudit(true)} />
      {audit && <AuditDrawer run={run} onClose={() => setAudit(false)} />}
    </>
  );
}

// ------------------------------------------------------------------ rules

const SEVERITY_TEXT: Record<Issue["severity"], string> = { reject: "Rejected because", review: "Needs a person because", fail: "Stopped because", ok: "Approved" };

function IssueList({ run }: { run: RunDetail }) {
  const issues = run.issues ?? [];
  if (!issues.length) return null;
  return (
    <ul className="issues" aria-label="What was found">
      {issues.map((i, n) => (
        <li key={n} className={`issue sev-${i.severity}`}>
          <div>
            {n === 0 && <div className="issue-kind">{SEVERITY_TEXT[i.severity]}</div>}
            <div className="issue-text">{i.text}</div>
          </div>
          {i.run ? <Link to={`/runs/${i.run}/review`}>{i.link}</Link> : <a href={`#${i.anchor}`}>{i.link}</a>}
        </li>
      ))}
    </ul>
  );
}

const CORE_FIELDS: [string, string, (v: Record<string, unknown>) => unknown][] = [
  ["invoice_number", "Invoice number", (v) => v.invoice_number],
  ["invoice_date", "Invoice date", (v) => v.invoice_date],
  ["vendor_name", "Vendor", (v) => v.vendor_name],
  ["po_reference", "PO on the invoice", (v) => v.po_reference],
  ["total", "Total", (v) => v.total_cents],
  ["currency", "Currency", (v) => v.currency],
];

function ReadFields({ run }: { run: RunDetail }) {
  const inv = (run.invoice ?? {}) as Record<string, unknown>;
  if (!Object.keys(inv).length) return null;
  return (
    <dl className="read-fields" id="fields" aria-label="What was read from the invoice">
      {CORE_FIELDS.map(([key, label, get]) => {
        const v = get(inv);
        const missing = v == null || v === "";
        return (
          <div key={key} id={`field-${key}`} className={missing ? "missing" : ""}>
            <dt>{label}</dt>
            <dd>{missing ? "Missing" : key === "total" ? money(v as number, run.currency) : String(v)}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function RulesTable({ run }: { run: RunDetail }) {
  const [showAll, setShowAll] = useState(false);
  const flagged = run.rules.filter((r) => r.status !== "pass" && r.status !== "not_applicable");
  const shown = showAll || !flagged.length ? run.rules : flagged;
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const counts = run.rules.reduce<Record<string, number>>((a, r) => ({ ...a, [r.status]: (a[r.status] ?? 0) + 1 }), {});
  const toggle = (k: string) => setOpen((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  return (
    <section className="panel" id="rules">
      <div className="panel-head">
        <h2>What was checked</h2>
        <div className="rules-summary">
          {(["pass", "fail", "error", "cannot_run", "not_applicable"] as const).filter((k) => counts[k]).map((k) => (
            <span key={k} className={`badge s-${k}`}>{counts[k]} {k === "pass" ? "passed" : k === "fail" ? "failed" : humanize(k).toLowerCase()}</span>
          ))}
        </div>
      </div>
      {run.rules.length === 0 ? <div className="empty">No rule results recorded.</div> : (
        <div className="table-wrap">
          <table className="t">
            <thead><tr><th>Rule</th><th>Check</th><th className="rule-status">Result</th><th>Finding</th><th /></tr></thead>
            <tbody>
              {shown.map((r) => {
                const hasEv = Object.keys(r.evidence ?? {}).length > 0;
                const isOpen = open.has(r.rule);
                return (
                  <Fragment key={r.rule}>
                    <tr id={`rule-${r.rule}`} className={`rule-row ${r.status === "fail" || r.status === "error" ? (r.outcome === "rejected" ? "bad" : "hl") : ""}`}>
                      <td className="mono">{r.rule}</td>
                      <td style={{ fontWeight: 500 }}>{r.title}</td>
                      <td><StatusChip status={r.status} /></td>
                      <td>
                        {r.message}
                        {r.reason && r.status !== "pass" && r.outcome && <div className="faint" style={{ marginTop: 4, fontSize: 12 }}>Leads to: {outcomeLabel(r.outcome)}</div>}
                      </td>
                      <td className="r">
                        {hasEv && (
                          <button className={`rule-toggle ${isOpen ? "open" : ""}`} aria-expanded={isOpen} onClick={() => toggle(r.rule)}>
                            Evidence <Icon.caret />
                          </button>
                        )}
                      </td>
                    </tr>
                    {isOpen && hasEv && (
                      <tr className="rule-ev"><td /><td colSpan={4}><KV data={r.evidence} /></td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {flagged.length > 0 && flagged.length < run.rules.length && (
        <button className="more-toggle" aria-expanded={showAll} onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Show only the checks that need attention" : `Show the ${run.rules.length - flagged.length} checks that passed`}
        </button>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ lines

function LineTable({ run }: { run: RunDetail }) {
  const inv = invoiceLines(run.invoice);
  const price = priceLines(run.rules);
  const matches = asLineMatches(run.line_matches);
  const cur = run.currency;
  if (!inv.length) return null;
  const tol = run.rules.find((r) => r.rule === "R10")?.evidence?.formula;
  const basis = run.rules.find((r) => r.rule === "R03")?.evidence?.tax_basis;
  const inclusive = basis === "inclusive" || basis === "inclusive_inferred";
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Invoice lines against the PO</h2>
        {inclusive && <span className="chip" title="The PO is priced net of tax, so each line is compared net of the tax inside it.">Prices include tax · compared net{basis === "inclusive_inferred" ? " (inferred from the totals)" : ""}</span>}
        {typeof tol === "string" && <span className="faint" style={{ fontSize: 12, marginLeft: "auto" }}>Tolerance per line: {tol}</span>}
      </div>
      <div className="table-wrap">
        <table className="t">
          <thead>
            <tr>
              <th>#</th><th>Invoice line</th><th>PO line</th>
              <th className="r">PO price</th><th className="r">Billed price</th>
              <th className="r">Expected</th><th className="r">Billed</th><th className="r">Variance</th><th className="r">Allowed</th><th />
            </tr>
          </thead>
          <tbody>
            {inv.map((l, i) => {
              const p = price.find((x) => x.invoice_line === i + 1);
              const m = matches.find((x) => x.inv_index === i);
              const bundle = m?.bundle.length ? m.bundle : p?.po_lines.length ? p.po_lines : [];
              const poLine = bundle.length ? bundle[0] : p?.po_line ?? m?.po_line_no ?? null;
              const unmatched = run.po_number != null && poLine == null;
              const options = m?.bundle_options ?? [];
              return (
                <tr key={i} id={`line-${i + 1}`} className={p && !p.ok ? "hl" : unmatched ? "hl" : ""}>
                  <td className="faint">{i + 1}</td>
                  <td>
                    <div className="cell-2"><span>{l.description}</span><span className="sub">{qty(l.qty_milli)} × {money(l.unit_price_cents, cur)}{l.sku ? ` · ${l.sku}` : ""}</span></div>
                  </td>
                  <td>
                    {bundle.length ? <div className="cell-2"><span>Lines {bundle.join(" + ")}</span><span className="sub">bundle</span></div>
                      : poLine != null ? `Line ${poLine}`
                      : <div className="cell-2"><span className={unmatched ? "var-pos" : "faint"}>{run.po_number ? "No match" : "No PO"}</span>
                          {options.length > 0 && <span className="sub">could be {options.map((o) => o.join("+")).join(" or ")}</span>}</div>}
                  </td>
                  <td className="r num">{p ? money(p.po_unit, cur) : "—"}</td>
                  <td className="r num">{money(p && inclusive ? p.invoice_unit : l.unit_price_cents, cur)}</td>
                  <td className="r num">{p ? money(p.expected, cur) : "—"}</td>
                  <td className="r num">{money(p?.billed ?? l.amount_cents, cur)}</td>
                  <td className={`r num ${p && !p.ok ? "var-pos" : "var-ok"}`}>{p ? money(p.variance, cur, { signed: true }) : "—"}</td>
                  <td className="r num">{p ? money(p.allowed, cur) : "—"}</td>
                  <td>{p ? <StatusChip status={p.ok ? "pass" : "fail"} /> : unmatched ? <StatusChip status="fail" /> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ side panels

function DuplicatesPanel({ run }: { run: RunDetail }) {
  const d = asDuplicates(run.duplicates);
  const r13 = run.rules.find((r) => r.rule === "R13");
  const tone = d?.kind === "exact" ? "o-rejected" : d && d.kind !== "none" ? "o-needs_review" : "";
  return (
    <section id="duplicates" className={`panel ${tone}`} style={tone ? { borderColor: "var(--c)" } : undefined}>
      <div className="panel-head"><h3>Duplicate check</h3>{r13 && <span style={{ marginLeft: "auto" }}><StatusChip status={r13.status} /></span>}</div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {!d ? <p className="faint">No duplicate data recorded.</p> : d.hits.length === 0 ? (
          <p className="muted">{d.ref_check_ran ? "No earlier invoice matches by file hash, invoice number, or amount, date and lines." : "Invoice-number check could not run (no number). File hash and amount/date checks found nothing."}</p>
        ) : d.hits.map((h) => (
          <div key={h.run_id} className="banner" style={{ flexDirection: "column", gap: 6 }}>
            <div>
              {d.kind === "exact" ? "Same obligation as " : "Resembles "}
              <b>{h.invoice_number ?? "an earlier invoice"}</b>
              {" · "}<Link to={`/runs/${h.run_id}/review`}>open original ({h.run_id})</Link>
            </div>
            <div className="chips">
              {h.signals.map((s) => <span key={s} className="chip">{humanize(s)}</span>)}
              <span className="chip">similarity {h.score.toFixed(2)}</span>
            </div>
          </div>
        ))}
        {d && !d.ref_check_ran && d.hits.length === 0 && <p className="faint" style={{ fontSize: 12 }}>A human must confirm this is not a re-sent invoice.</p>}
      </div>
    </section>
  );
}

function MatchPanel({ run }: { run: RunDetail }) {
  const v = asMatch(run.vendor_match, "vendor");
  const p = asMatch(run.po_match, "po");
  const l = ledger(run.rules);
  const cur = run.currency;
  const pct = (x: number) => (l && l.po_total ? Math.max(0, Math.min(100, (x / l.po_total) * 100)) : 0);
  return (
    <section className="panel" id="vendor-po">
      <div className="panel-head"><h3>Vendor and PO</h3></div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <dl className="kv">
          <dt>Vendor</dt>
          <dd>{run.vendor_name ?? "—"}{v && !v.ref ? <span className="err"> (not in the vendor list)</span> : null}</dd>
          <dt>Purchase order</dt>
          <dd>{p?.ref ?? run.po_number ?? "None found"}</dd>
          {p && p.candidates.length > 1 && (<><dt>Could also be</dt><dd>{p.candidates.slice(1).map(([c]) => c).join(", ")}</dd></>)}
        </dl>
        {l && (
          <div id="po-balance">
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>PO balance</div>
            <div className="meter" role="img" aria-label={`Billed before ${money(l.billed_before, cur)}, this invoice ${money(l.this_invoice, cur)}, of ${money(l.po_total, cur)}`}>
              <span style={{ width: `${pct(l.billed_before)}%`, "--c": "var(--ink-3)" } as CSSProperties} />
              <span style={{ width: `${pct(l.this_invoice)}%`, "--c": l.remaining_after < -l.allowance ? "var(--bad)" : "var(--accent)" } as CSSProperties} />
            </div>
            <dl className="kv" style={{ marginTop: 8 }}>
              <dt>PO total</dt><dd className="num">{money(l.po_total, cur)}</dd>
              <dt>Billed before</dt><dd className="num">{money(l.billed_before, cur)}{l.prior_runs.length > 0 && <span className="faint"> · {l.prior_runs.join(", ")}</span>}</dd>
              <dt>This invoice</dt><dd className="num">{money(l.this_invoice, cur)}</dd>
              <dt>Remaining after</dt><dd className="num" style={{ fontWeight: 600, color: l.remaining_after < 0 ? "var(--bad)" : undefined }}>{money(l.remaining_after, cur)}</dd>
              <dt>Allowance</dt><dd className="num">{money(l.allowance, cur)}</dd>
            </dl>
          </div>
        )}
      </div>
    </section>
  );
}

const ACTION_TEXT: Record<string, string> = {
  request_credit_note: "Request a credit note", return_to_vendor: "Return to vendor",
  approve_with_justification: "Approve with justification", hold_for_receipt: "Hold until received",
  verify_bank_change_out_of_band: "Verify bank change by phone", escalate: "Escalate", reject: "Reject",
};

/** Exception investigation agent: the model picks read-only tools, then recommends. Never decides. */
function MailPanel({ run }: { run: RunDetail }) {
  const m = run.mail;
  if (!m) return null;
  return (
    <section className="panel" aria-label="Arrived by email">
      <div className="panel-head"><h3>Arrived by email</h3><MailLabelBadge label={m.label} /></div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <dl className="kv">
          <dt>From</dt><dd>{m.sender_name ? `${m.sender_name} <${m.sender}>` : m.sender}</dd>
          <dt>Subject</dt><dd>{m.subject}</dd>
        </dl>
        <div className="faint" style={{ fontSize: 12 }}>Matching keys {m.keys.matched}/3{m.keys.confirmed ? ", confirmed against the PO book" : ", not confirmed"}</div>
        <KeyChips keys={m.keys} />
        <Link to="/inbox" style={{ fontSize: 12.5 }}>Open in the inbox</Link>
      </div>
    </section>
  );
}

function VendorDraftPanel({ run }: { run: RunDetail }) {
  const qc = useQueryClient();
  const show = ["needs_review", "rejected", "returned"].includes(run.outcome);
  const q = useQuery({ queryKey: ["vendor-draft", run.id, run.outcome], queryFn: () => api.vendorDraft(run.id), enabled: show });
  const [subject, setSubject] = useState<string | null>(null);
  const [body, setBody] = useState<string | null>(null);
  const [name, setName] = useState(() => { try { return localStorage.getItem("cockpit-reviewer") ?? ""; } catch { return ""; } });
  const send = useMutation({
    mutationFn: () => api.sendVendorEmail(run.id, { reviewer: name, subject: subject ?? q.data?.subject ?? "", body: body ?? q.data?.body ?? "" }),
    onSuccess: (r) => {
      try { localStorage.setItem("cockpit-reviewer", name); } catch { /* storage blocked */ }
      qc.setQueryData(["run", run.id], r.run);
      qc.setQueryData(["vendor-draft", run.id, r.run.outcome], r.draft);
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });
  if (!show || !q.data) return null;
  const d = q.data;
  if (!d.available) {
    if (!d.internal_notes.length) return null;
    return (
      <section className="panel" aria-label="Email to vendor">
        <div className="panel-head"><h3>Email to vendor</h3><span className="faint" style={{ fontSize: 12 }}>Not drafted</span></div>
        <div className="panel-body">
          <p style={{ margin: "0 0 6px" }}>{d.why}</p>
          <ul style={{ margin: 0, paddingLeft: 18 }}>{d.internal_notes.map((n) => <li key={n}>{n}</li>)}</ul>
        </div>
      </section>
    );
  }
  const sent = (d.sent ?? []).filter((x) => x.status === "sent");
  const via = d.send_via === "gmail" ? "Gmail" : d.send_via === "outlook" ? "Outlook" : null;
  const canSend = !!via && !!d.to && name.trim().length > 0 && !send.isPending;
  return (
    <section className="panel" aria-label="Email to vendor" id="vendor-email">
      <div className="panel-head">
        <h3>Email to vendor</h3>
        <span className="faint" style={{ fontSize: 12.5 }}>Written from the failed checks. Edit it if you like, then send.</span>
      </div>
      <div className="panel-body draft">
        {sent.length > 0 && (
          <div className="banner o-auto_approved"><Icon.check /><div>Sent to {String(sent[0].payload.to ?? d.to)} {when(sent[0].ts)}{sent.length > 1 ? ` (${sent.length} times)` : ""}. The invoice is marked as returned to the vendor.</div></div>
        )}
        <label className="draft-row"><span>To</span>
          <div>{d.to ?? <span className="err">No billing email on the vendor master. Add one in the PO book.</span>} {d.to && <span className="faint">from the vendor master</span>}</div>
        </label>
        <label className="draft-row"><span>Subject</span>
          <input className="input" value={subject ?? d.subject ?? ""} onChange={(e) => setSubject(e.target.value)} />
        </label>
        <textarea className="input draft-body" rows={Math.min(22, (body ?? d.body ?? "").split("\n").length + 1)} value={body ?? d.body ?? ""} onChange={(e) => setBody(e.target.value)} aria-label="Email text" />
        {d.internal_notes.length > 0 && <div className="banner o-needs_review"><Icon.bang /><div>{d.internal_notes.join(" ")}</div></div>}
        <div className="draft-send">
          <input className="input" placeholder="Your name" value={name} onChange={(e) => setName(e.target.value)} aria-label="Your name" />
          <button className="btn btn-primary" disabled={!canSend} onClick={() => send.mutate()}>
            {send.isPending ? <><span className="spinner" /> Sending…</> : via ? `Send from ${via}` : "Send"}
          </button>
          {!via && <span className="faint" style={{ fontSize: 12.5 }}>Connect Gmail or Outlook on <Link to="/">Today</Link> to send from here.</span>}
          {d.mailto && d.to && <a className="btn btn-ghost btn-sm" href={d.mailto}>Open in my email app</a>}
        </div>
        {send.isError && <p className="err" style={{ margin: 0 }}>{(send.error as Error).message}</p>}
        <p className="faint" style={{ fontSize: 11.5, margin: 0 }}>Goes only to the address on the vendor master, never to whoever emailed the invoice. Sending is recorded in the audit trail.</p>
      </div>
    </section>
  );
}

function InvestigationPanel({ run }: { run: RunDetail }) {
  const qc = useQueryClient();
  const [copied, setCopied] = useState(false);
  const [showSteps, setShowSteps] = useState(false);
  const go = useMutation({
    mutationFn: () => api.investigate(run.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", run.id] }),
  });
  if (run.outcome === "auto_approved" || run.outcome === "processing") return null;
  const inv = (go.data?.investigation ?? run.investigation) || null;
  const f = inv?.findings;
  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch { /* clipboard blocked */ }
  };
  return (
    <section className="panel" aria-label="Investigation">
      <div className="panel-head">
        <h3>Investigation</h3>
        <span className="faint" style={{ fontSize: 12 }}>
          {inv ? `${inv.steps.length} lookup${inv.steps.length === 1 ? "" : "s"} · ${inv.turns} turn${inv.turns === 1 ? "" : "s"} · ${inv.tokens.toLocaleString()} tokens · ${inv.seconds}s`
            : "An agent checks the PO, vendor and history, then recommends. It cannot approve or change anything."}
        </span>
        <button className="btn btn-sm" style={{ marginLeft: "auto" }} disabled={go.isPending} onClick={() => go.mutate()}>
          {go.isPending ? <><span className="spinner" /> Investigating…</> : inv ? "Re-run" : "Investigate this exception"}
        </button>
      </div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {go.isError && <p className="err" style={{ margin: 0 }}>{(go.error as Error).message}</p>}
        {!inv && !go.isPending && <p className="faint" style={{ margin: 0 }}>Not investigated yet. It runs only when you ask, so it costs nothing until then.</p>}
        {go.isPending && <p className="faint" style={{ margin: 0 }}>The agent is checking the purchase order, vendor record and invoice history…</p>}
        {inv && f && (<>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className="chip" style={{ fontWeight: 600 }}>Recommended: {ACTION_TEXT[f.recommended_action] ?? humanize(f.recommended_action)}</span>
            <span className="chip">Confidence: {f.confidence}</span>
            {inv.status !== "submitted" && <span className="chip" style={{ color: "var(--review)" }}>Stopped: {humanize(inv.status)}</span>}
          </div>
          <p style={{ margin: 0 }}>{f.summary}</p>
          {inv.unverified_figures.length > 0 && (
            <div className="banner o-needs_review"><Icon.bang />Not found in the case or the lookups: {inv.unverified_figures.join(", ")}. Check before relying on them.</div>
          )}
          {f.findings.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {f.findings.map((x, i) => <li key={i}>{x.finding} <span className="faint" style={{ fontSize: 12 }}>· {x.evidence}</span></li>)}
            </ul>
          )}
          {f.draft_message && (
            <div style={{ border: "1px solid var(--rule)", borderRadius: "var(--r)", padding: 12 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                <b style={{ fontSize: 13 }}>Draft to {f.draft_message.to}</b>
                <button className="btn btn-sm btn-ghost" style={{ marginLeft: "auto" }}
                        onClick={() => copy(`Subject: ${f.draft_message!.subject}\n\n${f.draft_message!.body}`)}>
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <div style={{ fontSize: 13 }}><span className="faint">Subject:</span> {f.draft_message.subject}</div>
              <p style={{ whiteSpace: "pre-wrap", fontSize: 13, margin: "6px 0 0" }}>{f.draft_message.body}</p>
              <p className="faint" style={{ fontSize: 11.5, margin: "6px 0 0" }}>Draft only. Nothing is sent from here.</p>
            </div>
          )}
          {f.open_questions.length > 0 && (
            <div><span className="faint" style={{ fontSize: 12 }}>Open questions:</span>
              <ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>{f.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul></div>
          )}
          <button className="more-toggle" aria-expanded={showSteps} onClick={() => setShowSteps((v) => !v)} style={{ alignSelf: "flex-start" }}>
            {showSteps ? "Hide" : "Show"} what the agent looked up ({inv.steps.length})
          </button>
          {showSteps && (
            <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
              {inv.steps.map((s, i) => (
                <li key={i} style={{ marginBottom: 4 }}>
                  <code>{s.tool}({Object.entries(s.args).filter(([, v]) => v != null).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})</code>
                  <span className="faint"> · turn {s.turn} · {s.ms} ms</span>
                  <div className="mono faint" style={{ fontSize: 11, overflowWrap: "anywhere" }}>{s.result_preview.slice(0, 240)}{s.result_preview.length > 240 ? "…" : ""}</div>
                </li>
              ))}
            </ol>
          )}
          <p className="faint" style={{ fontSize: 11.5, margin: 0 }}>Advisory. The decision above was made by the rules and is unchanged; a reviewer takes the action.</p>
        </>)}
      </div>
    </section>
  );
}

type Field = { label: string; value: string; section?: string; verified?: boolean | null };
const SECTION_LABEL: Record<string, string> = {
  header: "Invoice details", seller: "Seller", buyer: "Buyer", shipping: "Shipping", payment: "Payment",
  line_items: "Line items", totals: "Totals", terms: "Terms", notes: "Notes", other: "Other",
};
const asFields = (v: unknown): Field[] =>
  Array.isArray(v) ? v.filter((f): f is Field => !!f && typeof f === "object" && "value" in f) : [];

function Verified({ v }: { v?: boolean | null }) {
  if (v === true) return <span className="chip" title="Found in the document text" style={{ color: "var(--ok)" }}>✓ in document</span>;
  if (v === false) return <span className="chip" title="Not found in the document text: check against the PDF" style={{ color: "var(--review)" }}>? not found</span>;
  return <span className="chip" title="Scanned page: no text layer to check against">scan</span>;
}

/** Every company-specific field the reader captured. Never used by the rules: shown for the reviewer and exported. */
function AllFieldsPanel({ run }: { run: RunDetail }) {
  const inv = (run.invoice ?? {}) as Record<string, unknown>;
  const fields = asFields(inv.other_fields);
  const lines = Array.isArray(inv.lines) ? (inv.lines as Record<string, unknown>[]) : [];
  const lineExtras = lines.flatMap((l, i) => asFields(l.extra).map((f) => ({ ...f, line: i + 1 })));
  const groups = new Map<string, Field[]>();
  for (const f of fields) groups.set(f.section ?? "other", [...(groups.get(f.section ?? "other") ?? []), f]);
  const unverified = [...fields, ...lineExtras].filter((f) => f.verified === false).length;
  const dataUrl = api.dataUrl(run.id);
  return (
    <section className="panel" aria-label="All fields on the invoice">
      <div className="panel-head">
        <h3>Everything on this invoice</h3>
        <span className="faint" style={{ fontSize: 12 }}>
          {fields.length + lineExtras.length} field{fields.length + lineExtras.length === 1 ? "" : "s"} beyond the core set
          {unverified ? ` · ${unverified} not found in the text` : ""}
        </span>
        {dataUrl && <a href={dataUrl} style={{ marginLeft: "auto", fontSize: 12.5 }} download>Download data (JSON)</a>}
      </div>
      <div className="panel-body">
        {fields.length + lineExtras.length === 0 ? (
          <p className="faint" style={{ margin: 0 }}>No fields beyond the core invoice fields were found on this document.</p>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: "12px 24px" }}>
            {[...groups.entries()].map(([sec, fs]) => (
              <div key={sec}>
                <div className="label" style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{SECTION_LABEL[sec] ?? humanize(sec)}</div>
                <dl className="kv">
                  {fs.map((f, i) => (
                    <Fragment key={i}><dt>{f.label || "—"}</dt><dd>{f.value} <Verified v={f.verified} /></dd></Fragment>
                  ))}
                </dl>
              </div>
            ))}
            {lineExtras.length > 0 && (
              <div>
                <div className="label" style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>Extra line-item columns</div>
                <dl className="kv">
                  {lineExtras.map((f, i) => (
                    <Fragment key={i}><dt>Line {f.line} · {f.label || "—"}</dt><dd>{f.value} <Verified v={f.verified} /></dd></Fragment>
                  ))}
                </dl>
              </div>
            )}
          </div>
        )}
        <p className="faint" style={{ fontSize: 11.5, margin: "10px 0 0" }}>Captured for the reviewer and for export. Approval rules use only the core fields.</p>
      </div>
    </section>
  );
}

function RecordPanel({ run }: { run: RunDetail }) {
  return (
    <section className="panel">
      <div className="panel-head"><h3>File</h3></div>
      <div className="panel-body">
        <dl className="kv">
          <dt>File</dt><dd><a href={api.fileUrl(run.id)} target="_blank" rel="noreferrer">{run.filename}</a></dd>
          <dt>Received</dt><dd>{stamp(run.created_at)}</dd>
        </dl>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ actions

const ACTION_ORDER: ReviewAction[] = ["approve", "return_to_vendor", "escalate", "reject"];
const PAST: Record<string, string> = { approve: "Approved", reject: "Rejected", return_to_vendor: "Returned to vendor", escalate: "Escalated", email_vendor: "Emailed the vendor" };

function ActionBar({ run, onAudit }: { run: RunDetail; onAudit: () => void }) {
  const qc = useQueryClient();
  const [reviewer, setReviewer] = useState(() => { try { return localStorage.getItem("cockpit-reviewer") ?? ""; } catch { return ""; } });
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const act = useMutation({
    mutationFn: (action: ReviewAction) => api.act(run.id, { action, reviewer: reviewer.trim(), note: note.trim() }),
    onSuccess: (detail, action) => {
      qc.setQueryData(["run", run.id], detail);
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
      setNote("");
      const pending = action === "approve" && detail.outcome === "needs_review";
      setToast(pending ? "First approval recorded. A second reviewer must approve." : `${PAST[action]}: ${detail.invoice_number ?? detail.id}`);
    },
    onError: (e: Error) => setErr(e.message),
  });

  const dual = needsDualApproval(run.rules, run.total_cents);
  const approvals = run.actions.filter((a) => a.action === "approve");
  const allowed = ACTION_ORDER.filter((a) => run.allowed_actions.includes(a));
  const sameApprover = dual && approvals.some((a) => a.reviewer.trim().toLowerCase() === reviewer.trim().toLowerCase());

  const submit = (a: ReviewAction) => {
    setErr(null);
    if (!reviewer.trim()) return setErr("Enter your name. Every action is recorded against a named reviewer.");
    if (!note.trim()) return setErr("Add a note saying why. It goes into the audit trail and the memo.");
    if (a === "approve" && sameApprover) return setErr("The second approval must come from a different reviewer.");
    try { localStorage.setItem("cockpit-reviewer", reviewer.trim()); } catch { /* storage blocked */ }
    act.mutate(a);
  };

  const toastEl = toast && <Toast onClose={() => setToast(null)}>{toast}</Toast>;

  if (!allowed.length) {
    const last = run.actions[run.actions.length - 1];
    return (
      <>
        <div className="banner final-note">
          <Icon.check />
          <span>
            {last
              ? <>{PAST[last.action] ?? humanize(last.action)} by <b>{last.reviewer}</b> on {stamp(last.ts)}. This decision is final. </>
              : run.outcome === "processing" ? "The run is still processing. " : <>{outcomeLabel(run.outcome)} by policy. This decision is final and needs no reviewer. </>}
            <button className="btn btn-sm btn-ghost" onClick={onAudit}>View audit trail</button>
          </span>
        </div>
        {toastEl}
      </>
    );
  }

  return (
    <>
      <section className="actionbar" aria-label="Reviewer decision">
        <div className="meta">
          <OutcomeBadge outcome={run.outcome} />
          <ReasonChips reasons={run.reasons} />
          {dual && (
            <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
              <span className="progress-dots" aria-hidden><i className={approvals.length >= 1 ? "on" : ""} /><i className={approvals.length >= 2 ? "on" : ""} /></span>
              <b>{Math.min(approvals.length, 2)} of 2 approvals</b>
              {approvals[0] && <span className="faint">first by {approvals[0].reviewer}</span>}
            </span>
          )}
          <button className="btn btn-sm btn-ghost" style={{ marginLeft: "auto" }} onClick={onAudit}>Audit trail ({run.actions.length})</button>
        </div>
        <label>
          Reviewer
          <input className="input" value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Your name" autoComplete="name" />
        </label>
        <label>
          Note for the audit trail
          <textarea className="input" rows={2} value={note} onChange={(e) => setNote(e.target.value)}
            placeholder={run.reasons.includes("PRICE_VARIANCE") ? "e.g. Called Northwind; credit note for $60.00 requested" : "Why you are taking this action"} />
        </label>
        <div className="acts">
          {allowed.map((a) => (
            <button key={a} className={`btn ${a === "approve" ? "btn-primary" : a === "reject" ? "btn-danger" : ""}`}
              disabled={act.isPending || (a === "approve" && sameApprover)} onClick={() => submit(a)}
              title={a === "approve" && sameApprover ? "A different reviewer must give the second approval" : undefined}>
              {act.isPending && act.variables === a ? <span className="spinner" /> : null}
              {a === "approve" && dual ? (approvals.length ? "Give second approval" : "Approve (1 of 2)") : ACTION_LABEL[a]}
            </button>
          ))}
        </div>
        {err && <p className="err" role="alert" style={{ gridColumn: "1 / -1" }}>{err}</p>}
      </section>
      {toastEl}
    </>
  );
}

// ------------------------------------------------------------------ audit drawer

function AuditDrawer({ run, onClose }: { run: RunDetail; onClose: () => void }) {
  const closeBtn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeBtn.current?.focus();
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  const decided = run.stages.filter((e) => e.stage === "decision" && e.status !== "started").pop();
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Audit trail">
        <header>
          <h2>Audit trail</h2>
          <button ref={closeBtn} className="btn btn-sm btn-ghost" style={{ marginLeft: "auto" }} onClick={onClose}>Close</button>
        </header>
        <div className="body">
          <ol className="audit">
            <li>
              <div className="who"><b>System</b> received {run.filename}<span className="ts">{stamp(run.created_at)}</span></div>
            </li>
            {decided && (
              <li>
                <div className="who"><b>Policy {run.policy_version}</b> decided <OutcomeBadge outcome={run.actions[0]?.outcome_before ?? run.outcome} /><span className="ts">{stamp(decided.ts)}</span></div>
                <p className="note">{decided.message}</p>
                <p className="note hash">packet {run.packet_hash}</p>
              </li>
            )}
            {run.actions.map((a, i) => (
              <li key={i}>
                <div className="who">
                  <b>{a.reviewer}</b> {ACTION_LABEL[a.action]?.toLowerCase() ?? a.action}
                  <span className="ts">{stamp(a.ts)}</span>
                </div>
                <div className="who" style={{ marginTop: 4 }}>
                  <OutcomeBadge outcome={a.outcome_before} /> <span className="faint">to</span> <OutcomeBadge outcome={a.outcome_after} />
                </div>
                {a.note && <p className="note">“{a.note}”</p>}
              </li>
            ))}
            {run.actions.length === 0 && <li className="faint">No human actions yet.</li>}
          </ol>
        </div>
      </aside>
    </>
  );
}
