# Invoice Cockpit on LangGraph — Rebuild Plan

Status: built 2026-11-17 (v2.0.0). v1 is tagged `v1-pipeline`. See "Changes from the plan" at the end.

## Goal

Rebuild the agent layer of Invoice Cockpit on LangGraph so it can be linked as "the best LangGraph agent I have built", **without weakening the thesis: the model reads, the rules decide, the human is accountable.**

Today a hand-written orchestrator (`cockpit/pipeline.py`) runs 9 nodes in a fixed order, a thread drains a SQLite queue, review actions are plain DB updates, and the investigation agent (`cockpit/investigate.py`) is a hand-rolled tool-calling loop on the OpenAI SDK. LangGraph replaces exactly those four pieces. The rule engine, matching, extraction code, store, API shape and React UI stay.

## What changes and what does not

| Keep as is | Replace with LangGraph |
|---|---|
| `cockpit/core/*` (R01–R16, matching, ledger, duplicates, policy) | `pipeline.py` orchestrator → `StateGraph` with typed state |
| `cockpit/extraction/*` (Claude/OpenAI/local/OCR/replay) | Extraction fallback chain → extraction **subgraph** with conditional edges |
| `cockpit/nodes/*` business logic (`run()` functions) | `Worker` queue + `requeue_interrupted` → **SQLite checkpointer** (crash resume) |
| `store.py` events table, SSE, React UI | Review buttons as DB writes → `interrupt()` + `Command(resume=...)` |
| All 447 tests | Investigation loop → **ReAct subgraph** with `ToolNode` and read-only tools |
| | Mail triage → small graph that fans out attachments with `Send` |

## Target architecture

```
                         ┌───────────────── invoice graph (StateGraph) ─────────────────┐
 mail graph              │                                                              │
 triage ─► hold?         │ intake ─► extraction ─► validate ─► vendor ─► po ─► match    │
   │         (Send per   │            (subgraph)                                 │       │
   └─────────attachment)─►                                                       ▼       │
                         │                      explanation ◄─ decision ◄─ policy R01–16 │
                         │                           │                                   │
                         │            auto_approved ─┼─► END                             │
                         │            rejected ──────┼─► END                             │
                         │            needs_review ──┴─► investigate (ReAct subgraph)    │
                         │                                   ▼                           │
                         │                          human_review: interrupt()  ◄─ API    │
                         │                                   ▼                           │
                         │                    second approver? (dual control) ─► END     │
                         │  any stage stop ─► failed ─► END                              │
                         └───────────────────────── SqliteSaver checkpoints ─────────────┘
```

### 1. Invoice graph (`cockpit/graph/invoice.py`)
- **State**: `TypedDict` with `run_id`, `pdf`, `invoice`, `ctx`, `vendor_match`, `po_match`, `decision`, `explanation`, `investigation`, `review_actions` (append reducer), `stop`.
- **Nodes**: thin adapters around the existing `cockpit/nodes/*.run()`, so logic is not duplicated.
- **Edges are code, not model choices.** Every routing function reads a field the rules set (`decision.outcome`, `stop`). The LLM can never pick the next step. This answers "why a graph if the steps are fixed?": durability, human-in-the-loop and replay, not autonomy.
- **Events**: each node writes its stage event through `get_stream_writer()` (custom stream mode); a listener persists it to the existing events table, so SSE and the Live Run screen work unchanged.

### 2. Extraction subgraph (`cockpit/graph/extraction.py`)
The current fallback chain becomes visible graph edges:
`cache ─► local_parser ─► llm_extract ─► grounding_check ─(fail, once)─► llm_repair ─► ocr ─► replay ─► unreadable`.
`RetryPolicy` on the LLM node (1 retry, 20 s timeout). The chosen route is recorded in state as `extraction_source`, as today.

### 3. Human review with `interrupt()`
- `needs_review` routes to `human_review`, which calls `interrupt({packet summary})`. The run pauses and is saved by the checkpointer.
- `POST /api/runs/{id}/review` resumes the thread with `Command(resume={"action", "reviewer", "note"})`.
- Approve above $50,000 or a dual-control rule → a second `interrupt()` that rejects the same reviewer twice.
- Reviewers can never move an outcome to a less safe state than the rules allow, same as today; the node enforces it.

### 4. Durability (`langgraph-checkpoint-sqlite`)
- `SqliteSaver` on its own `data/graph.db`; `thread_id = run_id`.
- On restart, unfinished threads resume from their last completed node (replaces `requeue_interrupted`).
- Runs stay serial (one worker), so ledger checks still see every earlier invoice (PLAN D7).
- Audit: checkpoint history gives a step-by-step state trail per invoice next to the packet hash.

### 5. Investigation agent as a LangGraph ReAct subgraph (`cockpit/graph/investigate.py`)
- Hand-built loop: `agent ─► tools (ToolNode) ─► agent … ─► submit_findings`, max 6 turns, token budget kept.
- Same four read-only tools (`get_purchase_order`, `get_vendor_record`, `vendor_invoice_history`, `search_invoices`) as `@tool` functions.
- Model via `langchain-openai` or `langchain-anthropic`, picked from the existing settings.
- Guardrail kept and made structural: the subgraph's output schema only has `investigation`; it has no write access to `decision`. Money figures not found in the case or tool results are still flagged.

