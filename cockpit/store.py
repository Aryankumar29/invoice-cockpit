"""SQLite persistence (WAL). Runs, stage events and review actions; the live view is a projection of events."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    filename TEXT NOT NULL, file_hash TEXT NOT NULL UNIQUE, file_path TEXT NOT NULL,
    fixture_key TEXT, status TEXT NOT NULL, outcome TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
    vendor_name TEXT, invoice_number TEXT, total_cents INTEGER, currency TEXT, po_number TEXT,
    extraction_source TEXT, duration_ms INTEGER, detail TEXT, prior TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
    title TEXT NOT NULL, message TEXT NOT NULL, data TEXT NOT NULL, ts TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, action TEXT NOT NULL,
    reviewer TEXT NOT NULL, note TEXT NOT NULL, ts TEXT NOT NULL,
    outcome_before TEXT NOT NULL, outcome_after TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at);
CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL, status TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id TEXT, purpose TEXT NOT NULL,
    model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
    request_id TEXT
);
CREATE TABLE IF NOT EXISTS mails (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, connector TEXT NOT NULL, received_at TEXT,
    created_at TEXT NOT NULL, sender TEXT NOT NULL, sender_name TEXT, subject TEXT NOT NULL, snippet TEXT,
    label TEXT NOT NULL, status TEXT NOT NULL, reasons TEXT NOT NULL, keys TEXT NOT NULL, signals TEXT NOT NULL,
    vendor_id TEXT, attachments TEXT NOT NULL, run_ids TEXT NOT NULL DEFAULT '[]', note TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, channel TEXT NOT NULL, kind TEXT NOT NULL,
    run_id TEXT, title TEXT NOT NULL, text TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

SUMMARY_COLS = ("id", "created_at", "filename", "status", "outcome", "reasons", "vendor_name", "invoice_number",
                "total_cents", "currency", "po_number", "extraction_source", "duration_ms")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir)
        (self.dir / "files").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.dir / "cockpit.db", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args=()) -> None:
        with self._lock:
            self._db.execute(sql, args)

    # runs -----------------------------------------------------------------
    def find_by_hash(self, file_hash: str) -> str | None:
        rows = self._q("SELECT id FROM runs WHERE file_hash=?", (file_hash,))
        return rows[0]["id"] if rows else None

    def create_run(self, filename: str, file_hash: str, data: bytes, fixture_key: str | None = None) -> tuple[str, bool]:
        """Idempotent on file hash: the same bytes link to the existing run instead of starting a new one."""
        with self._lock:
            existing = self.find_by_hash(file_hash)
            if existing:
                return existing, True
            path = self.dir / "files" / f"{file_hash}.pdf"
            if not path.exists():
                path.write_bytes(data)
            rid = f"run-{uuid.uuid4().hex[:10]}"
            t = now()
            self._x("INSERT INTO runs (id, created_at, updated_at, filename, file_hash, file_path, fixture_key,"
                    " status, outcome) VALUES (?,?,?,?,?,?,?,'queued','processing')",
                    (rid, t, t, filename, file_hash, str(path), fixture_key))
            return rid, False

    def update_run(self, rid: str, **fields) -> None:
        for k in ("reasons", "detail", "prior"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(fields[k], default=str)
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), rid))

    def run(self, rid: str) -> dict | None:
        rows = self._q("SELECT * FROM runs WHERE id=?", (rid,))
        if not rows:
            return None
        r = dict(rows[0])
        for k, default in (("reasons", []), ("detail", {}), ("prior", None)):
            r[k] = json.loads(r[k]) if r[k] else default
        return r

    def summaries(self, outcome: str | None = None, q: str | None = None, limit: int = 50) -> list[dict]:
        sql = f"SELECT {', '.join(SUMMARY_COLS)} FROM runs WHERE 1=1"
        args: list = []
        if outcome:
            sql += " AND outcome=?"
            args.append(outcome)
        if q:
            sql += " AND (filename LIKE ? OR vendor_name LIKE ? OR invoice_number LIKE ? OR po_number LIKE ?)"
            args += [f"%{q}%"] * 4
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) | {"reasons": json.loads(r["reasons"])} for r in self._q(sql, args)]

    def next_queued(self) -> str | None:
        rows = self._q("SELECT id FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1")
        return rows[0]["id"] if rows else None

    def claim(self, rid: str) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE runs SET status='running', updated_at=? WHERE id=? AND status='queued'",
                                   (now(), rid))
            return cur.rowcount == 1

    def requeue_interrupted(self, resumable=None) -> int:
        """A run that was mid-flight when the process died is queued again. With a saved graph checkpoint
        (`resumable(run_id)`) it continues from its last completed stage and keeps its events; without one it
        restarts from scratch (stages are deterministic)."""
        with self._lock:
            ids = [r["id"] for r in self._db.execute("SELECT id FROM runs WHERE status='running'")]
            for rid in ids:
                if not (resumable and resumable(rid)):
                    self._db.execute("DELETE FROM events WHERE run_id=?", (rid,))
                self._db.execute("UPDATE runs SET status='queued' WHERE id=?", (rid,))
            return len(ids)

    def priors(self, exclude: str | None = None) -> list[dict]:
        """Processed invoices the ledger and duplicate checks must see, oldest first."""
        rows = self._q("SELECT id, prior FROM runs WHERE prior IS NOT NULL ORDER BY created_at")
        return [json.loads(r["prior"]) for r in rows if r["id"] != exclude]

    # events ---------------------------------------------------------------
    def add_event(self, rid: str, stage: str, status: str, title: str, message: str, data: dict | None = None) -> dict:
        with self._lock:
            seq = self._db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?", (rid,)).fetchone()[0]
            ev = dict(seq=seq, run_id=rid, stage=stage, status=status, title=title, message=message,
                      data=data or {}, ts=now())
            self._db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                             (rid, seq, stage, status, title, message, json.dumps(ev["data"], default=str), ev["ts"]))
            return ev

    def events(self, rid: str, after: int = 0) -> list[dict]:
        rows = self._q("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (rid, after))
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows]

    # actions --------------------------------------------------------------
    def add_action(self, rid: str, action: str, reviewer: str, note: str, before: str, after: str) -> None:
        self._x("INSERT INTO actions (run_id, action, reviewer, note, ts, outcome_before, outcome_after)"
                " VALUES (?,?,?,?,?,?,?)", (rid, action, reviewer, note, now(), before, after))

    def actions(self, rid: str) -> list[dict]:
        return [dict(r) for r in self._q("SELECT action, reviewer, note, ts, outcome_before, outcome_after"
                                         " FROM actions WHERE run_id=? ORDER BY id", (rid,))]

    # stats / reset --------------------------------------------------------
    def stats(self) -> dict:
        rows = self._q("SELECT outcome, COUNT(*) n, COALESCE(SUM(total_cents), 0) v, AVG(duration_ms) d"
                       " FROM runs WHERE status IN ('done', 'failed') GROUP BY outcome")
        by = {r["outcome"]: r["n"] for r in rows}
        total = sum(by.values())
        durations = [(r["d"], r["n"]) for r in rows if r["d"] is not None]
        avg = sum(d * n for d, n in durations) / sum(n for _, n in durations) / 1000 if durations else None
        return {"total": total, "by_outcome": by,
                "stp_rate": round(by.get("auto_approved", 0) / total, 4) if total else 0.0,
                "avg_seconds": round(avg, 2) if avg is not None else None,
                "open_reviews": by.get("needs_review", 0),
                "value_cents_by_outcome": {r["outcome"]: r["v"] for r in rows}}

    # API spend (survives demo reset: it is real money) ----------------------
    def add_usage(self, row: dict) -> None:
        self._x("INSERT INTO api_usage (ts, run_id, purpose, model, input_tokens, output_tokens, cache_write_tokens,"
                " cache_read_tokens, cost_usd, request_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now(), row.get("run_id"), row["purpose"], row["model"], row["input_tokens"], row["output_tokens"],
                 row["cache_write_tokens"], row["cache_read_tokens"], row["cost_usd"], row.get("request_id")))

    def usage_stats(self) -> dict:
        """Calls and tokens as the provider reported them. No prices: the deployment's rate is not known here."""
        tok = "input_tokens + output_tokens + cache_read_tokens + cache_write_tokens"
        tot = self._q(f"SELECT COUNT(*) calls, COALESCE(SUM(input_tokens), 0) inp, COALESCE(SUM(output_tokens), 0) out,"
                      f" COALESCE(SUM(cache_read_tokens + cache_write_tokens), 0) cached, COALESCE(SUM({tok}), 0) total,"
                      f" MAX(ts) last FROM api_usage")[0]
        today = self._q(f"SELECT COUNT(*) calls, COALESCE(SUM({tok}), 0) total FROM api_usage WHERE ts >= ?",
                        (now()[:10],))[0]
        per_run = self._q(f"SELECT COUNT(DISTINCT run_id) runs, COALESCE(SUM({tok}), 0) total, COUNT(*) calls"
                          f" FROM api_usage WHERE run_id IS NOT NULL")[0]
        by_model = self._q(f"SELECT model, COUNT(*) calls, SUM({tok}) tokens FROM api_usage GROUP BY model"
                           f" ORDER BY tokens DESC")
        by_purpose = self._q(f"SELECT purpose, COUNT(*) calls, SUM({tok}) tokens FROM api_usage GROUP BY purpose")
        runs = per_run["runs"]
        return {"calls": tot["calls"], "input_tokens": tot["inp"], "output_tokens": tot["out"],
                "cached_tokens": tot["cached"], "total_tokens": tot["total"],
                "today_calls": today["calls"], "today_tokens": today["total"],
                "invoices": runs, "avg_tokens_per_invoice": round(per_run["total"] / runs) if runs else None,
                "avg_calls_per_invoice": round(per_run["calls"] / runs, 2) if runs else None,
                "last_call_at": tot["last"],
                "by_model": [dict(r) for r in by_model], "by_purpose": [dict(r) for r in by_purpose]}

    # investigations (agent findings; advisory, never a decision) ------------
    def add_investigation(self, rid: str, result: dict) -> None:
        self._x("INSERT INTO investigations (run_id, ts, status, result) VALUES (?,?,?,?)",
                (rid, now(), result.get("status", ""), json.dumps(result, default=str)))

    def investigation(self, rid: str) -> dict | None:
        rows = self._q("SELECT ts, result FROM investigations WHERE run_id=? ORDER BY id DESC LIMIT 1", (rid,))
        return json.loads(rows[0]["result"]) | {"ts": rows[0]["ts"]} if rows else None

    # mail intake ------------------------------------------------------------
    _MAIL_JSON = ("reasons", "keys", "signals", "attachments", "run_ids")

    def add_mail(self, row: dict) -> str:
        row = dict(row, id=row.get("id") or f"mail-{uuid.uuid4().hex[:10]}", created_at=now())
        for k in self._MAIL_JSON:
            row[k] = json.dumps(row.get(k) if row.get(k) is not None else [], default=str)
        cols = ", ".join(row)
        self._x(f"INSERT INTO mails ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        return row["id"]

    def _mail_row(self, r) -> dict:
        d = dict(r)
        for k in self._MAIL_JSON:
            d[k] = json.loads(d[k]) if d[k] else []
        return d

    def mail(self, mid: str) -> dict | None:
        rows = self._q("SELECT * FROM mails WHERE id=? OR message_id=?", (mid, mid))
        return self._mail_row(rows[0]) if rows else None

    def mails(self, label: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM mails", []
        if label:
            sql, args = sql + " WHERE label=?", [label]
        return [self._mail_row(r) for r in self._q(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))]

    def update_mail(self, mid: str, **fields) -> None:
        for k in self._MAIL_JSON:
            if k in fields:
                fields[k] = json.dumps(fields[k], default=str)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE mails SET {cols} WHERE id=?", (*fields.values(), mid))

    def mail_for_run(self, rid: str) -> dict | None:
        rows = self._q("SELECT * FROM mails WHERE run_ids LIKE ? ORDER BY created_at DESC LIMIT 1", (f'%"{rid}"%',))
        return self._mail_row(rows[0]) if rows else None

    def mail_stats(self, since: str | None = None) -> dict:
        sql, args = "SELECT label, COUNT(*) n FROM mails", ()
        if since:
            sql, args = sql + " WHERE created_at >= ?", (since,)
        return {r["label"]: r["n"] for r in self._q(sql + " GROUP BY label", args)}

    # notifications ------------------------------------------------------------
    def add_notification(self, channel: str, kind: str, title: str, text: str, payload: dict, status: str,
                         run_id: str | None = None, error: str | None = None) -> dict:
        with self._lock:
            cur = self._db.execute("INSERT INTO notifications (ts, channel, kind, run_id, title, text, payload,"
                                   " status, error) VALUES (?,?,?,?,?,?,?,?,?)",
                                   (now(), channel, kind, run_id, title, text, json.dumps(payload, default=str),
                                    status, error))
            return self.notifications(limit=1, nid=cur.lastrowid)[0]

    def notifications(self, limit: int = 100, nid: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM notifications", ()
        if nid is not None:
            sql, args = sql + " WHERE id=?", (nid,)
        rows = self._q(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def kv_get(self, k: str) -> str | None:
        rows = self._q("SELECT v FROM kv WHERE k=?", (k,))
        return rows[0]["v"] if rows else None

    def kv_set(self, k: str, v: str) -> None:
        self._x("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def runs_since(self, since: str, until: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM runs WHERE created_at >= ?", [since]
        if until:
            sql, args = sql + " AND created_at < ?", args + [until]
        out = []
        for r in self._q(sql + " ORDER BY created_at", args):
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"])
            d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            out.append(d)
        return out

    def reset(self) -> None:
        with self._lock:
            self._db.executescript("DELETE FROM runs; DELETE FROM events; DELETE FROM actions; "
                                   "DELETE FROM investigations; DELETE FROM mails; DELETE FROM notifications;")
