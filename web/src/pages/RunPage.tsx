import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { useRunStream } from "../api/useRunStream";
import { Review, Verdict } from "../components/Review";
import { Timeline } from "../components/Timeline";
import { OutcomeBadge } from "../components/ui";
import { duration, money } from "../lib/format";

export default function RunPage({ view }: { view: "live" | "review" }) {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();
  const existing = (location.state as { existing?: boolean } | null)?.existing;

  const run = useQuery({
    queryKey: ["run", id],
    queryFn: () => api.run(id),
    refetchInterval: (q) => {
      const d = q.state.data;
      return d && (d.status === "queued" || d.status === "running") ? 1500 : false;
    },
  });
  const detail = run.data;

  // Was this run still going when the page opened? Only then do we celebrate and auto-advance.
  const sawLive = useRef<boolean | null>(null);
  if (detail && sawLive.current === null) sawLive.current = detail.status === "queued" || detail.status === "running";
  const [justFinished, setJustFinished] = useState(false);

  const onDone = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["run", id] });
    qc.invalidateQueries({ queryKey: ["runs"] });
    qc.invalidateQueries({ queryKey: ["stats"] });
    setJustFinished(true);
  }, [qc, id]);
  const { events } = useRunStream(id, detail, onDone);

  const finished = detail?.status === "done" || detail?.status === "failed";
  const running = !finished;

  // needs_review: once the verdict lands, move the reviewer into the review.
  useEffect(() => {
    if (!justFinished || !finished || view !== "live" || detail?.outcome !== "needs_review") return;
    const t = setTimeout(() => nav(`/runs/${id}/review`, { replace: true }), 1400);
    return () => clearTimeout(t);
  }, [justFinished, finished, detail?.outcome, view, id, nav]);

  // Reset per-run memory when navigating between runs.
  useEffect(() => { sawLive.current = null; setJustFinished(false); }, [id]);

  if (run.isError) {
    const nf = run.error instanceof ApiError && run.error.status === 404;
    return (
      <div className="empty">
        <p>{nf ? `Run ${id} does not exist. It may have been removed by a demo reset.` : `Could not load run ${id}: ${(run.error as Error).message}`}</p>
        <p><Link to="/">Back to the dashboard</Link></p>
      </div>
    );
  }
  if (!detail) return <div className="empty">Loading run…</div>;

  const first = events[0]?.ts ?? detail.created_at;
  const lastTs = events[events.length - 1]?.ts;
  const reviewReady = finished && detail.outcome !== "processing";

  return (
    <>
      <div className="run-head">
        <Link to="/" className="back">← Today</Link>
        <div className="run-title">
          <h1>
            <span>{detail.vendor_name ?? (running ? "Reading invoice…" : "Unidentified vendor")}</span>
            {detail.invoice_number && <span className="muted" style={{ fontWeight: 500 }}>{detail.invoice_number}</span>}
            {detail.total_cents != null && <span className="amt num">{money(detail.total_cents, detail.currency)}</span>}
          </h1>
          <span className="sub">
            {detail.filename}
            {detail.po_number ? ` · ${detail.po_number}` : ""}
          </span>
        </div>
        <div className="right">
          {!finished && <Elapsed start={first} end={lastTs} total={detail.duration_ms} />}
          {view !== "review" && <OutcomeBadge outcome={detail.outcome} />}
        </div>
      </div>

      {existing && (
        <div className="banner o-processing" style={{ marginBottom: 16 }}>
          This exact file (same SHA-256) was processed before, so no new run was created. You are looking at the original run.
        </div>
      )}

      <nav className="viewtabs" aria-label="Run views">
        <Link to={`/runs/${id}`} className={view === "live" ? "active" : ""} replace>Live run</Link>
        <Link to={`/runs/${id}/review`} className={`${view === "review" ? "active" : ""} ${reviewReady ? "" : "disabled"}`} aria-disabled={!reviewReady} replace>
          Decision review {detail.outcome === "needs_review" && <span className="badge o-needs_review" style={{ height: 18 }}>action needed</span>}
        </Link>
      </nav>

      {view === "review" && reviewReady ? (
        <Review run={detail} />
      ) : (
        <div className="live-grid">
          <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
            {reviewReady && (
              <Verdict run={detail} compact animate={sawLive.current === true} />
            )}
            {reviewReady && (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Link className={`btn ${detail.outcome === "needs_review" ? "btn-primary" : ""}`} to={`/runs/${id}/review`}>
                  {detail.outcome === "needs_review" ? "Review decision" : detail.outcome === "rejected" ? "See why" : "Open decision review"}
                </Link>
                {detail.outcome === "needs_review" && justFinished && <span className="faint" style={{ alignSelf: "center", fontSize: 12.5 }}>Opening the review…</span>}
              </div>
            )}
            {view === "review" && !reviewReady && <div className="banner o-processing">The decision review opens when the run finishes.</div>}
            <section className="panel">
              <div className="panel-head">
                <h2>Pipeline</h2>
                <span className="faint" style={{ fontSize: 12, marginLeft: "auto" }}>{events.length} events · policy {detail.policy_version}</span>
              </div>
              <Timeline events={events} running={running} />
            </section>
          </div>
          <section className="panel doc-pane" aria-label="Invoice document">
            <div className="panel-head">
              <h2>Document</h2>
              <a href={api.fileUrl(id)} target="_blank" rel="noopener" style={{ marginLeft: "auto", fontSize: 12.5 }}>Open in new tab</a>
            </div>
            {api.fileUrl(id)
              ? <iframe src={api.fileUrl(id)} title={`PDF ${detail.filename}`} />
              : <div className="empty">No document preview available for this run.</div>}
          </section>
        </div>
      )}
    </>
  );
}

function Elapsed({ start, end, total }: { start: string; end?: string; total: number | null }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (end) return;
    const t = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(t);
  }, [end]);
  const ms = end ? total ?? Date.parse(end) - Date.parse(start) : now - Date.parse(start);
  return <span className="conn num" title="Elapsed from receipt">{end ? "took" : "elapsed"} {duration(Math.max(0, ms))}</span>;
}

