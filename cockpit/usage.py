"""Claude API spend: every response's token usage is priced and recorded, whatever the call's outcome."""
from __future__ import annotations

import contextvars
from typing import Callable

# USD per million tokens: (input, output, cache write 5m, cache read). Anthropic list prices, 2026-10.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    # OpenAI (developers.openai.com/api/docs/pricing, standard tier, <=272K context). No cache-write charge.
    "gpt-6.1-sol": (2.00, 10.00, 0.0, 0.10),
    "gpt-6-sol": (2.00, 10.00, 0.0, 0.20),
    "gpt-6-luna": (0.10, 0.50, 0.0, 0.01),
    "gpt-6-astra": (10.00, 50.00, 0.0, 1.00),
    "gpt-5.6-sol": (4.00, 20.00, 0.0, 0.40),
    "gpt-5.6-terra": (2.00, 12.00, 0.0, 0.20),
    "gpt-5.6-luna": (0.20, 1.20, 0.0, 0.02),
    "gpt-5.5": (5.00, 30.00, 0.0, 0.50),
    "gpt-5.4": (2.50, 15.00, 0.0, 0.25),
    "gpt-5.4-mini": (0.75, 4.50, 0.0, 0.075),
    "gpt-4.1": (2.00, 8.00, 0.0, 0.50),
    "gpt-4o": (2.50, 10.00, 0.0, 1.25),
}
_UNKNOWN = PRICES["gpt-5.5"]  # unknown model: priced high so spend is never understated


def _price(model: str) -> tuple[float, float, float, float]:
    if model in PRICES:
        return PRICES[model]
    # dated snapshots ("gpt-6.1-sol-2026-09-01") share their base model's price
    base = max((m for m in PRICES if model.startswith(m + "-")), key=len, default=None)
    return PRICES[base] if base else _UNKNOWN

run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("cockpit_run_id", default=None)
_sink: Callable[[dict], None] | None = None


def set_sink(fn: Callable[[dict], None] | None) -> None:
    global _sink
    _sink = fn


def cost_usd(model: str, inp: int, out: int, cache_write: int = 0, cache_read: int = 0) -> float:
    p = _price(model)
    return (inp * p[0] + out * p[1] + cache_write * p[2] + cache_read * p[3]) / 1_000_000


def _emit(purpose: str, model: str, inp: int, out: int, cw: int, cr: int, request_id) -> dict:
    row = {"run_id": run_id.get(), "purpose": purpose, "model": model or "", "input_tokens": inp,
           "output_tokens": out, "cache_write_tokens": cw, "cache_read_tokens": cr, "request_id": request_id,
           "cost_usd": cost_usd(model or "", inp, out, cw, cr)}
    if _sink:
        _sink(row)
    return row


def record(resp, purpose: str) -> dict | None:
    """Price one Anthropic Messages response. Never raises: accounting must not break a run."""
    try:
        u = resp.usage
        return _emit(purpose, getattr(resp, "model", ""), u.input_tokens or 0, u.output_tokens or 0,
                     getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0,
                     getattr(resp, "_request_id", None))
    except Exception:
        return None


def record_openai(resp, purpose: str) -> dict | None:
    """Price one OpenAI Responses response. OpenAI's input_tokens include cached tokens; split them out."""
    try:
        u = resp.usage
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        return _emit(purpose, getattr(resp, "model", ""), max((u.input_tokens or 0) - cached, 0),
                     u.output_tokens or 0, 0, cached, getattr(resp, "_request_id", None) or getattr(resp, "id", None))
    except Exception:
        return None
