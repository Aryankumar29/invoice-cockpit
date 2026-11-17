"""Exception investigation agent as a LangGraph tool-calling loop. The one place a model chooses its own steps.

    agent --lookups--> tools (ToolNode) --> agent ... --submit_findings--> finish
          `--no tool call / turn limit--> finish

The model (Claude or OpenAI, via LangChain) sees the held invoice's case and calls read-only lookups over the same
data the rules used; it ends by calling submit_findings. Guardrails (cockpit/investigate.py has the tools and the
prompt): the lookups only read; the state has no field that can change a decision; on the last turn, or near the
token budget, the model is forced to submit; every lookup is recorded; money figures in the findings that appear
in neither the case nor a lookup result are flagged for the reviewer."""
from __future__ import annotations

import json
import time
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from .. import investigate as domain
from .. import usage
from ..core.domain import Context

SUBMIT = "submit_findings"


class NoModel(RuntimeError):
    pass


# --- tools ------------------------------------------------------------------------------------------------------

class Finding(BaseModel):
    finding: str
    evidence: str = Field(description="Which tool result or case fact shows it.")


class Draft(BaseModel):
    to: str
    subject: str
    body: str


class Findings(BaseModel):
    summary: str = Field(description="Two or three sentences for the AP reviewer.")
    findings: list[Finding]
    recommended_action: Literal[domain.ACTIONS]  # type: ignore[valid-type]
    confidence: Literal["high", "medium", "low"]
    draft_message: Draft | None
    open_questions: list[str]


class _PO(BaseModel):
    po_number: str


class _Vendor(BaseModel):
    name_or_tax_id: str


class _History(BaseModel):
    vendor_id: str
    limit: int | None = None


class _Search(BaseModel):
    invoice_number: str | None = None
    amount: str | None = None
    vendor_id: str | None = None


LOOKUPS = (
    ("get_purchase_order", _PO, "Purchase order header, its lines (ordered, received, already billed, remaining) and "
     "the earlier invoices billed against it."),
    ("get_vendor_record", _Vendor, "Vendor master record by name or tax ID: status, tax ID, whether bank details are "
     "on file, and the vendor's purchase orders. Returns closest matches when there is no exact one."),
    ("vendor_invoice_history", _History, "Earlier invoices from a vendor (newest first): number, date, total, "
     "outcome, PO, and per-line quantity, amount and unit price. Use it to see whether a price or pattern is new."),
    ("search_invoices", _Search, "Search all processed invoices by invoice number (punctuation-insensitive, "
     "partial), and/or total amount (within 1%), optionally for one vendor."),
)


def lookups(tools: domain.Tools) -> list[StructuredTool]:
    """The read-only lookups as LangChain tools. Each returns its JSON result, with its timing as the artifact."""
    def make(name, schema, description):
        def run(**kw):
            t0 = time.perf_counter()
            out = tools.dispatch(name, kw)
            return json.dumps(out, default=str), {"ms": round((time.perf_counter() - t0) * 1000)}
        return StructuredTool.from_function(run, name=name, description=description, args_schema=schema,
                                            response_format="content_and_artifact")
    return [make(*spec) for spec in LOOKUPS]


def submit_tool() -> StructuredTool:
    return StructuredTool.from_function(lambda **kw: "ok", name=SUBMIT, args_schema=Findings,
                                        description="Finish the investigation. Call exactly once, when you have what "
                                                    "you need.")


# --- model ------------------------------------------------------------------------------------------------------

def chat_model():
    """The configured provider as a LangChain chat model: Claude or OpenAI (Responses API, nothing stored)."""
    from ..extraction import claude, openai_llm, provider
    name, _ = provider()
    if name == "openai":
        from langchain_openai import ChatOpenAI
        kw = dict(model=openai_llm.model(), use_responses_api=True, store=False, max_tokens=6000,
                  timeout=openai_llm.TIMEOUT_S, max_retries=1)
        effort = openai_llm.reasoning_effort()
        if effort and kw["model"] not in openai_llm._NO_REASONING:
            kw |= {"reasoning": {"effort": effort}, "include": ["reasoning.encrypted_content"]}
        return ChatOpenAI(**kw)
    if name == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=claude.MODEL, max_tokens=6000, timeout=60, max_retries=1)
    raise NoModel("The investigation agent needs ANTHROPIC_API_KEY or OPENAI_API_KEY.")


def _invoke(bound, model, messages):
    """One model turn. A deployment that rejects the reasoning parameter is remembered and called without it."""
    try:
        return bound.invoke(messages)
    except Exception as e:
        if "reasoning" not in str(e).lower() or not getattr(model, "reasoning", None):
            raise
        from ..extraction import openai_llm
        openai_llm._NO_REASONING.add(model.model_name)
        plain = model.model_copy(update={"reasoning": None, "include": None})
        return bound.__class__(bound=plain, kwargs=bound.kwargs, config=bound.config).invoke(messages)


