"""OpenAI extraction: Responses API, native PDF input (text + page images), strict JSON-schema output.

Same contract as claude.py: call() returns (raw, note, attempts) or raises LLMError; the schema, prompt and
grounding are shared, so the rules see identical input whichever provider read the document.
"""
from __future__ import annotations

import base64
import json
import os

from cockpit import usage

from .claude import SCHEMA, SYSTEM, USER, LLMError, repair_instruction, status_detail  # noqa: F401  (re-exported)

DEFAULT_MODEL = "gpt-6.1-sol"


def model() -> str:
    return os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL


TIMEOUT_S = 30.0


def api_key_present() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def make_client():
    import openai
    return openai.OpenAI(timeout=TIMEOUT_S, max_retries=0)


def _content(pdf_bytes: bytes, text: str, filename: str = "invoice.pdf") -> list[dict]:
    return [{"type": "input_file", "filename": filename or "invoice.pdf",
             "file_data": "data:application/pdf;base64," + base64.standard_b64encode(pdf_bytes).decode()},
            {"type": "input_text", "text": text}]


_NO_REASONING: set[str] = set()  # deployments that rejected the reasoning parameter; not sent to them again


def reasoning_effort() -> str | None:
    """Reading an invoice needs little deliberation: on gpt-6-sol, 'low' read E2 as correctly as the default in
    half the time (6.8 s vs 14 s). Blank sends no reasoning parameter (non-reasoning models)."""
    v = os.environ.get("OPENAI_REASONING_EFFORT", "low").strip().lower()
    return v or None


def _create(client, content: list[dict]):
    import openai
    kw = dict(model=model(), instructions=SYSTEM, max_output_tokens=16000, store=False,
              input=[{"role": "user", "content": content}],
              text={"format": {"type": "json_schema", "name": "invoice", "strict": True, "schema": SCHEMA}})
    effort = reasoning_effort()
    if effort and kw["model"] not in _NO_REASONING:
        try:
            return client.responses.create(**kw, reasoning={"effort": effort})
        except openai.BadRequestError as e:
            if "reasoning" not in str(e).lower():
                raise
            _NO_REASONING.add(kw["model"])
    return client.responses.create(**kw)


def _call(client, content: list[dict]) -> tuple[dict, str]:
    import openai
    try:
        resp = _create(client, content)
    except openai.APITimeoutError:
        raise LLMError(f"timeout after {TIMEOUT_S:g}s", True)
    except openai.APIConnectionError as e:
        raise LLMError(f"connection error: {e}", True)
    except openai.APIStatusError as e:
        raise LLMError(status_detail(e.status_code, "OPENAI_API_KEY"), e.status_code == 429 or e.status_code >= 500)
    usage.record_openai(resp, "extraction")
    if resp.status == "incomplete":
        reason = getattr(resp.incomplete_details, "reason", None)
        raise LLMError(f"response incomplete ({reason})", reason == "max_output_tokens")
    for item in resp.output or []:
        for part in getattr(item, "content", None) or []:
            if part.type == "refusal":
                raise LLMError(f"refusal: {part.refusal[:120]}", False)
    text = resp.output_text
    if not text:
        raise LLMError("no output text in response", True)
    try:
        raw = json.loads(text)
    except ValueError:
        raise LLMError("response was not valid JSON", True)
    return raw, f"model {getattr(resp, 'model', model())}"


def call(client, pdf_bytes: bytes, instruction: str = USER, retries: int = 1) -> tuple[dict, str, int]:
    content = _content(pdf_bytes, instruction)
    for i in range(retries + 1):
        try:
            raw, note = _call(client, content)
            return raw, note, i + 1
        except LLMError as e:
            if not e.retryable or i == retries:
                e.detail += f" (attempt {i + 1})"
                raise
