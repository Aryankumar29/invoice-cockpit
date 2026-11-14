import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Outcome } from "../api";
import { DemoPanel, RunHistory, UploadPanel } from "./Dashboard";

export default function Invoices() {
  const [outcome, setOutcome] = useState<Outcome | "">("");
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 3000 });
  const { hash } = useLocation();
  useEffect(() => { if (hash === "#add") document.getElementById("add")?.scrollIntoView({ behavior: "smooth", block: "start" }); }, [hash]);
  return (
    <>
      <div className="dash-head">
        <div>
          <h1>Invoices</h1>
          <p>Add invoices here and see every invoice with its decision and the reason for it.</p>
        </div>
      </div>
      <div className="dash-grid">
        <RunHistory outcome={outcome} setOutcome={setOutcome} stats={stats.data} />
        <aside className="dash-side" id="add">
          <UploadPanel />
          <DemoPanel />
        </aside>
      </div>
    </>
  );
}
