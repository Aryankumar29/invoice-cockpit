import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type NotificationItem } from "../api";
import { when } from "../lib/format";
import { Acc } from "../components/ui";

const STATUS_CLASS: Record<NotificationItem["status"], string> = { sent: "o-auto_approved", failed: "o-rejected", preview: "o-failed" };
const STATUS_TEXT: Record<NotificationItem["status"], string> = { sent: "Sent to Slack", failed: "Not delivered", preview: "Preview only" };

/** Slack mrkdwn to plain text: *bold*, <url|label> links. */
const plain = (s: string) => s.replace(/<([^|>]+)\|([^>]+)>/g, "$2").replace(/\*([^*\n]+)\*/g, "$1");

export default function Notifications() {
  const qc = useQueryClient();
  const conn = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const list = useQuery({ queryKey: ["notifications"], queryFn: api.notifications, refetchInterval: 4000 });
  const preview = useQuery({ queryKey: ["digest"], queryFn: api.digestPreview, refetchInterval: 10000 });
  const send = useMutation({ mutationFn: api.sendDigest, onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }) });
  const slack = conn.data?.slack;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Notifications</h1>
          <p>Alerts when something needs a person, and a summary at the end of each day.</p>
        </div>
        <div className="page-actions">
          <button className="btn" disabled={send.isPending} onClick={() => send.mutate()}>
            {send.isPending ? <span className="spinner" /> : slack?.configured ? "Send today's summary to Slack" : "Make today's summary"}
          </button>
        </div>
      </div>
      {slack && !slack.configured && (
        <div className="banner o-needs_review" style={{ marginBottom: 16 }}>
          Slack is not connected, so messages are kept here. <Link to="/connections">Connect Slack</Link> to receive them there.
        </div>
      )}
      {send.isError && <p className="err">{(send.error as Error).message}</p>}
      <section className="acc-group">
        <h2>Today's summary <span className="faint">{slack?.digest_time ? `sent automatically at ${slack.digest_time}` : ""}</span></h2>
        <div className="acc-list">
          <Acc title={preview.data?.title ?? "Today's summary"} sub={preview.data ? `${preview.data.invoices} invoices, ${preview.data.emails} emails` : "…"}>
            {preview.data && <pre className="slack-text">{plain(preview.data.text)}</pre>}
          </Acc>
        </div>
      </section>
      <section className="acc-group">
        <h2>Messages {list.data ? <span className="faint">({list.data.items.length})</span> : null}</h2>
        {!list.data ? <div className="empty">Loading…</div> : list.data.items.length === 0 ? (
          <div className="empty"><p>No messages yet. They appear when an invoice needs review, is rejected or fails, when an email is held, and once a day.</p></div>
        ) : (
          <div className="acc-list">
            {list.data.items.map((n) => (
              <Acc key={n.id} title={plain(n.title)} sub={n.kind === "digest" ? `Daily summary, ${when(n.ts)}` : when(n.ts)}
                   right={<span className={`badge ${STATUS_CLASS[n.status]}`}>{STATUS_TEXT[n.status]}</span>}>
                <div className="acc-stack">
                  <pre className="slack-text">{plain(n.text)}</pre>
                  {n.error && <p className="err" style={{ margin: 0 }}>{n.error}</p>}
                  {n.run_id && <Link to={`/runs/${n.run_id}/review`}>Open the invoice</Link>}
                  {n.kind.startsWith("mail_") && <Link to="/inbox">Open the inbox</Link>}
                </div>
              </Acc>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
