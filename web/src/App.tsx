import { useQuery } from "@tanstack/react-query";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import { api, MOCK } from "./api";
import ConnectionsPage from "./pages/ConnectionsPage";
import Dashboard from "./pages/Dashboard";
import HowItWorks from "./pages/HowItWorks";
import Invoices from "./pages/Invoices";
import Inbox from "./pages/Inbox";
import Notifications from "./pages/Notifications";
import PoBook from "./pages/PoBook";
import RunPage from "./pages/RunPage";

function Count({ n, label }: { n?: number; label: string }) {
  if (!n) return null;
  return <span className="nav-count" aria-label={`${n} ${label}`}>{n}</span>;
}

export default function App() {
  const stats = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 5000 });
  const mail = useQuery({ queryKey: ["mail"], queryFn: api.mails, refetchInterval: 5000 });
  const held = mail.data?.mails.filter((m) => m.status === "held").length;

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand">
            <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden>
              <rect x="3" y="2" width="26" height="28" rx="5" fill="#000" />
              <rect x="8" y="9" width="16" height="4" rx="1.5" fill="#ffe14d" />
              <path d="M9 18h14M9 23h9" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" />
            </svg>
            Invoice Cockpit
          </Link>
          <nav className="topnav" aria-label="Main">
            <NavLink to="/" end>Today<Count n={stats.data?.open_reviews} label="invoices waiting for review" /></NavLink>
            <NavLink to="/invoices">Invoices</NavLink>
            <NavLink to="/inbox">Inbox<Count n={held} label="emails held" /></NavLink>
            <NavLink to="/po-book">PO book</NavLink>
            <NavLink to="/connections">Connections</NavLink>
            <NavLink to="/notifications">Notifications</NavLink>
            <NavLink to="/about">About</NavLink>
          </nav>
          <div className="topbar-right">
            {MOCK && <span className="mock-flag" title="VITE_MOCK=1: in-memory data, no backend">Mock data</span>}
          </div>
        </div>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/invoices" element={<Invoices />} />
          <Route path="/connections" element={<ConnectionsPage />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/po-book" element={<PoBook />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/about" element={<HowItWorks />} />
          <Route path="/how-it-works" element={<HowItWorks />} />
          <Route path="/runs/:id" element={<RunPage view="live" />} />
          <Route path="/runs/:id/review" element={<RunPage view="review" />} />
          <Route path="*" element={<div className="empty"><p>This page does not exist.</p><p><Link to="/">Go to today's work</Link></p></div>} />
        </Routes>
      </main>
    </>
  );
}
