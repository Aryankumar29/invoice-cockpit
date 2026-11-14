import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { ConnectionsBoard, StatusPill } from "../components/Connections";
import { Acc } from "../components/ui";
import { ApiUsagePanel } from "./Dashboard";

export default function ConnectionsPage() {
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 10000 });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 15000 });
  const u = stats.data?.api_usage;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Connections</h1>
          <p>Click a row to see what it does, what you need, and to connect it.</p>
        </div>
      </div>
      <ConnectionsBoard extra={
        <section className="acc-group">
          <h2>Reading invoices</h2>
          <div className="acc-list">
            <Acc title="Invoice reader" sub={health.data ? (health.data.extraction_mode === "local" ? "Local reader only" : "Local reader, with the AI model as backup") : "…"}
                 right={<StatusPill on={!!health.data?.ok} text={health.data?.ok ? "Working" : "Checking"} />}>
              <div className="acc-stack">
                <p>Text is read on this server first. The AI model is only used when a document cannot be read locally.</p>
              </div>
            </Acc>
            <Acc title="AI model usage" sub={u ? (u.calls ? `${u.calls} calls, ${u.total_tokens.toLocaleString()} tokens so far` : "Not used yet") : "…"}>
              <ApiUsagePanel usage={u} />
            </Acc>
          </div>
        </section>
      } />
    </>
  );
}
