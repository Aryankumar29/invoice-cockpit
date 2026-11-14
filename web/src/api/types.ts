// Types from docs/CONTRACT.md (HTTP API). Keep in sync with the contract.

export type Outcome = "processing" | "auto_approved" | "approved" | "needs_review" | "rejected" | "returned" | "failed";

export type RunStatus = "queued" | "running" | "done" | "failed";

export type RunSummary = {
  id: string;
  created_at: string;
  filename: string;
  status: RunStatus;
  outcome: Outcome;
  reasons: string[];
  vendor_name: string | null;
  invoice_number: string | null;
  total_cents: number | null;
  currency: string | null;
  po_number: string | null;
  extraction_source: string | null;
  duration_ms: number | null;
  issues?: Issue[];
};

/** One plain-language finding and where on the review page it lives (#anchor) or which run it points to. */
export type Issue = {
  rule: string | null; reason: string | null; severity: "reject" | "review" | "fail" | "ok";
  text: string; anchor: string; link: string; run: string | null;
};

export type Stage = "intake" | "extraction" | "validation" | "vendor" | "po" | "matching" | "policy" | "decision" | "explanation";

export type StageStatus = "started" | "ok" | "warn" | "stopped" | "failed";

export type StageEvent = {
  seq: number;
  run_id: string;
  stage: Stage;
  status: StageStatus;
  title: string;
  message: string;
  data: Record<string, unknown>;
  ts: string;
};

export type RuleStatus = "pass" | "fail" | "cannot_run" | "not_applicable" | "error";

export type RuleRow = {
  rule: string;
  title: string;
  status: RuleStatus;
  message: string;
  tier: number;
  outcome: string | null;
  reason: string | null;
  evidence: Record<string, unknown>;
};

export type ReviewAction = "approve" | "reject" | "return_to_vendor" | "escalate";

export type AuditAction = {
  action: string;
  reviewer: string;
  note: string;
  ts: string;
  outcome_before: string;
  outcome_after: string;
};

export type RunDetail = RunSummary & {
  stages: StageEvent[];
  verdict: string;
  recommended_action: string;
  explanation: string;
  rules: RuleRow[];
  invoice: Record<string, unknown>;
  line_matches: unknown[];
  allocations: unknown[];
  duplicates: unknown;
  vendor_match: unknown;
  po_match: unknown;
  grounding: unknown;
  warnings: string[];
  packet_hash: string;
  policy_version: string;
  actions: AuditAction[];
  allowed_actions: string[];
  investigation?: Investigation | null;
  mail?: MailBrief | null;
};

export type Health = {
  ok: boolean;
  extraction_mode: "claude" | "openai" | "local";
  api_key_present: boolean;
  llm_model?: string | null;
  policy_version: string;
  db: unknown;
  po_source?: string;
};

export type FixtureKind = "digital" | "scan";

/** A manifest entry as returned by GET /fixtures (no `extraction`). */
export type Fixture = {
  key: string;
  file: string;
  kind: FixtureKind;
  demo: boolean;
  order: number | null;
  title: string;
  description: string;
  expected: { outcome: Outcome; reason: string };
  prerequisites: string[];
};

export type RunCreated = { run_id: string; existing: boolean };

export type Investigation = {
  status: string; budget_reached?: boolean; turns: number; tokens: number; seconds: number; model: string; ts?: string;
  unverified_figures: string[];
  steps: { turn: number; tool: string; args: Record<string, unknown>; result_preview: string; ms: number }[];
  findings: {
    summary: string; findings: { finding: string; evidence: string }[]; recommended_action: string;
    confidence: string; draft_message: { to: string; subject: string; body: string } | null; open_questions: string[];
  };
};

export type Stats = {
  total: number;
  by_outcome: Partial<Record<Outcome, number>>;
  stp_rate: number;
  avg_seconds: number;
  open_reviews: number;
  value_cents_by_outcome: Partial<Record<Outcome, number>>;
  api_usage?: ApiUsage;
};

export type ApiUsage = {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  today_calls: number;
  today_tokens: number;
  invoices: number;
  avg_tokens_per_invoice: number | null;
  avg_calls_per_invoice: number | null;
  last_call_at: string | null;
  by_model: { model: string; calls: number; tokens: number }[];
  by_purpose: { purpose: string; calls: number; tokens: number }[];
};

export type ActionBody = { action: ReviewAction; reviewer: string; note: string };

// The contract leaves these as `unknown`. These are the shapes the core engine
// serializes (cockpit/core: LineMatch, VendorMatch, POMatch, DupResult, grounding).
// The UI reads them defensively through the helpers in lib/shapes.ts.