### 6. Mail graph (`cockpit/graph/mail.py`)
`fetch ─► triage ─► (hold | Send("invoice_graph", attachment) per invoice attachment)`. Bank-change and look-alike senders are held before any invoice run starts.

### 7. Developer surface
- `langgraph.json` so the graphs open in LangGraph Studio.
- Mermaid diagrams generated from the compiled graphs (`graph.get_graph().draw_mermaid()`) and committed to `docs/graphs/`, embedded in the README.
- Optional LangSmith tracing via `LANGSMITH_TRACING=true` (off by default, no key needed).
- UI: a small graph strip on Live Run showing the current node and the paused `human_review` state. No other UI changes.

## Proof it still behaves the same

1. **Parity test**: run every fixture and sample through the old pipeline and the new graph; outcome, reason codes and **packet SHA-256 must be identical**. The old pipeline is kept in `cockpit/legacy/` only until parity passes, then deleted.
2. All 447 existing tests pass (API tests updated only where review moves to resume).
3. New graph tests: topology (no edge chosen by a model), interrupt/resume, dual control, crash mid-run then resume from checkpoint, extraction fallback routes, investigator cannot write `decision`, prompt-injection fixture still has no effect.
4. Run the app in Docker, process the demo set in the browser, approve one held invoice, screenshot.

## Build steps

| # | Step | Done when |
|---|---|---|
| 1 | Tag current `main` as `v1-pipeline`; branch `langgraph`; add `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-openai`, `langchain-anthropic` pinned | `pip install` clean on 3.11 and 3.12 |
| 2 | Invoice graph + node adapters + event streaming; parity test | parity 100%, 447 tests green |
| 3 | Extraction subgraph with fallback edges and retry policy | extraction tests green, route recorded |
| 4 | Checkpointer + crash resume; remove `Worker` queue code | kill-and-restart test resumes mid-run |
| 5 | `human_review` interrupt + dual control; API review endpoint resumes the graph | review tests green, UI approve works |
| 6 | Investigation ReAct subgraph | investigator tests green, guardrail test |
| 7 | Mail graph with `Send` fan-out | mail tests green |
| 8 | `langgraph.json`, Mermaid diagrams, README rewrite (architecture, why LangGraph now, D1 revisited), Live Run graph strip | README renders on GitHub |
| 9 | Docker build + browser run of the demo set, screenshots | checked, not assumed |
| 10 | PR `langgraph` → `main`, merge, release `v2.0.0` | PR green |
| 11 | Portfolio: update Invoice Cockpit card (LangGraph in stack, new steps line, repo link), `check:design`, deploy to Vercel | live site shows it |

## Decision record update (PLAN D1, revisited)

D1 rejected LangGraph because "multi-agent frameworks add moving parts with no graded benefit". v2 adopts it for three benefits the hand-written pipeline had to build itself or lacked: durable checkpoints with resume, first-class human-in-the-loop pauses, and a tool-calling subgraph for investigation. The reason D1 existed still holds and is kept: the topology is fixed in code, only extraction, explanation and investigation call a model, and no model output chooses a route or a payment outcome.

## Risks

| Risk | Mitigation |
|---|---|
| LangGraph serializes state; dataclasses with `bytes`/`date` | Keep PDF on disk (store path, not bytes); custom serde or plain dicts at the node boundary |
| SSE regressions | Events still go to the same table; existing SSE tests must pass unchanged |
| Two SQLite files | Separate `graph.db` avoids locking against `cockpit.db`; both WAL |
| Scope creep in UI | Only the graph strip; no screen redesign |

## Changes from the plan (as built)

- **Mail fan-out with `Send` dropped.** Parallel branches would submit an email's invoice attachments in a
  nondeterministic order, and the PO ledger depends on order. The mail graph submits them one after another.
- **Worker queue kept.** LangGraph has no job queue outside its hosted platform. The serial DB queue stays (it also
  guarantees ledger order); the checkpointer replaces only the restart-from-scratch recovery.
- **Events written by the nodes**, and also sent to `get_stream_writer()`, rather than persisted by a stream
  consumer, so the Live Run screen shows "started" the moment a stage begins.
- **Old pipeline not kept in `cockpit/legacy/`.** Parity was proven against an export of tag `v1-pipeline` and
  frozen as `tests/golden/v1_decisions.json`; the test runs whenever Tesseract is available (Docker, or locally).
- **LangGraph Studio config (`langgraph.json`) not added.** Diagrams are generated from the compiled graphs
  instead (`scripts/draw_graphs.py` → `docs/graphs/`). Studio needs a LangSmith login to view, so it was left out.
- **Investigation agent now runs on Claude or OpenAI** (v1 was OpenAI only), through LangChain chat models.
- **The Live Run graph strip** became one extra timeline row, "Human review", showing the pause and each action.
