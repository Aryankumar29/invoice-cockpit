import type {
  ActionBody, Connections, Digest, Fixture, Health, Mail, MailList, NotificationItem, Records, RunCreated, RunDetail,
  RunSummary, Sheet, StageEvent, Stats, SyncResult, VendorDraft, ConnectResult, VendorEmailSent,
} from "./types";

export interface Api {
  health(): Promise<Health>;
  stats(): Promise<Stats>;
  fixtures(): Promise<Fixture[]>;
  runFixture(key: string): Promise<RunCreated>;
  upload(file: File): Promise<RunCreated>;
  runs(params: { outcome?: string; q?: string; limit?: number }): Promise<RunSummary[]>;
  run(id: string): Promise<RunDetail>;
  events(id: string, after: number): Promise<StageEvent[]>;
  act(id: string, body: ActionBody): Promise<RunDetail>;
  investigate(id: string): Promise<RunDetail>;
  reset(): Promise<{ ok: boolean }>;
  connections(): Promise<Connections>;
  records(): Promise<Records>;
  reloadRecords(): Promise<Records>;
  sheet(): Promise<Sheet>;
  policy(): Promise<{ version: string; values: Record<string, number> }>;
  mails(): Promise<MailList>;
  syncMail(connector: string): Promise<SyncResult>;
  uploadMail(file: File): Promise<Mail & { new: boolean }>;
  releaseMail(id: string, reviewer: string, note: string): Promise<Mail>;
  dismissMail(id: string, reviewer: string, note: string): Promise<Mail>;
  notifications(): Promise<{ slack_configured: boolean; items: NotificationItem[] }>;
  digestPreview(): Promise<Digest>;
  sendDigest(): Promise<NotificationItem>;
  vendorDraft(id: string): Promise<VendorDraft>;
  sendVendorEmail(id: string, body: { reviewer: string; subject: string; body: string }): Promise<VendorEmailSent>;
  connect(service: string, values: Record<string, string>): Promise<ConnectResult>;
  testConnection(service: string): Promise<ConnectResult>;
  disconnect(service: string): Promise<ConnectResult>;
  bookUrl(): string;
  uploadBook(file: File): Promise<ConnectResult>;
  setSource(kind: "uploaded" | "sheets" | "sample"): Promise<ConnectResult>;
  templateUrl(sample?: boolean): string;
  fileUrl(id: string): string;
  memoUrl(id: string): string;
  dataUrl(id: string): string;
  /** Live stream. Returns a close function. `onError` fires once if the transport fails. */
  stream(id: string, after: number, h: StreamHandlers): () => void;
}

export type StreamHandlers = {
  onEvent: (e: StageEvent) => void;
  onDone: () => void;
  onError: () => void;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const BASE = "/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") msg = body.detail;
      else if (Array.isArray(body?.detail)) msg = body.detail.map((d: { msg?: string }) => d.msg).join("; ");
    } catch { /* not JSON */ }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const httpApi: Api = {
  health: () => req("/health"),
  stats: () => req("/stats"),
  fixtures: () => req("/fixtures"),
  runFixture: (key) => req(`/fixtures/${encodeURIComponent(key)}/run`, { method: "POST" }),
  upload: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return req("/runs", { method: "POST", body: fd });
  },
  runs: ({ outcome, q, limit = 50 }) => {
    const p = new URLSearchParams();
    if (outcome) p.set("outcome", outcome);
    if (q) p.set("q", q);
    p.set("limit", String(limit));
    return req(`/runs?${p}`);
  },
  run: (id) => req(`/runs/${id}`),
  events: (id, after) => req(`/runs/${id}/events.json?after=${after}`),
  act: (id, body) => req(`/runs/${id}/actions`, json(body)),
  investigate: (id) => req(`/runs/${id}/investigate`, { method: "POST" }),
  reset: () => req("/reset", { method: "POST" }),
  connections: () => req("/connections"),
  records: () => req("/records"),
  reloadRecords: () => req("/records/reload", { method: "POST" }),
  sheet: () => req("/records/sheet"),
  policy: () => req("/policy"),
  mails: () => req("/mail"),
  syncMail: (connector) => req("/mail/sync", json({ connector })),
  uploadMail: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return req("/mail/upload", { method: "POST", body: fd });
  },
  releaseMail: (id, reviewer, note) => req(`/mail/${id}/release`, json({ reviewer, note })),
  dismissMail: (id, reviewer, note) => req(`/mail/${id}/dismiss`, json({ reviewer, note })),
  notifications: () => req("/notifications"),
  digestPreview: () => req("/notify/digest/preview"),
  sendDigest: () => req("/notify/digest", { method: "POST" }),
  vendorDraft: (id) => req(`/runs/${id}/vendor-draft`),
  sendVendorEmail: (id, body) => req(`/runs/${id}/vendor-email`, json(body)),
  connect: (service, values) => req(`/connections/${service}`, json({ values })),
  testConnection: (service) => req(`/connections/${service}/test`, { method: "POST" }),
  disconnect: (service) => req(`/connections/${service}`, { method: "DELETE" }),
  bookUrl: () => `${BASE}/records/book`,
  uploadBook: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return req("/records/upload", { method: "POST", body: fd });
  },
  setSource: (kind) => req("/records/source", json({ kind })),
  templateUrl: (sample = false) => `${BASE}/records/template${sample ? "?sample=true" : ""}`,
  fileUrl: (id) => `${BASE}/runs/${id}/file`,
  memoUrl: (id) => `${BASE}/runs/${id}/memo`,
  dataUrl: (id) => `${BASE}/runs/${id}/extraction.json`,
  stream: (id, after, h) => {
    // EventSource cannot set Last-Event-ID on the first connect, so the resume point
    // also goes in the query. The browser sends the header itself on auto-reconnect.
    const es = new EventSource(`${BASE}/runs/${id}/events?after=${after}`);
    let closed = false;
    const close = () => { closed = true; es.close(); };
    const onStage = (m: MessageEvent) => {
      try { h.onEvent(JSON.parse(m.data) as StageEvent); } catch { /* ignore malformed frame */ }
    };
    es.onmessage = onStage;
    es.addEventListener("stage", onStage as EventListener);
    es.addEventListener("done", () => { close(); h.onDone(); });
    es.onerror = () => { if (!closed) { close(); h.onError(); } };
    return close;
  },
};
