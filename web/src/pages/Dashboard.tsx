import { Fragment, useEffect, useRef, useState, type CSSProperties, type DragEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type ApiUsage, type Fixture, type Issue, type Outcome, type RunCreated, type RunSummary, type Stats } from "../api";
import { SetupLine } from "../components/Connections";
import { Icon, OutcomeBadge } from "../components/ui";
import { money, OUTCOME_LABEL, percent, reasonLabel, sourceLabel, when } from "../lib/format";

export default function Dashboard() {
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 3000 });
  const qc = useQueryClient();
  const nav = useNavigate();
  const sync = useMutation({ mutationFn: () => api.syncMail("folder"), onSuccess: () => qc.invalidateQueries() });
  const s = stats.data;
  const by = s?.by_outcome ?? {};
  const decided = (s?.total ?? 0) - (by.processing ?? 0);
  const v = s?.value_cents_by_outcome ?? {};
  const dash = stats.isError ? "—" : "…";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Today</h1>
          <p>{new Date().toLocaleDateString("en-US", { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</p>
        </div>
        <div className="page-actions">
          <button className="btn" disabled={sync.isPending} onClick={() => sync.mutate()}>{sync.isPending ? <span className="spinner" /> : null}Check inbox</button>
          <button className="btn btn-primary" onClick={() => nav("/invoices#add")}>Add invoices</button>
        </div>
      </div>
      {sync.data && <div className="banner o-auto_approved" style={{ marginBottom: 16 }}><Icon.check />Inbox checked: {plural(sync.data.new, "new email")}, {plural(sync.data.runs_started, "invoice")} sent for checking.</div>}
      {sync.isError && <div className="banner o-rejected" style={{ marginBottom: 16 }}><Icon.cross />{(sync.error as Error).message}</div>}

      <SetupLine />
      <section className="tiles" aria-label="Key figures">
        <div className="tile tile-link" role="link" tabIndex={0} onClick={() => nav("/invoices")} onKeyDown={(e) => { if (e.key === "Enter") nav("/invoices"); }}>
          <span className="tile-label">Invoices processed</span>
          <span className="tile-value">{s ? decided : dash}</span>
          <span className="tile-sub">{s ? `${money((v.auto_approved ?? 0) + (v.approved ?? 0) + (v.needs_review ?? 0) + (v.rejected ?? 0) + (v.returned ?? 0), "USD", { whole: true })} in total` : " "}</span>
        </div>
        <div className="tile">
          <span className="tile-label">Approved automatically</span>
          <span className="tile-value">{s ? by.auto_approved ?? 0 : dash}</span>
          <span className="tile-sub">{s && decided ? `${percent(s.stp_rate)} of invoices` : " "}</span>
        </div>
        <div className="tile">
          <span className="tile-label">Rejected</span>
          <span className="tile-value">{s ? by.rejected ?? 0 : dash}</span>
          <span className="tile-sub">{s ? `${money(v.rejected ?? 0, "USD", { whole: true })} not paid` : " "}</span>
        </div>
      </section>

      <NeedsYou />
    </>
  );
}

// ------------------------------------------------------------------ today

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const cvar = (c: string) => ({ "--c": c }) as CSSProperties;

function NeedsYou() {
  const nav = useNavigate();
  const [all, setAll] = useState(false);
  const runs = useQuery({ queryKey: ["runs", "needs_review", ""], queryFn: () => api.runs({ outcome: "needs_review", limit: 50 }), refetchInterval: 4000 });
  const mail = useQuery({ queryKey: ["mail"], queryFn: api.mails, refetchInterval: 4000 });
  const held = (mail.data?.mails ?? []).filter((m) => m.status === "held");
  const items = [
    ...held.map((m) => ({
      key: m.id, at: m.created_at, what: m.label === "suspicious" ? `Suspicious email from ${m.sender}` : `Email to check from ${m.sender}`,
      why: m.reasons[0] ?? "", meta: m.subject, amount: null as string | null, cta: "Look at email", to: "/inbox",
    })),
    ...(runs.data ?? []).map((r) => ({
      key: r.id, at: r.created_at, what: `${r.vendor_name ?? "Unidentified vendor"}${r.invoice_number ? `, invoice ${r.invoice_number}` : ""}`,
      why: r.issues?.[0]?.text ?? r.reasons.map(reasonLabel).join(", "), meta: r.po_number ?? "No PO linked",
      amount: money(r.total_cents, r.currency), cta: r.issues?.[0]?.link ?? "Review", to: issueHref(r.id, r.issues?.[0]),
    })),
  ].sort((a, b) => b.at.localeCompare(a.at));

  return (
    <section className="panel" aria-label="Needs you">
      <div className="panel-head">
        <h2>Needs you</h2>
      </div>
      {!runs.data || !mail.data ? <div className="empty">Loading…</div> : items.length === 0 ? (
        <div className="queue-empty">
          <span className="tick"><Icon.check /></span>
          <div><b>You are up to date.</b><div className="faint" style={{ fontSize: 13 }}>New invoices that fail a check, and emails that look wrong, will appear here.</div></div>
        </div>
      ) : (
        <ul className="queue">
          {(all ? items : items.slice(0, 5)).map((i) => (
            <li key={i.key}>
              <div style={{ minWidth: 0 }}>
                <div className="what">{i.what}</div>
                <div className="why">{i.why}</div>
                <div className="meta">{i.meta}, {when(i.at)}</div>
              </div>
              <div className="go">
                {i.amount && <span className="amount">{i.amount}</span>}
                <button className="btn btn-sm btn-primary" onClick={() => nav(i.to)}>{i.cta}</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {items.length > 5 && (
        <button className="more-toggle" aria-expanded={all} onClick={() => setAll((v) => !v)}>
          {all ? "Show fewer" : `Show all ${items.length}`}
        </button>
      )}
    </section>
  );
}

export function issueHref(id: string, i?: Issue): string {
  if (!i) return `/runs/${id}/review`;
  if (i.run) return `/runs/${i.run}/review`;
  if (i.anchor === "timeline") return `/runs/${id}`;
  return `/runs/${id}/review#${i.anchor}`;
}

function IssueLine({ run }: { run: RunSummary }) {
  const nav = useNavigate();
  const all = run.issues ?? [];
  const i = all[0];
  if (!i || run.outcome === "processing") return null;
  return (
    <div className={`issue-line sev-${i.severity}`}>
      <span>{i.text}{all.length > 1 ? ` (+${all.length - 1} more)` : ""}</span>
      <a href={issueHref(run.id, i)} onClick={(e) => { e.preventDefault(); e.stopPropagation(); nav(issueHref(run.id, i)); }}>{i.link}</a>
    </div>
  );
}

// ------------------------------------------------------------------ health

export function HealthStrip() {
  const { data, isError } = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 15000 });
  if (isError) return <div className="health"><div style={cvar("var(--bad)")}><span className="dot" />API unreachable</div></div>;
  if (!data) return <div className="health"><div className="faint">Checking services…</div></div>;
  const llm = data.extraction_mode !== "local";
  return (
    <div className="health" aria-label="System health">
      <div style={cvar(data.ok ? "var(--ok)" : "var(--bad)")}>
        <span className="dot" />{data.ok ? "Pipeline ready" : "Pipeline degraded"}
      </div>
      <div style={cvar(llm ? "var(--ok)" : "var(--review)")} title={data.llm_model ? `Model ${data.llm_model}` : "Which extractor reads the PDFs"}>
        <span className="dot" />Extraction: {llm ? sourceLabel(data.extraction_mode) : "local parser"}
      </div>
      <div style={cvar(data.api_key_present ? "var(--ok)" : "var(--review)")}>
        <span className="dot" />{data.api_key_present ? "API key present" : "No API key"}
      </div>
      <div title="Policy thresholds version stamped on every decision">Policy {data.policy_version}</div>
    </div>
  );
}

// ------------------------------------------------------------------ run history

const TABS: (Outcome | "")[] = ["", "needs_review", "auto_approved", "approved", "rejected", "returned", "failed"];

export function RunHistory({ outcome, setOutcome, stats, compact = false }: { outcome: Outcome | ""; setOutcome: (o: Outcome | "") => void; stats?: Stats; compact?: boolean }) {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 200);
    return () => clearTimeout(t);
  }, [q]);

  const runs = useQuery({
    queryKey: ["runs", outcome, debounced, compact],
    queryFn: () => api.runs({ outcome: outcome || undefined, q: debounced || undefined, limit: compact ? 6 : 50 }),
    refetchInterval: (query) => ((query.state.data as RunSummary[] | undefined)?.some((r) => r.outcome === "processing") ? 1000 : 4000),
    placeholderData: (prev) => prev,
  });

  const count = (o: Outcome | "") => (o === "" ? stats?.total : stats?.by_outcome?.[o]);
  const open = (r: RunSummary) => nav(r.outcome === "processing" || r.status === "failed" ? `/runs/${r.id}` : `/runs/${r.id}/review`);

  return (
    <section className="panel" aria-label="All invoices">
      <div className="panel-head">
        <h2>{compact ? "Latest invoices" : "All invoices"}</h2>
        {compact && <button className="btn btn-sm btn-ghost" style={{ marginLeft: "auto" }} onClick={() => nav("/invoices")}>See all invoices</button>}
      </div>
      {!compact && <div className="filters">
        <div className="tabs" role="tablist" aria-label="Filter by outcome">
          {TABS.filter((t) => t === "" || t === outcome || (count(t) ?? 0) > 0).map((t) => (
            <button key={t || "all"} role="tab" className="tab" aria-selected={outcome === t} onClick={() => setOutcome(t)}>
              {t === "" ? "All" : OUTCOME_LABEL[t]}
              {count(t) != null && <span className="count">{count(t)}</span>}
            </button>
          ))}
        </div>
        <input className="input search" type="search" placeholder="Search vendor, invoice, PO" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search runs" />
      </div>}
      {runs.isError ? (
        <div className="panel-body"><div className="banner o-rejected">Could not load runs: {(runs.error as Error).message} <button className="btn btn-sm" onClick={() => runs.refetch()}>Retry</button></div></div>
      ) : !runs.data ? (
        <div className="empty">Loading runs…</div>
      ) : runs.data.length === 0 ? (
        <div className="empty">
          {debounced ? <p>No invoices match “{debounced}”.</p> : outcome ? <p>No {OUTCOME_LABEL[outcome].toLowerCase()} invoices.</p> : <p>No invoices yet. Upload a PDF or run a demo invoice.</p>}
          {(debounced || outcome) && <button className="btn btn-sm" onClick={() => { setQ(""); setOutcome(""); }}>Clear filters</button>}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="t">
            <thead>
              <tr>
                <th>Vendor / invoice</th>
                <th>PO</th>
                <th className="r">Amount</th>
                <th>Decision</th>
                <th>Received</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => open(r)} tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") open(r); }}>
                  <td>
                    <div className="cell-2">
                      <span style={{ fontWeight: 600 }}>{r.vendor_name ?? (r.outcome === "processing" ? "Reading…" : "Unidentified vendor")}</span>
                      <span className="sub">{r.invoice_number ?? (r.outcome === "processing" ? r.filename : "No invoice number")}</span>
                    </div>
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>{r.po_number ?? <span className="faint">No PO</span>}</td>
                  <td className="r num">{money(r.total_cents, r.currency)}</td>
                  <td>
                    <div className="cell-2" style={{ gap: 4 }}>
                      <OutcomeBadge outcome={r.outcome} />
                      <IssueLine run={r} />
                    </div>
                  </td>
                  <td>
                    <div className="cell-2">
                      <span>{when(r.created_at)}</span>
                      <span className="sub">{r.filename}</span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ upload

function useStartRun() {
  const nav = useNavigate();
  const qc = useQueryClient();
  return (res: RunCreated) => {
    qc.invalidateQueries({ queryKey: ["runs"] });
    qc.invalidateQueries({ queryKey: ["stats"] });
    nav(`/runs/${res.run_id}`, { state: { existing: res.existing } });
  };
}

export function UploadPanel() {
  const start = useStartRun();
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [localErr, setLocalErr] = useState<string | null>(null);
  const upload = useMutation({ mutationFn: api.upload, onSuccess: start });
  const nav = useNavigate();
  const qc = useQueryClient();
  const mail = useMutation({ mutationFn: api.uploadMail, onSuccess: () => { qc.invalidateQueries(); nav("/inbox"); } });

  const take = (f: File | undefined) => {
    setLocalErr(null);
    if (!f) return;
    if (/\.eml$/i.test(f.name)) return mail.mutate(f);
    if (!/\.(pdf|png|jpe?g|tiff?|bmp|gif|webp)$/i.test(f.name) && !/^(application\/pdf|image\/)/.test(f.type))
      return setLocalErr(`${f.name} is not a PDF or an image. Upload a PDF, JPG, PNG or TIFF invoice.`);
    if (f.size > 10 * 1024 * 1024) return setLocalErr(`${f.name} is larger than 10 MB.`);
    upload.mutate(f);
  };
  const onDrop = (e: DragEvent) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files?.[0]); };

  return (
    <section className="panel">
      <div className="panel-head"><h2>Add invoices</h2></div>
      <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div
          className={`drop ${over ? "over" : ""}`} role="button" tabIndex={0}
          onClick={() => input.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}
        >
          {upload.isPending || mail.isPending ? <span className="spinner" /> : <Icon.upload />}
          <strong>{upload.isPending || mail.isPending ? "Uploading…" : "Drop an invoice here"}</strong>
          <span style={{ fontSize: 12.5 }}>or click to choose a file</span>
        </div>
        <input ref={input} type="file" accept="application/pdf,.pdf,.eml,message/rfc822,image/png,image/jpeg,image/tiff,image/bmp,image/gif,image/webp" hidden onChange={(e) => { take(e.target.files?.[0]); e.target.value = ""; }} />
        {(localErr || upload.isError || mail.isError) && <p className="err" style={{ margin: 0 }}>{localErr ?? ((upload.error ?? mail.error) as Error).message}</p>}
        <div className="needs">
          <h3>What the file can be</h3>
          <ul>
            <li>A PDF invoice, or a clear photo or scan (JPG, PNG, TIFF)</li>
            <li>An email saved as .eml, with the invoice attached</li>
            <li>Up to 10 MB and 10 pages</li>
          </ul>
          <h3>What the invoice must show</h3>
          <ul>
            <li>Vendor name, matching the vendor list in the PO book</li>
            <li>Invoice number and invoice date</li>
            <li>The purchase order number (PO-1042 style)</li>
            <li>Each line: description, quantity, unit price and amount</li>
            <li>Total, plus tax and freight if charged</li>
          </ul>
          <p className="faint">If something is missing, the invoice is not rejected: it goes to Needs you and says exactly what is missing.</p>
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ API usage

const tokens = (n: number | null | undefined) =>
  n == null ? "—" : n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : `${n}`;

export function ApiUsagePanel({ usage }: { usage?: ApiUsage }) {
  return (
    <section className="panel" aria-label="API usage">
      <div className="panel-head">
        <h2>API usage</h2>
        <span className="faint" style={{ fontSize: 12, marginLeft: "auto" }}>Tokens reported by the model API</span>
      </div>
      <div className="panel-body">
        {!usage ? (
          <p className="faint">Loading…</p>
        ) : usage.calls === 0 ? (
          <p className="faint" style={{ margin: 0 }}>No model calls yet. Usage appears here after the first invoice is read by the model.</p>
        ) : (
          <>
            <div style={{ display: "flex", gap: 24, alignItems: "baseline" }}>
              <div><div className="value num" style={{ fontSize: 24, fontWeight: 600 }}>{tokens(usage.total_tokens)}</div><div className="faint" style={{ fontSize: 12 }}>tokens, all time</div></div>
              <div><div className="num" style={{ fontSize: 16, fontWeight: 600 }}>{tokens(usage.today_tokens)}</div><div className="faint" style={{ fontSize: 12 }}>today (UTC)</div></div>
              <div><div className="num" style={{ fontSize: 16, fontWeight: 600 }}>{tokens(usage.avg_tokens_per_invoice)}</div><div className="faint" style={{ fontSize: 12 }}>per invoice</div></div>
            </div>
            <dl className="kv" style={{ marginTop: 12 }}>
              <dt>Calls</dt><dd className="num">{usage.calls} · {plural(usage.invoices, "invoice")}{usage.avg_calls_per_invoice != null ? ` · ${usage.avg_calls_per_invoice} per invoice` : ""}</dd>
              <dt>Tokens</dt><dd className="num">{tokens(usage.input_tokens)} in · {tokens(usage.output_tokens)} out{usage.cached_tokens ? ` · ${tokens(usage.cached_tokens)} cached` : ""}</dd>
              {usage.by_model.map((m) => (<Fragment key={m.model}><dt>{m.model || "unknown model"}</dt><dd className="num">{tokens(m.tokens)} · {plural(m.calls, "call")}</dd></Fragment>))}
              {usage.last_call_at && (<><dt>Last call</dt><dd>{when(usage.last_call_at)}</dd></>)}
            </dl>
            <p className="faint" style={{ fontSize: 11.5, margin: "10px 0 0" }}>Counted from every model response, including retries and refused calls. Kept across demo resets.</p>
          </>
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ demo fixtures

export function DemoPanel() {
  const start = useStartRun();
  const qc = useQueryClient();
  const fixtures = useQuery({ queryKey: ["fixtures"], queryFn: api.fixtures, staleTime: 60_000 });
  const [showMore, setShowMore] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const run = useMutation({ mutationFn: api.runFixture, onSuccess: start });
  const reset = useMutation({
    mutationFn: api.reset,
    onSuccess: () => { setConfirmReset(false); qc.invalidateQueries(); },
  });

  const all = fixtures.data ?? [];
  const demo = all.filter((f) => f.demo).sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
  const extra = all.filter((f) => !f.demo);

  const item = (f: Fixture, i: number | null) => (
    <li key={f.key}>
      <span className="demo-step" aria-hidden>{i ?? ""}</span>
      <div>
        <h4>{f.title}</h4>
        <p>{f.description}</p>
        <div className="expect">
          Expected <OutcomeBadge outcome={f.expected.outcome} />
          {f.expected.reason !== "STP_CLEAN" && <span className="chip">{reasonLabel(f.expected.reason)}</span>}
          {f.kind === "scan" && <span className="chip">Scanned</span>}
          {f.prerequisites.length > 0 && <span>after {f.prerequisites.join(", ")}</span>}
        </div>
      </div>
      <button className="btn btn-sm" disabled={run.isPending} onClick={() => run.mutate(f.key)} aria-label={`Run ${f.title}`}>
        {run.isPending && run.variables === f.key ? <span className="spinner" /> : "Run"}
      </button>
    </li>
  );

  return (
    <section className="panel">
      <div className="panel-head"><h2>Sample invoices</h2></div>
      {fixtures.isError ? (
        <div className="panel-body err">Could not load demo invoices.</div>
      ) : !fixtures.data ? (
        <div className="empty">Loading…</div>
      ) : (
        <>
          <ul className="demo-list">{demo.map((f, i) => item(f, i + 1))}</ul>
          {extra.length > 0 && (
            <>
              <button className="more-toggle" aria-expanded={showMore} onClick={() => setShowMore((v) => !v)}>
                {showMore ? "Hide" : "Show"} {extra.length} more test case{extra.length > 1 ? "s" : ""}
              </button>
              {showMore && <ul className="demo-list" style={{ borderTop: "1px solid var(--rule)" }}>{extra.map((f) => item(f, null))}</ul>}
            </>
          )}
        </>
      )}
      {run.isError && <p className="err" style={{ padding: "0 16px 12px" }}>{(run.error as Error).message}</p>}
      <div className="panel-body" style={{ borderTop: "1px solid var(--rule)" }}>
        {!confirmReset ? (
          <button className="btn btn-sm btn-ghost" onClick={() => setConfirmReset(true)}>Reset demo</button>
        ) : (
          <div className="confirm" role="alert">
            <span>Delete every run and reseed the demo data?</span>
            <button className="btn btn-sm btn-danger" onClick={() => reset.mutate()} disabled={reset.isPending}>
              {reset.isPending ? "Resetting…" : "Delete runs and reseed"}
            </button>
            <button className="btn btn-sm btn-ghost" onClick={() => setConfirmReset(false)}>Cancel</button>
          </div>
        )}
        {reset.isError && <p className="err">{(reset.error as Error).message}</p>}
      </div>
    </section>
  );
}
