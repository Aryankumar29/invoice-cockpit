# Invoice Cockpit — Design

**The model reads; the rules decide; the human is accountable.**

This document records every architectural decision with its reason, the alternatives rejected, and a source. Decision IDs (D1–D14) are referenced from code comments.

---

## 1. Architectural decisions

| # | Decision | Why this | Why not the alternatives | Source |
|---|---|---|---|---|
| D1 | Pipeline is a **code-orchestrated workflow of 9 stage agents**, not one autonomous LLM agent | Stages and decisions are known in advance; a fixed pipeline is deterministic, replayable, and boundary-testable | An autonomous tool-calling agent decides its own steps, so two runs on the same PDF can differ. That breaks replay and audit, and gives a weak answer to "what happens when it's wrong?". Multi-agent frameworks (LangGraph, CrewAI) add moving parts with no benefit here | [S14] |
| D2 | Only **2 of 9 agents call an LLM**: Extraction (required) and Explanation (optional, with invariant check). Every other agent is plain Python | Payment decisions must not depend on model output. Rules are versioned code that can be unit-tested at the $40.00 / $40.01 boundary | LLM-as-judge for approval can't be boundary-tested, and a model can be talked into an answer through prompt injection in the invoice text | [S8], [S9] |
| D3 | **LLM extraction with native PDF input + structured outputs** (Claude or OpenAI, chosen by which key is set) | One call handles both digital and scanned PDFs, because the model receives each page as text and as an image. Structured outputs guarantee schema-valid JSON | Managed IDP (Azure DI, Textract, Mindee): strong on scans, but adds a vendor account and a second schema mapping. Tesseract-only + regex: free, but brittle on line tables (kept as the offline fallback) | [S12], [S13] |
| D4 | **Grounding check after extraction**: every extracted amount must appear in the PDF text layer (digital pages). Arithmetic must reconcile within $0.02 (all pages) | A schema-valid answer can still be invented. Grounding catches hallucinated numbers on digital PDFs. On scans, arithmetic is the consistency check | Model confidence scores aren't calibrated for this. A second-model cross-check doubles latency and cost for a weaker signal | — |
| D5 | **Money as integer cents, quantity as integer thousandths** | Removes float rounding from tolerance checks. $40.00 vs $40.01 must be exact | `float`: 0.1+0.2 errors at boundaries. `Decimal`: correct but noisier for no benefit at this scale | — |
| D6 | **Line-level tolerance `min(2% of expected line amount, $50)`; PO-level allowance `max(2% of PO, $50)`; tax `max($1, 0.1% of total)`** | The two-bound formula stops large-dollar leakage and small-invoice noise. Checking lines first catches the $60 line error that a header check passes | Header-only tolerance: a 5% header tolerance can clear a $526 line problem. Percentage-only or absolute-only bounds each fail at one end of the size range | [S3], [S5], [S6] |
| D7 | **Cumulative commitment ledger for split POs** (remaining = PO − prior non-rejected billing) | Partial invoicing is normal. The last invoice must be tested against what has already been billed | Comparing each invoice to the full PO total either double-counts the commitment or flags the second invoice as over-billing | [S4] |
| D8 | **Global line assignment** (SciPy `linear_sum_assignment` with an explicit "unmatched" cost of 0.45) | A greedy best-pair match can steal a line that another invoice line fits better. A global solve is optimal, and "unmatched" is a legitimate answer | Greedy matching is order-dependent and wrong on near-identical descriptions. LLM line matching is non-deterministic | [S7] |
| D9 | **Fuzzy text similarity with RapidFuzz** (`token_set_ratio`, `token_sort_ratio`) | MIT license, C++ speed, deterministic, handles word reordering ("Widget A blue" vs "Blue widget A") | `difflib`: slower, worse on reordering. Embeddings add a model call or a local model | [S15] |
| D10 | **Two-tier duplicate detection**: exact keys reject; near-duplicate score ≥0.70 goes to review | A false clear pays twice (permanent loss). A false hold delays a real supplier (recoverable). So exact matches reject and fuzzy matches go to a human | File hash only misses an altered-template duplicate. Fuzzy-only auto-reject rejects legitimate recurring bills | — |
| D11 | **All 16 rules always evaluate; the most severe outcome wins; tiers 0–6 order the reason codes** | The reviewer sees the full picture, not just the first failure. Control severity, not dollar size, decides ($60 variance → review; $60 duplicate → reject) | First-failure short-circuit hides co-occurring problems. A weighted risk score is not explainable as "rule X failed by $Y" | — |
| D12 | **FastAPI + SQLite (WAL) + DB-backed worker thread + SSE with polling fallback, one Docker image** | One process, no Redis or Celery, survives restart, and the live view is a projection of persisted events | n8n/Make: hard to unit-test rule boundaries. Celery+Redis: a second service for no gain. Serverless: cold starts, timeouts, ephemeral disk. WebSockets: bidirectional push that isn't needed | [S10], [S11] |
| D13 | **React + Vite + TypeScript + TanStack Query** | Real routing and a live-stream UI over SSE | Streamlit: weak live-stream UX and no real routing. Low-code builders: harder to self-host | — |
| D14 | **Python 3.11-compatible code**; Docker image on **3.12** | The pinned numpy / scipy publish no 3.11 wheels, so the image uses 3.12 | Pinning older numpy/scipy diverges from the tested versions | — |
| D15 | **v2 (2026-11): the stages run as a LangGraph `StateGraph`**, with a SQLite checkpointer (one thread per invoice), `interrupt()` at a `human_review` node, the extraction ladder as a subgraph, and the investigation agent as a tool-calling graph. This revisits D1 and keeps its reason | D1's concern was autonomy, not graphs. Edges are still chosen by code from rule output, so runs stay deterministic and replayable. LangGraph replaces three things v1 built by hand: crash recovery (v1 restarted a run from scratch; v2 resumes from the last completed node), review state (v1 kept it in rows; v2 pauses the run and resumes it with the reviewer's action, dual control as a loop), and the investigator's tool loop | Keep the hand-written orchestrator: works, but every new pause or retry is more custom code. A prebuilt ReAct agent for the whole pipeline: hands routing to the model, which D1 and D2 rule out. Celery/Temporal for durability: a second service for one process | Parity: all 22 fixtures and 7 real samples give the same outcome, reasons, packet SHA-256 and stage events as tag `v1-pipeline` (`tests/test_graph.py`, `scripts/decision_snapshot.py`) |

