import { useEffect, useState } from "react";
import type { AuditAction, RunDetail, Stage, StageEvent } from "../api/types";
import { duration } from "../lib/format";
import { Icon, KV } from "./ui";

export const STAGES: { key: Stage; label: string; active: string }[] = [
  { key: "intake", label: "Receive the file", active: "Checking the file" },
  { key: "extraction", label: "Read the document", active: "Reading the invoice" },
  { key: "validation", label: "Check fields and totals", active: "Checking required fields and arithmetic" },
  { key: "vendor", label: "Check the vendor", active: "Looking up the vendor master" },
  { key: "po", label: "Find the purchase order", active: "Matching to a purchase order" },
  { key: "matching", label: "Match lines and balance", active: "Comparing invoice lines with PO lines" },
  { key: "policy", label: "Apply policy rules", active: "Running all 16 rules" },
  { key: "decision", label: "Decide", active: "Applying precedence to the rule results" },
  { key: "explanation", label: "Explain the decision", active: "Writing the explanation and memo" },
];

export type StageState = { stage: Stage; started?: StageEvent; last?: StageEvent };

export function stageStates(events: StageEvent[]): Record<Stage, StageState> {
  const out = Object.fromEntries(STAGES.map((s) => [s.key, { stage: s.key }])) as Record<Stage, StageState>;
  for (const e of events) {
    const st = out[e.stage];
    if (!st) continue;
    if (e.status === "started" && !st.started) st.started = e;
    st.last = e;
  }
  return out;
}

function elapsed(st: StageState, now: number): number | null {
  const fin = st.last && st.last.status !== "started" ? st.last : null;
  if (fin) {
    const ms = fin.data?.ms;
    if (typeof ms === "number") return ms;
    if (st.started) return Date.parse(fin.ts) - Date.parse(st.started.ts);
    return null;
  }
  if (st.started) return now - Date.parse(st.started.ts);
  return null;
}

const ICON: Record<string, () => JSX.Element> = { ok: Icon.check, warn: Icon.bang, failed: Icon.cross, stopped: Icon.minus };

const ACTION_LABEL: Record<string, string> = {
  approve: "Approved", reject: "Rejected", return_to_vendor: "Returned to vendor", escalate: "Escalated",
};

/** The graph's human_review step: paused until a reviewer acts, then each action in order. */
function ReviewStep({ review, actions }: { review?: RunDetail["review_state"]; actions: AuditAction[] }) {
  const waiting = !!review?.waiting;
  if (!waiting && actions.length === 0) return null;
  const last = actions[actions.length - 1];
  const status = waiting ? "paused" : "ok";
  const msg = waiting
    ? review?.dual_control && (review.approvals_given ?? 0) > 0
      ? `Paused for a second approver (${review.approvals_given} of 2 given).`
      : "Paused. The run resumes when a reviewer acts."
    : `${ACTION_LABEL[last.action] ?? last.action} by ${last.reviewer}: ${last.note}`;
  return (
    <li className={`stage s-${status} ${waiting ? "" : "done"}`}>
      <span className="stage-icon">{waiting ? <span className="pause-mark" aria-hidden /> : <Icon.check />}</span>
      <div className="stage-row">
        <h3>Human review</h3>
        <span className="time">{actions.length > 0 ? `${actions.length} action${actions.length === 1 ? "" : "s"}` : ""}</span>
        <p className="msg">{msg}</p>
      </div>
    </li>
  );
}

export function Timeline({ events, running, review, actions = [] }: {
  events: StageEvent[]; running: boolean; review?: RunDetail["review_state"]; actions?: AuditAction[];
}) {
  const states = stageStates(events);
  const [now, setNow] = useState(Date.now());
  const [open, setOpen] = useState<Set<Stage>>(new Set());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [running]);

  const toggle = (k: Stage) => setOpen((s) => {
    const n = new Set(s);
    if (n.has(k)) n.delete(k); else n.add(k);
    return n;
  });

  return (
    <ol className="timeline" aria-label="Pipeline stages">
      {STAGES.map((s) => {
        const st = states[s.key];
        const status = st.last?.status ?? "idle";
        const fin = st.last && st.last.status !== "started" ? st.last : null;
        const detail = fin && Object.keys(fin.data ?? {}).filter((k) => k !== "ms").length > 0;
        const isOpen = (status === "failed") !== open.has(s.key); // failures start expanded
        const ms = elapsed(st, now);
        const I = ICON[status];
        const done = status === "ok" || status === "warn";
        return (
          <li key={s.key} className={`stage s-${status} ${detail ? "expandable" : ""} ${isOpen && detail ? "open" : ""} ${done ? "done" : ""}`} aria-current={status === "started" ? "step" : undefined}>
            <span className="stage-icon">{status === "started" ? <span className="spinner" /> : I ? <I /> : null}</span>
            <div
              className="stage-row" role={detail ? "button" : undefined} tabIndex={detail ? 0 : undefined}
              aria-expanded={detail ? isOpen : undefined}
              onClick={() => detail && toggle(s.key)}
              onKeyDown={(e) => { if (detail && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(s.key); } }}
            >
              <h3>{fin?.title || s.label}{detail && <Icon.caret />}</h3>
              <span className="time">{ms != null ? duration(ms) : ""}</span>
              <p className="msg">
                {status === "started" ? s.active + "…" : fin ? fin.message : <span className="faint">Waiting</span>}
              </p>
            </div>
            {isOpen && detail && fin && (
              <div className="stage-detail"><KV data={fin.data} omit={["ms"]} /></div>
            )}
          </li>
        );
      })}
      <ReviewStep review={review} actions={actions} />
    </ol>
  );
}
