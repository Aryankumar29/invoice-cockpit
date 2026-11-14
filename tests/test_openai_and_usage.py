"""OpenAI extraction path, provider selection, API-spend accounting, .env loading."""
import dataclasses
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import cockpit.extraction as ex
from cockpit import usage
from cockpit.api import create_app, load_env_file
from cockpit.core import seed
from cockpit.extraction import claude, openai_llm
from cockpit.pipeline import Pipeline
from cockpit.store import Store
from test_extraction import make_pdf, raw_of, reply, sha, use_claude


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("COCKPIT_EXTRACTION", raising=False)
    monkeypatch.setenv("COCKPIT_REPLAY_DIR", str(tmp_path / "replay"))
    monkeypatch.setenv("COCKPIT_LOCAL_FIRST", "0")  # these tests exercise the model path itself
    monkeypatch.setenv("COCKPIT_CACHE_FIRST", "0")
    rows = []
    usage.set_sink(rows.append)
    yield rows
    usage.set_sink(None)


def oa_reply(raw=None, *, status="completed", refusal=None, model="gpt-6.1-sol", inp=1000, out=200, cached=400):
    parts = [SimpleNamespace(type="refusal", refusal=refusal)] if refusal else []
    return SimpleNamespace(
        status=status, incomplete_details=SimpleNamespace(reason="max_output_tokens") if status == "incomplete" else None,
        output=[SimpleNamespace(type="message", content=parts)], output_text=json.dumps(raw) if raw else "",
        model=model, id="resp_1",
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out,
                              input_tokens_details=SimpleNamespace(cached_tokens=cached)))


class FakeOpenAI:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def use_openai(monkeypatch, *outcomes):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake = FakeOpenAI(*outcomes)
    monkeypatch.setattr(openai_llm, "make_client", lambda: fake)
    return fake