### Edge-case resolutions

| Problem | Resolution | Reason |
|---|---|---|
| "Highest precedence tier decides" let a tier-1 near-duplicate *review* soften a tier-2 *reject* | **The most severe outcome across all failures wins**; tiers only order the reason codes | Fail closed: a review must never downgrade a hard control |
| Near-duplicate weights flagged a clean $4,800 invoice against an earlier, unrelated $4,800 invoice from the same vendor | **Counter-signal**: a different PO *and* a different invoice number skip the near-dup check; same-number resubmissions are still caught by the exact check | Same vendor + same amount is common (standard orders). The PO is the obligation identity |
| Pending reviews and the ledger | `needs_review` invoices **reserve** PO balance; rejected/returned/failed release it | Two reviewers can't approve against the same remaining balance |
| A document a human can read but the model couldn't | `needs_review` (`failed` is reserved for system errors) | A readable-by-human document is a review, not a crash |
| Vendor scoring | Tax ID first, then name | The tax-ID-first ladder is the control that matters |

---

## 2. Pipeline agents — method, fallback, tests

Every agent returns `ok` with evidence, or stops with a reason code. None of them silently "fixes" data.

| # | Agent | Method | Fallback chain (in order) |
|---|---|---|---|
| A1 | **Intake** | MIME + magic bytes, ≤10 MB, ≤10 pages, SHA-256, idempotency key. Images are converted to PDF | Invalid/encrypted/zero-page → `failed` + plain message; re-upload of the same hash → link to the existing run |
| A2 | **Extraction** (LLM) | PDF document input, strict JSON schema, prompt marks the document as untrusted data | 1) refusal → server-side fallback 2) API error/timeout → retry once 3) grounding fails → one repair call 4) **local extractor**: pdfplumber (digital), Tesseract OCR (scanned) 5) **replay cache** keyed by file hash 6) all fail → `needs_review DOCUMENT_UNREADABLE` |
| A3 | **Normalize + Validate** | ISO dates, ISO-4217, cents; dual invoice-number keys; R02, R03 hard checks | Hard fail → re-extract once with table-aware parser → `needs_review` (never auto-correct) |
| A4 | **Vendor resolution** | Tax ID exact → name fuzzy (≥93, margin ≥5) | Ambiguous (85–92) → review; unknown → `UNKNOWN_VENDOR` review; tax-ID conflict → hold |
| A5 | **PO resolution** | Exact ref → OCR-tolerant ref (O↔0, I/l↔1) → vendor open-PO scoring | Score 0.70–0.89 or near-tie → `PO_AMBIGUOUS` review; <0.70 → `PO_MISSING` review (never force a match) |
| A6 | **Line matching + ledger** | Global assignment with unmatched cost 0.45; cumulative ledger; bundled-line detection | Unmatched line → `LINE_UNVERIFIED` review; an allocation write fails → the run fails (no partial allocation) |
| A7 | **Policy (R01–R16)** | Pure functions, all evaluated, policy version stamped | A rule raises → that rule = `error` → `needs_review RULE_ERROR` (fail closed) |
| A8 | **Decision** | Severity precedence; decision packet + SHA-256 hash | Packet write fails → `failed`, retry from stage |
| A9 | **Explanation + Memo** (LLM optional) | Deterministic template first; optional LLM summary only if invariants pass (same outcome, only packet numbers, all uncertainty tags cited) | Invariant fails or API down → deterministic template |

