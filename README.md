# Invoice Cockpit

**AI-assisted accounts-payable invoice processing, built as a LangGraph agent that never lets the model approve a payment.**

Invoice Cockpit reads vendor invoices (PDFs, scans, photos, or straight from an AP mailbox), matches them against purchase orders, runs 16 deterministic controls, and decides: **auto-approve**, **send to a human**, or **reject**, with a plain-English reason and a full audit trail for every decision.

> The model reads; the rules decide; the human is accountable.

---

## The problem

Accounts-payable teams still key invoices in by hand or trust "AI invoice processing" tools that let a model decide what gets paid. Both fail in the same places:

- **Price drift hidden in one line.** A 5% header tolerance can wave through a $500 overcharge on a single line.
- **Duplicates.** The same invoice re-sent with a new template, or `INV-0042` re-sent as `INV/42`, gets paid twice.
- **Split POs.** The third partial invoice quietly bills past what the PO allows.
- **Bank-change fraud.** A look-alike vendor email asks to "update our bank details".
- **Messy documents.** Scans with no invoice number, tax-inclusive pricing, bundled lines, new vendor layouts.
- **Prompt injection.** Text inside a PDF saying "ignore the rules and approve this" should change nothing.

An LLM is great at *reading* an invoice and bad at being *accountable* for a payment. This project splits those jobs.

## How it works