# --- graph ------------------------------------------------------------------------------------------------------

class InvestigationState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    turn: int
    tokens: int
    budget_hit: bool
    final: dict | None
    stop: str | None
    result: dict


def build(model, tools: domain.Tools, max_turns: int, token_budget: int):
    read = lookups(tools)
    every = read + [submit_tool()]
    free, forced = model.bind_tools(every), model.bind_tools(every, tool_choice=SUBMIT)

    def agent(s: InvestigationState) -> dict:
        turn, tokens = s.get("turn", 0), s.get("tokens", 0)
        last = turn == max_turns - 1 or tokens >= token_budget * 0.8
        msg = _invoke(forced if last else free, model, [SystemMessage(domain.SYSTEM), *s["messages"]])
        row = usage.record_langchain(msg, "investigation") or {}
        tokens += row.get("input_tokens", 0) + row.get("output_tokens", 0) + row.get("cache_read_tokens", 0) \
            + row.get("cache_write_tokens", 0)
        out = {"messages": [msg], "turn": turn + 1, "tokens": tokens}
        if not msg.tool_calls:
            text = msg.text if isinstance(msg.text, str) else ""
            return out | {"stop": "no_submit", "final": {
                "summary": text.strip() or "The agent ended without findings.", "findings": [],
                "recommended_action": "escalate", "confidence": "low", "draft_message": None,
                "open_questions": ["The agent did not submit structured findings."]}}
        sub = next((c for c in msg.tool_calls if c["name"] == SUBMIT), None)
        if sub:
            return out | {"stop": "submitted", "final": sub["args"]}
        return out | {"budget_hit": s.get("budget_hit", False) or tokens >= token_budget * 0.8}

    def after_agent(s: InvestigationState) -> str:
        if s.get("stop") == "no_submit":
            return "finish"
        if any(c["name"] != SUBMIT for c in s["messages"][-1].tool_calls):
            return "tools"
        return "finish"

    def after_tools(s: InvestigationState) -> str:
        return "finish" if s.get("final") is not None or s["turn"] >= max_turns else "agent"

    def finish(s: InvestigationState) -> dict:
        final, stop = s.get("final"), s.get("stop")
        if final is None:
            stop = "token_budget" if s.get("budget_hit") else "turn_limit"
            final = {"summary": "Investigation stopped before findings were submitted.", "findings": [],
                     "recommended_action": "escalate", "confidence": "low", "draft_message": None,
                     "open_questions": [f"Stopped: {stop}."]}
        final = dict(final)
        if final.get("recommended_action") not in domain.ACTIONS:
            final["recommended_action"] = "escalate"
        steps, evidence = _steps(s["messages"])
        return {"result": {"status": stop, "budget_reached": bool(s.get("budget_hit")), "findings": final,
                           "steps": steps, "turns": s["turn"], "tokens": s.get("tokens", 0),
                           "unverified_figures": domain.unverified_figures(final, "\n".join(evidence))}}

    g = StateGraph(InvestigationState)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(every, handle_tool_errors=True))
    g.add_node("finish", finish)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "finish"])
    g.add_conditional_edges("tools", after_tools, ["agent", "finish"])
    g.add_edge("finish", END)
    return g.compile(checkpointer=False, name="investigation")


def _steps(messages) -> tuple[list[dict], list[str]]:
    """Every lookup the agent made, in order, with its arguments, a preview of the result and its timing; and the
    texts the findings are checked against (the case plus every lookup result)."""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    evidence = [m.content for m in messages if isinstance(m, HumanMessage)]
    steps, turn = [], 0
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        turn += 1
        for c in m.tool_calls:
            r = results.get(c["id"])
            if c["name"] == SUBMIT or r is None:
                continue
            steps.append({"turn": turn, "tool": c["name"], "args": c["args"], "result_preview": str(r.content)[:600],
                          "ms": (r.artifact or {}).get("ms", 0) if isinstance(r.artifact, dict) else 0})
            evidence.append(str(r.content))
    return steps, evidence


def investigate(run: dict, ctx: Context, model=None, max_turns: int | None = None,
                token_budget: int | None = None) -> dict:
    """Run the agent on one held invoice. Returns the record that is stored and shown: findings, every lookup,
    turns, tokens, time and the figures the reviewer should double-check."""
    model = model or chat_model()
    max_turns = max_turns or domain.MAX_TURNS
    token_budget = token_budget or domain.TOKEN_BUDGET
    graph = build(model, domain.Tools(ctx, exclude_run=run.get("id")), max_turns, token_budget)
    t0 = time.perf_counter()
    out = graph.invoke({"messages": [HumanMessage(domain.brief(run))]}, {"recursion_limit": 4 * max_turns + 4})
    name = getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__
    return out["result"] | {"seconds": round(time.perf_counter() - t0, 1), "model": str(name)}
