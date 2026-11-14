"""Node 9 - Explanation: plain-language summary of the decision packet; falls back to the verdict on error."""
from __future__ import annotations

from ..core.engine import Decision

STAGE, TITLE = "explanation", "Explanation"


def run(d: Decision, explainer):
    try:
        out = explainer(d.packet)
        text, src = out if isinstance(out, tuple) else (out, "template")
    except Exception as e:
        text, src = d.verdict, f"fallback ({type(e).__name__})"
    return "ok", text, {"source": src}, (text, src)