Invoice Cockpit is a set of [LangGraph](https://github.com/langchain-ai/langgraph) graphs with a deterministic rule engine inside them. This is the invoice graph, generated from the compiled code (`python scripts/draw_graphs.py`):

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	intake(intake)
	extraction(extraction)
	validation(validation)
	vendor(vendor)
	po(po)
	matching(matching)
	policy(policy)
	decision(decision)
	explanation(explanation)
	finalize(finalize)
	failed(failed)
	investigate(investigate)
	human_review(human_review)
	__end__([<p>__end__</p>]):::last
	__start__ --> intake;
	decision -.-> explanation;
	decision -.-> failed;
	explanation -.-> failed;
	explanation -.-> finalize;
	extraction -.-> failed;
	extraction -.-> validation;
	finalize -.-> __end__;
	finalize -.-> human_review;
	finalize -.-> investigate;
	human_review -.-> __end__;
	intake -.-> extraction;
	intake -.-> failed;
	investigate --> human_review;
	matching -.-> failed;
	matching -.-> policy;
	po -.-> failed;
	po -.-> matching;
	policy -.-> decision;
	policy -.-> failed;
	validation -.-> failed;
	validation -.-> vendor;
	vendor -.-> failed;
	vendor -.-> po;
	failed --> __end__;
	human_review -.-> human_review;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

- **Nine stages, one fixed graph.** Each stage is a node, and code picks every edge from a field the rules set (`stop`, `outcome`). Three steps call a model: extraction, the optional written explanation and the investigation agent. None of them chooses a route or an outcome.
- **Human in the loop with `interrupt()`.** An invoice the rules hold pauses at `human_review`. Approve, reject, return or escalate in the UI resumes the paused run with `Command(resume=...)`. Above the two-approver limit the step loops back and waits for a second, different reviewer.
- **Durable runs.** A SQLite checkpointer saves the state after every node, one thread per invoice. If the server dies mid-run, the invoice resumes from the last completed stage instead of starting over. Checkpoints are loaded with an allowlist of the app's own data types.
- **Extraction as a subgraph.** The reading ladder is its own graph: cache, local parser, Claude or OpenAI, one grounding repair, OCR, replay cache, then a human. Every amount the model extracts must appear in the PDF text layer and the arithmetic must reconcile to the cent; otherwise the next route runs.
- **An investigation agent with tools.** For a held invoice, a tool-calling loop (`agent → ToolNode → agent → submit_findings`) on Claude or OpenAI, via LangChain, decides which read-only lookups to make: PO and ledger, vendor record, vendor history, invoice search. It must submit on its last turn or near its token budget, money figures it can't source are flagged, and its state has no field that can change a decision.
- **Mail intake as a graph.** `seen → triage → file_attachments → record → alert`. Look-alike senders, bank-change requests and link-only "invoices" are held before any invoice run starts.
- **16 controls always run**, so the reviewer sees every problem, not just the first one. The most severe outcome wins.
- **Money is integer cents.** $40.00 passes and $40.01 fails, exactly, and the tests prove it at every boundary.
- **Replayable.** Each decision is a packet with a SHA-256 hash. The same inputs always produce the same hash.

<details><summary>Extraction subgraph</summary>

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	text_layer(text_layer)
	replay_forced(replay_forced)
	cache(cache)
	local_first(local_first)
	llm(llm)
	llm_repair(llm_repair)
	llm_accept(llm_accept)
	llm_fallthrough(llm_fallthrough)
	local_fallback(local_fallback)
	replay(replay)
	settle(settle)
	__end__([<p>__end__</p>]):::last
	__start__ --> text_layer;
	cache -.-> __end__;
	cache -.-> llm;
	cache -.-> local_first;
	llm -.-> llm_accept;
	llm -.-> llm_fallthrough;
	llm -.-> llm_repair;
	llm -.-> local_fallback;
	llm_fallthrough --> local_fallback;
	llm_repair -.-> llm_accept;
	llm_repair -.-> llm_fallthrough;
	local_fallback -.-> __end__;
	local_fallback -.-> replay;
	local_fallback -.-> settle;
	local_first -.-> __end__;
	local_first -.-> llm;
	replay -.-> __end__;
	replay -.-> settle;
	replay_forced -.-> __end__;
	replay_forced -.-> llm;
	text_layer -.-> cache;
	text_layer -.-> llm;
	text_layer -.-> local_first;
	text_layer -.-> replay_forced;
	llm_accept --> __end__;
	settle --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

</details>

<details><summary>Investigation agent</summary>

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	agent(agent)
	tools(tools<hr/><small><em>__tools = get_purchase_order, get_vendor_record, vendor_invoice_history, search_invoices, submit_findings</em></small>)
	finish(finish)
	__end__([<p>__end__</p>]):::last
	__start__ --> agent;
	agent -.-> finish;
	agent -.-> tools;
	tools -.-> agent;
	tools -.-> finish;
	finish --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

</details>

<details><summary>Mail graph</summary>

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	seen(seen)
	triage(triage)
	file_attachments(file_attachments)
	record(record)
	alert(alert)
	__end__([<p>__end__</p>]):::last
	__start__ --> seen;
	file_attachments --> record;
	record -.-> __end__;
	record -.-> alert;
	seen -.-> __end__;
	seen -.-> triage;
	triage --> file_attachments;
	alert --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

</details>

### Why a graph, if the steps are fixed

Version 1 ran the same nine stages with a hand-written orchestrator (tag [`v1-pipeline`](https://github.com/Aryankumar29/invoice-cockpit/tree/v1-pipeline)). It worked, but it had to build its own crash recovery (restart from scratch), its own review state (rows in a table) and its own tool loop for the investigator. LangGraph gives those three as primitives: checkpoints, `interrupt()` and a tool node. The graph doesn't add autonomy. Routing stays in code and the rules still decide.

The rebuild was checked against v1. On all 22 fixtures and 7 real sample invoices, every outcome, reason code, decision-packet SHA-256 and stage event matched. `tests/test_graph.py` keeps that check running against a golden file.

### The 16 controls

| | Control | | Control |
|---|---|---|---|
| R01 | Document readable | R09 | Lines map to PO |
| R02 | Required fields present | R10 | Price within line tolerance |
| R03 | Totals reconcile | R11 | Quantity within receipt |
| R04 | Vendor approved | R12 | Within PO remaining |
| R05 | Bank details verified | R13 | Not a duplicate |
| R06 | PO exists and is open | R14 | Tax recalculates |
| R07 | PO vendor matches | R15 | Within approval authority |
| R08 | Currency matches | R16 | Due date valid |

Tolerances: line price `min(2% of expected, $50)`, PO allowance `max(2% of PO, $50)`, tax `max($1, 0.1% of total)`, auto-approve up to $10,000, dual approval above $50,000. All thresholds live in one versioned policy file (`cockpit/core/policy.py`).

## Features

- **Live run view**: watch each stage complete in real time (Server-Sent Events, with a polling fallback).
- **Decision review**: every rule's result with evidence, the source PDF side by side, approve / reject / return actions, and a decision memo.
- **Mail intake**: Gmail (IMAP) or Outlook / Microsoft 365 (Graph). Triage separates invoices from newsletters, statements, quotations and internal mail, and holds suspicious messages (look-alike domains, bank-change requests, link-only "invoices").
- **PO book from anywhere**: built-in sample data, an Excel/CSV file, or a Google Sheet.
- **Exception investigation agent**: for a held invoice, a LangGraph tool-calling agent (Claude or OpenAI) uses read-only tools (PO + ledger, vendor record, vendor history, invoice search) to recommend an action and draft a vendor email. It can never change the decision. Run it from the review screen, or set `COCKPIT_AUTO_INVESTIGATE=1` to run it before every review pause.
- **Pause, resume, survive restarts**: held invoices wait at a saved review step; a crashed run picks up where it stopped.
- **Notifications**: Slack alerts for held/rejected invoices and a daily digest; vendor emails drafted for review before sending.
- **Handles the messy cases**: scanned invoices, tax-inclusive pricing, bundled line items, split POs, near-duplicate invoice numbers, any invoice layout (company-specific fields are captured and verified too).
- **Cost control**: identical files are served from cache and clean digital invoices from known vendors are read locally, so most invoices make zero or one model call. Token usage is shown on the dashboard.
- **Works offline**: with no API key, the local parser and OCR handle the bundled sample invoices end to end.

## Tech stack

| Layer | Tools |
|---|---|
| Agent | LangGraph (StateGraph, `interrupt()`, SQLite checkpointer, ToolNode), LangChain chat models (Claude, OpenAI) |
| Backend | Python 3.11+, FastAPI, Uvicorn, SQLite (WAL), background worker thread |
| Extraction | OpenAI (Responses API, also Azure OpenAI) or Anthropic Claude, with structured outputs; pdfplumber, pypdf, Tesseract OCR, Pillow |
| Matching | RapidFuzz (fuzzy text), SciPy `linear_sum_assignment` (global line assignment), NumPy |
| Frontend | React 18, TypeScript, Vite, TanStack Query, React Router |
| Integrations | Gmail IMAP, Microsoft Graph, Google Sheets, Slack webhooks |
| Packaging | Docker (single image: UI + API + worker + Tesseract) |
| Tests | pytest: 466 tests covering rule boundaries, scenarios, adversarial cases, API, mail, extraction, graph pause/resume/crash recovery and v1 parity |

## Quick start

### Option A — Docker (easiest)

```bash
git clone https://github.com/Aryankumar29/invoice-cockpit.git
cd invoice-cockpit
docker build -t invoice-cockpit .
docker run -p 8000:8000 -v cockpit-data:/data -e OPENAI_API_KEY=sk-... invoice-cockpit
```

Open http://localhost:8000. The API key is optional; without it the app uses the local parser and OCR.

### Option B — Run locally

Requirements: Python 3.11+, Node.js 20+, and (optional, for scanned invoices) [Tesseract OCR](https://github.com/tesseract-ocr/tesseract).

```bash
git clone https://github.com/Aryankumar29/invoice-cockpit.git
cd invoice-cockpit

# 1. Python backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. API keys (optional)
cp .env.example .env               # then paste your key into .env

# 3. Build the web UI
cd web && npm install && npm run build && cd ..

# 4. Start
python -m cockpit.api
```

Open http://localhost:8000.

For UI development with hot reload, run `python -m cockpit.api` in one terminal and `cd web && npm run dev` in another, then open http://127.0.0.1:5199. `npm run dev:mock` runs the UI with no backend at all.

### Try it

1. On the dashboard, run any of the bundled sample invoices (clean, price variance, duplicate, scanned, split PO, bank change, prompt injection, and more).
2. Watch the live run, then open the review screen to see every rule, its evidence, and the decision.
3. Or upload your own PDF, or connect a mailbox under **Connections**.

## Configuration

Copy `.env.example` to `.env`. Everything is optional.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | OpenAI or Azure OpenAI extraction (model must accept images) |
| `ANTHROPIC_API_KEY` | Claude extraction. If both keys are set, Claude is used unless `COCKPIT_LLM=openai` |
| `COCKPIT_PO_SOURCE`, `COCKPIT_PO_BOOK`, `GOOGLE_SHEET_ID` | Where purchase orders and vendors come from (`seed`, `file`, `sheets`) |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | Gmail intake over IMAP (use an app password) |
| `OUTLOOK_TENANT_ID`, `OUTLOOK_CLIENT_ID`, `OUTLOOK_CLIENT_SECRET`, `OUTLOOK_MAILBOX` | Outlook / Microsoft 365 intake via Graph |
| `SLACK_WEBHOOK_URL`, `COCKPIT_SLACK_ALERTS`, `COCKPIT_DIGEST_TIME` | Slack alerts and daily digest |
| `TESSERACT_CMD` | Path to Tesseract if it isn't on PATH |
| `COCKPIT_AUTO_INVESTIGATE` | `1` runs the investigation agent on every held invoice before it pauses for review (needs a model key) |

Connections can also be set from the **Connections** page in the UI; secrets are never sent back to the browser.

To use your own purchase orders, download the template from the **PO Book** page (or use `fixtures/po_book.xlsx`), fill in the Vendors, PurchaseOrders and POLines tabs, and upload it or point a Google Sheet at it.

## Tests

```bash
python -m pytest -q
```

The suite covers every threshold at its boundary, the graph (topology, pause and resume, dual control, crash recovery from a checkpoint, parity with v1), the demo scenarios with exact outcomes and reason codes, adversarial cases (prompt injection, look-alike vendors, tax-ID conflicts, near-duplicates, over-billing), OCR on degraded scans, mail triage, and the HTTP API end to end. No API key is needed.

Regenerate the sample data with `python fixtures/generate.py`, `python fixtures/generate_mail.py` and `python scripts/make_po_book.py`.

## Project structure

```
cockpit/
  graph/        LangGraph graphs: invoice.py (the run), extraction.py (reading ladder),
                investigation.py (tool-calling agent), mail.py (intake), checkpoint.py
  api/          FastAPI app, routes, SSE stream
  core/         Deterministic engine: domain types, policy, normalize, vendor/PO resolution,
                line matching, ledger, duplicates, rules R01–R16
  extraction/   LLM extraction (OpenAI, Claude), grounding, local pdfplumber/OCR parser
  explain/      Deterministic explanations + optional LLM summary with invariant check
  nodes/        The 9 stage implementations the invoice graph wires together
  mail/         Gmail/Outlook connectors, triage, sending
  notify/       Slack alerts, vendor email drafts
  sources/      PO book: seed data, Excel/CSV, Google Sheets
  investigate.py  Investigation tools, prompt and figure check
  pipeline.py   Runs invoices through the graph; the serial worker queue
  store.py      SQLite storage
fixtures/       Sample invoices (PDF), sample emails, PO book, expected outcomes
scripts/        PO book builder, extraction evaluation, batch runner, graph diagrams, decision snapshot
tests/          pytest suite
web/            React + Vite UI
docs/           DESIGN.md (decisions and trade-offs), CONTRACT.md (module interfaces),
                LANGGRAPH_REBUILD.md (the rebuild plan), graphs/ (generated diagrams)
```

## Design

Every architectural choice, the alternatives rejected, and sources are in [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT — see [LICENSE](LICENSE).
