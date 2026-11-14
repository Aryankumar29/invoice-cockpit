# Interfaces between modules

The fixed interfaces between extraction, fixtures, the HTTP API and the web UI. Change them together.

## Extraction (extraction -> pipeline)

```python
from cockpit.extraction import extract, ExtractionResult

res: ExtractionResult = extract(pdf_bytes: bytes, file_hash: str, filename: str = "")
res.invoice        # cockpit.core.domain.Invoice; readable=False when nothing usable came out
res.source         # "claude" | "local_text" | "local_ocr" | "replay" | "none"
res.grounding      # {"checked": int, "grounded": int, "ungrounded": [field paths], "arithmetic_ok": bool}
res.warnings       # list[str], plain English, shown in the Live Run view
res.attempts       # list[{"step": str, "ok": bool, "detail": str, "ms": int}]  fallback chain trace
res.raw            # dict, the model/parser output before normalization (stored, never used for decisions)
```

- No `ANTHROPIC_API_KEY` -> skip the Claude step silently (warning "No API key: local extractor used"), never raise.
- `Invoice.bank_hash` = `cockpit.core.normalize.bank_fingerprint(<account number on the PDF>)`.
- `Invoice.file_hash` = the `file_hash` argument (real SHA-256 of the bytes).
- Money in integer cents, qty in thousandths, dates `datetime.date`, currency ISO-4217.
- Replay cache dir: env `COCKPIT_REPLAY_DIR` (default `data/replay/`), keyed by file hash.

```python
from cockpit.explain import explain
text: str = explain(decision_packet: dict)   # deterministic template; optional Claude rewrite only if invariants hold
memo_html: str = memo(decision_packet: dict, run: dict)
```

## Fixtures

`fixtures/manifest.json`:
```json
[{"key": "H1", "file": "pdf/H1_northwind_INV-H-001.pdf", "kind": "digital|scan",
  "demo": true, "order": 1, "title": "Happy path", "description": "...",
  "expected": {"outcome": "auto_approved", "reason": "STP_CLEAN"},
  "extraction": { ...Invoice fields as JSON, cents/milli ints, ISO dates... },
  "prerequisites": ["S1"]}]
```
`extraction` must equal `cockpit.core.seed.FIXTURES[key]` for demo keys (except `file_hash`, which is the real PDF hash, and `bank_hash`).

## HTTP API (server -> web UI)

Base `/api`. JSON. Money fields are integer cents; UI formats.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/health` | | `{ok, extraction_mode: "claude"\|"local", api_key_present, policy_version, db}` |
| GET | `/fixtures` | | manifest entries (no `extraction`) |
| POST | `/fixtures/{key}/run` | | `{run_id, existing}` |
| POST | `/runs` | multipart `file` | `{run_id, existing}` (`existing=true` when same SHA-256 already ran) |
| GET | `/runs` | `?outcome=&q=&limit=50` | `[RunSummary]` newest first |
| GET | `/runs/{id}` | | `RunDetail` |
| GET | `/runs/{id}/events` | SSE, header `Last-Event-ID` | stream of `StageEvent`, ends with event `done` |
| GET | `/runs/{id}/events.json` | `?after=<seq>` | `[StageEvent]` (polling fallback) |
| POST | `/runs/{id}/actions` | `{action: "approve"\|"reject"\|"return_to_vendor"\|"escalate", reviewer, note}` | `RunDetail` |
| GET | `/runs/{id}/file` | | the PDF |
| GET | `/runs/{id}/memo` | | `text/html` decision memo |
| GET | `/stats` | | `{total, by_outcome: {..}, stp_rate, avg_seconds, open_reviews, value_cents_by_outcome}` |
| POST | `/reset` | | `{ok}` (demo reset: wipes runs, reseeds) |

```ts
type Outcome = "processing" | "auto_approved" | "approved" | "needs_review" | "rejected" | "returned" | "failed";
type RunSummary = { id: string; created_at: string; filename: string; status: "queued"|"running"|"done"|"failed";
  outcome: Outcome; reasons: string[]; vendor_name: string|null; invoice_number: string|null;
  total_cents: number|null; currency: string|null; po_number: string|null; extraction_source: string|null;
  duration_ms: number|null };
type StageEvent = { seq: number; run_id: string; stage: Stage; status: "started"|"ok"|"warn"|"stopped"|"failed";
  title: string; message: string; data: Record<string, unknown>; ts: string };
type Stage = "intake"|"extraction"|"validation"|"vendor"|"po"|"matching"|"policy"|"decision"|"explanation";
type RuleRow = { rule: string; title: string; status: "pass"|"fail"|"cannot_run"|"not_applicable"|"error";
  message: string; tier: number; outcome: string|null; reason: string|null; evidence: Record<string, unknown> };
type RunDetail = RunSummary & {
  stages: StageEvent[]; verdict: string; recommended_action: string; explanation: string;
  rules: RuleRow[]; invoice: Record<string, unknown>; line_matches: unknown[]; allocations: unknown[];
  duplicates: unknown; vendor_match: unknown; po_match: unknown; grounding: unknown; warnings: string[];
  packet_hash: string; policy_version: string;
  actions: { action: string; reviewer: string; note: string; ts: string; outcome_before: string; outcome_after: string }[];
  allowed_actions: string[] };
```

Review actions: allowed only when outcome is `needs_review` (approve/reject/return_to_vendor/escalate). `approve` on a run with `dual_control` evidence or total > $50,000 requires a second distinct reviewer (first approve returns `allowed_actions: ["approve"]` with a "1 of 2 approvals" note). Rejected/auto_approved runs are final.