Beyond the pipeline:

- **Exception investigation agent** (`cockpit/investigate.py`): on demand for held invoices. The model chooses among 4 read-only tools (PO + ledger, vendor record, vendor history, invoice search) and submits findings, a recommended action and a draft vendor message. Bounded (6 turns, 40K-token budget), every tool call recorded, money figures checked against the case, decision never changed.
- **Mail intake** (`cockpit/mail/`): Gmail (IMAP) or Outlook (Microsoft Graph). Triage separates invoices from newsletters, statements, quotations and internal mail, and holds suspicious messages (look-alike sender domains, bank-change requests, link-only "invoices").
- **Tax-inclusive invoices**: detected from wording ("prices include tax", "incl. VAT") or inferred when the total only reconciles that way. Matching rules compare a net-of-tax view.
- **API savers**: cache-first for identical bytes and local-first for complete, grounded, reconciling digital readings.

---

## 3. Test matrix

A wrong auto-approval is a critical bug; an over-cautious review is minor.

**Boundaries** (rule level): R03 $0.02 pass / $0.03 fail · R10 $40.00 pass / $40.01 fail · R12 $120.00 pass / $120.01 fail · R14 $1.00 pass / $1.01 fail · R15 $10,000.00 auto / $10,000.01 review; >$50,000 requires dual approval · R16 120 days pass / 121 fail · R13 near-dup score at 0.70.

**Scenarios** (exact outcome + reason):

| ID | Case | Expected |
|---|---|---|
| H1 | Northwind `INV-H-001` $4,800 / PO-1042 | `auto_approved` · `STP_CLEAN` |
| S1 | Globex `INV-S-001` $6,000 / PO-1047 | `auto_approved`, remaining $4,000 |
| S2 | Globex `INV-S-002` $4,000 / PO-1047 (after S1) | `auto_approved`, remaining $0 |
| D1 | Northwind `INV-D-001` $6,060 / PO-1051 | `needs_review` · `PRICE_VARIANCE` (line 2 +$60 vs $40 allowed) |
| E1 | Altered-template copy of `NW-2026-001` $4,800 | `rejected` · `DUPLICATE_INVOICE`, links original |
| E2 | Scanned Acme invoice, no invoice number, $3,000 / PO-1046 | `needs_review` · `MISSING_INVOICE_NUMBER` |

**Adversarial regression**: third Globex invoice → `rejected OVER_PO_REMAINING`; bank change on a clean invoice → `needs_review BANK_CHANGE`; currency mismatch → `rejected`; closed PO → `rejected`; PO of a different vendor → `rejected VENDOR_PO_MISMATCH`; unknown vendor → `needs_review`; tax-ID conflict → `needs_review TAX_ID_CONFLICT`; `INV/42` after `INV-0042` → `needs_review` near-dup; invoice >$10k clean → `needs_review APPROVAL_REQUIRED`; extra unmatched line → `needs_review LINE_UNVERIFIED`; miscomputed tax → `needs_review TAX_VARIANCE`; prompt injection in the PDF → same decision; replay → identical packet hash.

---

## Sources

- [S3] Oracle Financials, Invoice tolerances — https://docs.oracle.com/en/cloud/saas/financials/26c/fappp/invoice-tolerances.html
- [S4] Microsoft Dynamics 365, AP invoice matching — https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/accounts-payable-invoice-matching
- [S5] AppZen, Invoice matching tolerances — https://www.appzen.com/learn/invoice-matching-what-your-tolerances-cost-you
- [S6] AutoPayables, Three-way matching tolerance — https://autopayables.com/blog/three-way-matching-tolerance
- [S7] SciPy `linear_sum_assignment` — https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html
- [S8] Deterministic reasoning + LLM explanations — https://hackernoon.com/designing-trust-aware-hybrid-ai-systems-with-deterministic-reasoning-and-llm-explanations
- [S9] ACFE Report to the Nations 2026 — https://www.acfe.com/acfe-insights-blog/blog-detail?s=key-findings-report-to-the-nations-2026
- [S10] SQLite, Appropriate uses — https://sqlite.org/whentouse.html
- [S11] FastAPI SSE — https://fastapi.tiangolo.com/tutorial/server-sent-events/
- [S12] Anthropic, PDF support — https://platform.claude.com/docs/en/build-with-claude/pdf-support
- [S13] Anthropic, Structured outputs — https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- [S14] Anthropic, Building effective agents — https://www.anthropic.com/engineering/building-effective-agents
- [S15] RapidFuzz (MIT) — https://github.com/rapidfuzz/RapidFuzz
- [S16] pdfplumber (MIT) — https://github.com/jsvine/pdfplumber
- [S17] Tesseract OCR (Apache-2.0) — https://github.com/tesseract-ocr/tesseract