export type LineMatchShape = { inv_index: number; po_line_no: number | null; score: number; bundle: number[]; bundle_options: number[][] };
export type AllocationShape = { po_line_no: number; qty_milli: number; amount_cents: number };
export type VendorMatchShape = { status: string; vendor: string | null; score: number; method: string; candidates: [string, number][] };
export type POMatchShape = { status: string; po: string | null; score: number; method: string; candidates: [string, number][] };
export type DupHitShape = { run_id: string; invoice_number: string | null; score: number; signals: string[] };
export type DuplicatesShape = { kind: "exact" | "near" | "review" | "none"; ref_check_ran: boolean; hits: DupHitShape[] };
export type GroundingShape = { checked: number; grounded: number; ungrounded: string[]; arithmetic_ok: boolean };
export type InvoiceLineShape = { description: string; qty_milli: number; unit_price_cents: number; amount_cents: number; sku: string | null };
/** One row of R10 evidence `lines`. */
export type PriceLineShape = {
  invoice_line: number; po_line: number; invoice_unit: number; po_unit: number;
  expected: number; billed: number; variance: number; allowed: number; ok: boolean; po_lines: number[];
};

// ------------------------------------------------------------------ connectors (mail intake, PO book, Slack)

export type MailLabel = "invoice" | "not_invoice" | "suspicious" | "unsure";
export type MailStatus = "processed" | "held" | "archived" | "released" | "dismissed";
export type MailKey = { ok: boolean; value: string | null; note: string };
export type MailKeys = {
  po_number: MailKey; vendor: MailKey; buyer: MailKey; matched: number; confirmed: boolean; all_po_numbers: string[];
};
export type MailAttachment = {
  filename: string; content_type: string; size: number; sha256: string; kind: string; kind_label: string;
  run_id: string | null; existing: boolean;
};
export type Mail = {
  id: string; message_id: string; connector: string; received_at: string | null; created_at: string;
  sender: string; sender_name: string | null; subject: string; snippet: string | null;
  label: MailLabel; status: MailStatus; reasons: string[]; keys: MailKeys; signals: Record<string, unknown>;
  vendor_id: string | null; attachments: MailAttachment[]; run_ids: string[]; note: string | null;
};
export type MailBrief = Pick<Mail, "id" | "sender" | "sender_name" | "subject" | "received_at" | "label" | "keys" | "connector">;
export type Mailbox = { name: string; label: string; configured: boolean; detail: string };
export type MailList = { mails: Mail[]; stats: Partial<Record<MailLabel, number>>; mailboxes: Mailbox[] };
export type SyncResult = {
  connector: string; fetched: number; new: number; by_label: Partial<Record<MailLabel, number>>; runs_started: number; mails: Mail[];
};

export type PoSource = {
  kind: "seed" | "file" | "sheets"; location: string; loaded_at?: string; error?: string | null; issues: string[];
  counts?: { vendors: number; purchase_orders: number; po_lines: number };
};
export type ConnectionField = {
  key: string; label: string; secret: boolean; hint: string; set: boolean; value: string | null; from: string | null;
};
export type PoOptions = {
  in_use: "uploaded" | "sheets" | "sample" | "builtin";
  uploaded: { exists: boolean; filename: string | null; uploaded_at: string | null };
  sheets: { connected: boolean; url: string | null };
  sample: { exists: boolean };
};
export type ConnectResult = { ok: boolean; message: string; connections?: Connections; issues?: string[] };
export type Connections = {
  po_source: PoSource & { open_url?: string | null; options?: PoOptions }; mailboxes: Mailbox[]; send_via: string | null;
  fields: Record<"gmail" | "outlook" | "slack" | "sheets", ConnectionField[]>;
  slack: { configured: boolean; alerts: string[]; digest_time: string | null; digest_last: string | null };
};
export type PoLineRecord = { line_no: number; description: string; qty_milli: number; unit_price_cents: number; received_qty_milli: number; sku: string | null };
export type PoRecord = {
  po_number: string; vendor_id: string; vendor_name: string | null; buyer_company: string | null; currency: string;
  status: string; issued_on: string | null; tax_rate_bp: number; total_cents: number; lines: PoLineRecord[];
};
export type VendorRecord = {
  vendor_id: string; vendor_name: string; tax_id: string; status: string; bank_on_file: boolean; email?: string | null; domain?: string | null;
};
export type SheetTab = { name: string; columns: string[]; computed?: string[]; rows: { row: number; cells: string[]; problem: string | null }[] };
export type Sheet = { source: PoSource; tabs: SheetTab[] };
export type Records = { source: PoSource; vendors: VendorRecord[]; purchase_orders: PoRecord[] };

export type NotificationItem = {
  id: number; ts: string; channel: string; kind: string; run_id: string | null; title: string; text: string;
  payload: Record<string, unknown>; status: "sent" | "failed" | "preview"; error: string | null;
};
export type Digest = { title: string; text: string; invoices: number; emails: number; counts: Record<string, number> };
export type VendorDraft = {
  available: boolean; to?: string | null; to_source?: string | null; missing_address?: boolean; subject?: string; body?: string;
  mailto?: string; reasons?: string[]; internal_notes: string[]; why?: string;
  send_via?: string | null; sent?: NotificationItem[];
};
export type VendorEmailSent = { sent: boolean; to: string; via: string; run: RunDetail; draft: VendorDraft };
