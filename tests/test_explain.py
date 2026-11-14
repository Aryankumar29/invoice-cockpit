"""A9 explanation + memo: T-A9-1 (template per reason code), T-A9-2 (invented number discarded)."""
import copy
from types import SimpleNamespace

import pytest

import cockpit.explain as X
from cockpit.core import seed
from cockpit.core.engine import _ACTIONS, evaluate, packet_hash
from cockpit.extraction import claude


def packet(key):
    return evaluate(seed.fixture(key), seed.context()).packet


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("COCKPIT_LLM_EXPLAIN", raising=False)


def test_every_reason_code_has_a_template():
    assert set(_ACTIONS) <= set(X.WHY)


@pytest.mark.parametrize("code", sorted(_ACTIONS))
def test_t_a9_1_template_for_each_reason_code(code):
    p = copy.deepcopy(packet("D1"))
    outcome = "auto_approved" if code == "STP_CLEAN" else "needs_review"
    p.update(outcome=outcome, reasons=[code], recommended_action=_ACTIONS[code])
    for r in p["rules"]:
        if r["status"] == "fail":
            r.update(reason=code)
    text = X.explain(p)
    assert text.startswith(X.OUTCOME_WORDS[outcome])
    assert X.WHY[code] in text
    if code != "STP_CLEAN":
        assert f"[{code}]" in text
    assert f"Recommended action: {_ACTIONS[code]}." in text
    assert X.check_invariants(text, p) == []


@pytest.mark.parametrize("key,must", [
    ("H1", ["Auto-approved", "INV-H-001", "$4,800.00", "PO-1042", "16 checks"]),
    ("D1", ["Needs review", "$60.00", "$40.00", "Line 2", "[PRICE_VARIANCE]", "credit note"]),
    ("E1", ["Rejected", "NW-2026-001", "[DUPLICATE_INVOICE]", "run-0007"]),
    ("E2", ["Needs review", "no number", "[MISSING_INVOICE_NUMBER]"]),
])
def test_scenario_explanations_cite_packet_numbers(key, must):
    text, source = X.explain_source(packet(key))
    assert source == "template"
    for m in must:
        assert m in text, (m, text)


def test_explanation_is_deterministic():
    assert X.explain(packet("D1")) == X.explain(packet("D1"))


def _fake_llm(monkeypatch, text, stop="end_turn"):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COCKPIT_LLM_EXPLAIN", "1")
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])
    monkeypatch.setattr(claude, "make_client", lambda: SimpleNamespace(beta=SimpleNamespace(
        messages=SimpleNamespace(create=create))))
    return calls


def test_t_a9_2_llm_summary_with_invented_number_is_discarded(monkeypatch):
    p = packet("D1")
    _fake_llm(monkeypatch, "Needs review [PRICE_VARIANCE]: line 2 is $75.00 over the PO price; allowed $40.00.")
    text, source = X.explain_source(p)
    assert source == "template" and text == X.template(p)
    assert any("75.00" in s for s in X.check_invariants("Needs review [PRICE_VARIANCE] $75.00", p))


def test_t_a9_2_llm_summary_that_changes_outcome_or_drops_a_reason_is_discarded(monkeypatch):
    p = packet("D1")
    assert X.check_invariants("Approved [PRICE_VARIANCE]: line 2 is $60.00 over.", p)
    assert X.check_invariants("Needs review: line 2 is $60.00 over; allowed $40.00.", p)
    _fake_llm(monkeypatch, "Rejected [PRICE_VARIANCE]: line 2 is $60.00 above the PO.")
    assert X.explain_source(p)[1] == "template"


def test_valid_llm_rewrite_is_accepted_and_labelled(monkeypatch):
    p = packet("D1")
    good = ("Needs review [PRICE_VARIANCE]. Invoice INV-D-001 from Northwind Supply Co. bills line 2 at $60.00 "
            "above PO-1051, while the allowed variance is $40.00. Ask the vendor for a credit note.")
    calls = _fake_llm(monkeypatch, good)
    assert X.explain_source(p) == (good, "claude")
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert not {"temperature", "top_p"} & set(kw)


def test_llm_not_called_without_opt_in(monkeypatch):
    calls = _fake_llm(monkeypatch, "x")
    monkeypatch.delenv("COCKPIT_LLM_EXPLAIN")
    assert X.explain_source(packet("D1"))[1] == "template" and calls == []


def test_llm_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COCKPIT_LLM_EXPLAIN", "1")
    monkeypatch.setattr(claude, "make_client", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert X.explain_source(packet("D1"))[1] == "template"


def test_memo_is_self_contained_and_complete():
    p = packet("D1")
    run = {"id": "run-42", "created_at": "2026-10-26T10:00:00Z", "filename": "D1.pdf <x>",
           "extraction_source": "local_text", "warnings": ["No API key: local extractor used."],
           "stages": [{"seq": 1, "stage": "intake", "status": "ok", "title": "Intake", "message": "ok",
                       "ts": "2026-10-26T10:00:00Z"}],
           "actions": [{"action": "escalate", "reviewer": "pat", "note": "<b>check</b>", "ts": "t",
                        "outcome_before": "needs_review", "outcome_after": "needs_review"}]}
    h = X.memo(p, run)
    assert h.startswith("<!doctype html>") and "<script" not in h and "http" not in h.split("</style>")[0]
    assert packet_hash(p) in h and p["policy_version"] in h
    for s in ["PRICE_VARIANCE", "R16", "$60.00", "$40.00", "Widget B, aluminium, 12 mm", "Audit trail", "pat"]:
        assert s in h, s
    assert "<b>check</b>" not in h and "&lt;b&gt;check&lt;/b&gt;" in h
    assert "D1.pdf &lt;x&gt;" in h


def test_memo_for_duplicate_lists_the_original():
    h = X.memo(packet("E1"), {})
    assert "run-0007" in h and "DUPLICATE_INVOICE" in h