def test_openai_request_shape_and_result(monkeypatch, tmp_path, _env):
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    fake = use_openai(monkeypatch, oa_reply(raw_of(inv)))
    res = ex.extract(pdf, sha(pdf))
    assert res.source == "openai" and res.grounding["ungrounded"] == []
    kw = fake.calls[0]
    assert kw["model"] == "gpt-6.1-sol" and kw["store"] is False
    fmt = kw["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["schema"] is claude.SCHEMA
    f = kw["input"][0]["content"][0]
    assert f["type"] == "input_file" and f["file_data"].startswith("data:application/pdf;base64,")
    assert "untrusted" in kw["instructions"]
    cache = json.loads((tmp_path / "replay" / f"{sha(pdf)}.json").read_text())
    assert cache["source"] == "openai" and cache["model"] == "gpt-6.1-sol"


def test_openai_usage_is_priced_with_cached_tokens_split_out(monkeypatch, _env):
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    use_openai(monkeypatch, oa_reply(raw_of(inv), inp=1000, out=200, cached=400))
    ex.extract(pdf, sha(pdf))
    (row,) = _env
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (600, 400, 200)
    assert row["cost_usd"] == pytest.approx((600 * 2.00 + 400 * 0.10 + 200 * 10.00) / 1e6)


def test_reasoning_effort_sent_and_dropped_for_models_that_reject_it(monkeypatch):
    import httpx2 as httpx
    import openai
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    fake = use_openai(monkeypatch, oa_reply(raw_of(inv)))
    ex.extract(pdf, sha(pdf))
    assert fake.calls[0]["reasoning"] == {"effort": "low"}
    monkeypatch.setenv("OPENAI_MODEL", "plain-model")
    req = httpx.Request("POST", "https://x/responses")
    bad = openai.BadRequestError("Unsupported parameter: 'reasoning'", response=httpx.Response(400, request=req), body=None)
    fake = use_openai(monkeypatch, bad, oa_reply(raw_of(inv), model="plain-model"), oa_reply(raw_of(inv), model="plain-model"))
    res = ex.extract(pdf, sha(pdf))
    assert res.source == "openai" and "reasoning" in fake.calls[0] and "reasoning" not in fake.calls[1]
    pdf2 = make_pdf(inv, bank="0000007782")
    ex.extract(pdf2, sha(pdf2))
    assert "reasoning" not in fake.calls[2]  # remembered: not sent to that deployment again


def test_openai_model_env_override(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4-mini")
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    fake = use_openai(monkeypatch, oa_reply(raw_of(inv), model="gpt-5.4-mini"))
    ex.extract(pdf, sha(pdf))
    assert fake.calls[0]["model"] == "gpt-5.4-mini"


def test_openai_refusal_and_incomplete_fall_back_to_local(monkeypatch):
    pdf = make_pdf(seed.fixture("H1"))
    fake = use_openai(monkeypatch, oa_reply(refusal="no"))
    res = ex.extract(pdf, sha(pdf))
    assert res.source == "local_text" and len(fake.calls) == 1
    fake = use_openai(monkeypatch, oa_reply(status="incomplete"), oa_reply(status="incomplete"))
    res = ex.extract(pdf, sha(pdf))
    assert res.source == "local_text" and len(fake.calls) == 2  # truncation is retried once


def test_openai_hallucinated_total_is_caught_by_grounding(monkeypatch):
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    use_openai(monkeypatch, oa_reply(raw_of(inv, total="9,999.00")), oa_reply(raw_of(inv, total="9,999.00")))
    res = ex.extract(pdf, sha(pdf))
    assert res.source == "local_text"  # never the ungrounded model answer


def test_provider_selection(monkeypatch):
    assert ex.provider() == (None, None)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert ex.provider()[0] == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert ex.provider()[0] == "claude"
    monkeypatch.setenv("COCKPIT_LLM", "openai")
    assert ex.provider()[0] == "openai"


def test_claude_usage_priced(monkeypatch, _env):
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    r = reply(raw_of(inv))
    r.usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                              cache_read_input_tokens=0)
    use_claude(monkeypatch, r)
    ex.extract(pdf, sha(pdf))
    assert _env[0]["cost_usd"] == pytest.approx((1000 * 4 + 100 * 20) / 1e6)


def test_unknown_and_snapshot_models_never_underpriced():
    assert usage.cost_usd("gpt-6.1-sol-2026-09-01", 10**6, 0) == 2.00
    assert usage.cost_usd("some-new-model", 10**6, 0) == usage.PRICES["gpt-5.5"][0]


def test_usage_stats_survive_reset_and_reach_dashboard(tmp_path):
    store = Store(tmp_path)
    api = TestClient(create_app(pipeline=Pipeline(store, stage_delay=0), start_worker=False))
    usage.run_id.set("run-a")
    usage._emit("extraction", "gpt-6.1-sol", 1000, 100, 0, 0, None)
    usage.run_id.set(None)
    usage._emit("extraction", "gpt-6.1-sol", 1000, 100, 0, 0, None)
    api.post("/api/reset")
    u = api.get("/api/stats").json()["api_usage"]
    assert u["calls"] == 2 and u["total_tokens"] == 2200 and u["input_tokens"] == 2000 and u["output_tokens"] == 200
    assert u["invoices"] == 1 and u["avg_tokens_per_invoice"] == 1100 and u["avg_calls_per_invoice"] == 1
    assert u["by_model"][0] == {"model": "gpt-6.1-sol", "calls": 2, "tokens": 2200}
    assert not any("usd" in k for k in u)  # usage only, no prices on the dashboard
    h = api.get("/api/health").json()
    assert h["extraction_mode"] == "local" and h["llm_model"] is None


def test_health_reports_openai(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    api = TestClient(create_app(data_dir=tmp_path, start_worker=False))
    h = api.get("/api/health").json()
    assert h["extraction_mode"] == "openai" and h["api_key_present"] and h["llm_model"] == "gpt-6.1-sol"


def test_env_file_loads_without_overriding_shell(monkeypatch, tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nOPENAI_API_KEY=sk-from-file\nOPENAI_MODEL=\nCOCKPIT_LLM='openai'\n", encoding="utf-8")
    monkeypatch.setenv("COCKPIT_LLM", "claude")
    load_env_file(p)
    import os
    assert os.environ["OPENAI_API_KEY"] == "sk-from-file"
    assert os.environ["COCKPIT_LLM"] == "claude"  # shell wins
    assert "OPENAI_MODEL" not in os.environ  # blank value ignored


# ---- API savers ------------------------------------------------------------------------------------

def test_local_first_skips_the_model_on_a_clean_digital_invoice(monkeypatch):
    monkeypatch.setenv("COCKPIT_LOCAL_FIRST", "1")
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    fake = use_openai(monkeypatch)  # any call would pop from an empty list and fail
    res = ex.extract(pdf, sha(pdf))
    assert res.source == "local_text" and fake.calls == [] and res.invoice.total_cents == inv.total_cents
    assert any("No model call needed" in n for n in res.notes)


def test_local_first_falls_through_to_the_model_when_incomplete(monkeypatch):
    monkeypatch.setenv("COCKPIT_LOCAL_FIRST", "1")
    inv = dataclasses.replace(seed.fixture("H1"), invoice_number=None)
    pdf = make_pdf(inv)
    fake = use_openai(monkeypatch, oa_reply(raw_of(inv, invoice_number=None)))
    res = ex.extract(pdf, sha(pdf))
    assert len(fake.calls) == 1 and res.source == "openai"


def test_cache_first_reuses_an_identical_file_without_calling(monkeypatch):
    monkeypatch.setenv("COCKPIT_CACHE_FIRST", "1")
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    use_openai(monkeypatch, oa_reply(raw_of(inv)))
    assert ex.extract(pdf, sha(pdf)).source == "openai"
    fake = use_openai(monkeypatch)
    res = ex.extract(pdf, sha(pdf))
    assert res.source == "cache" and fake.calls == [] and res.grounding["ungrounded"] == []


def test_cache_is_ignored_after_a_prompt_change(monkeypatch):
    monkeypatch.setenv("COCKPIT_CACHE_FIRST", "1")
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    use_openai(monkeypatch, oa_reply(raw_of(inv)))
    ex.extract(pdf, sha(pdf))
    monkeypatch.setattr(claude, "SYSTEM", claude.SYSTEM + " ")
    fake = use_openai(monkeypatch, oa_reply(raw_of(inv)))
    assert ex.extract(pdf, sha(pdf)).source == "openai" and len(fake.calls) == 1


def test_discount_reconciles(monkeypatch):
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    raw = raw_of(inv, discount="480.00", total="4,320.00")
    inv2, _, derived = ex.to_invoice(raw, "", "h")
    g = ex.ground(inv2, "", derived)
    assert inv2.discount_cents == 48000 and g["arithmetic_ok"]
    from cockpit.core.engine import evaluate
    d = evaluate(dataclasses.replace(inv2, file_hash="h"), seed.context())
    assert "ARITHMETIC_MISMATCH" not in d.reasons


# ---- every field, any template ----------------------------------------------------------------------

def test_model_extra_fields_and_line_columns_are_captured_and_verified(monkeypatch):
    inv = seed.fixture("H1")
    pdf = make_pdf(inv, extra=["Order ID: SO-99812", "Ship Mode: Ground"])
    raw = raw_of(inv)
    raw["other_fields"] = [{"label": "Order ID", "value": "SO-99812", "section": "header"},
                           {"label": "Ship Mode", "value": "Ground", "section": "shipping"},
                           {"label": "Account Manager", "value": "Jane Invented", "section": "seller"},
                           {"label": "Weird", "value": "x", "section": "not-a-section"}]
    raw["lines"][0]["extra"] = [{"label": "Tax %", "value": "0%"}]
    use_openai(monkeypatch, oa_reply(raw))
    res = ex.extract(pdf, sha(pdf))
    got = {f["label"]: f for f in res.invoice.other_fields}
    assert got["Order ID"]["verified"] is True and got["Ship Mode"]["section"] == "shipping"
    assert got["Account Manager"]["verified"] is False  # not printed: kept, flagged, never decisive
    assert got["Weird"]["section"] == "other"
    assert res.invoice.lines[0].extra == [{"label": "Tax %", "value": "0%", "verified": False}]
    from cockpit.core.engine import evaluate
    assert evaluate(res.invoice, seed.context()).outcome == "auto_approved"  # extras never change the decision


def test_schema_requires_other_fields_and_line_extras():
    assert "other_fields" in claude.SCHEMA["required"]
    assert "extra" in claude.SCHEMA["properties"]["lines"]["items"]["required"]
    assert "EVERY other piece of labelled information" in claude.SYSTEM


def test_local_parser_captures_labelled_custom_fields():
    from cockpit.extraction import local
    r = local.parse_text("\n".join(["Invoice Number: A-1", "Order ID : MX-2012-41250", "Ship Mode:",
                                    "Standard Class", "Salesperson: R. Diaz", "Total: $5.00"]))
    got = {f["label"]: f["value"] for f in r["other_fields"]}
    assert got == {"Order ID": "MX-2012-41250", "Ship Mode": "Standard Class", "Salesperson": "R. Diaz"}


def test_new_vendor_template_always_gets_the_full_model_read(monkeypatch):
    monkeypatch.setenv("COCKPIT_LOCAL_FIRST", "1")
    inv = seed.fixture("H1")
    pdf = make_pdf(inv)
    fake = use_openai(monkeypatch, oa_reply(raw_of(inv)))
    res = ex.extract(pdf, sha(pdf), known_vendor=lambda i: False)
    assert len(fake.calls) == 1 and res.source == "openai"
    fake = use_openai(monkeypatch)
    res = ex.extract(pdf, sha(pdf) + "b", known_vendor=lambda i: True)
    assert fake.calls == [] and res.source == "local_text"


def test_extraction_json_export(tmp_path):
    from cockpit.api import create_app
    from cockpit.pipeline import Pipeline, Worker
    pipe = Pipeline(Store(tmp_path), stage_delay=0)
    api = TestClient(create_app(pipeline=pipe, start_worker=False))
    pdf = make_pdf(seed.fixture("H1"), extra=["Order ID: SO-1"])
    rid = api.post("/api/runs", files={"file": ("h1.pdf", pdf, "application/pdf")}).json()["run_id"]
    Worker(pipe).drain()
    body = api.get(f"/api/runs/{rid}/extraction.json").json()
    assert body["invoice"]["invoice_number"] == "INV-H-001" and body["decision"]["outcome"]
    assert {"label": "Order ID", "value": "SO-1", "verified": True, "section": "other"} in body["invoice"]["other_fields"]
