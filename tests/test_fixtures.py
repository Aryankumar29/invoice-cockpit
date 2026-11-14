"""Fixtures: every PDF opens, text layers carry the extracted values, and the engine decides as the manifest says."""
import hashlib
import json
import re
from pathlib import Path

import pdfplumber
import pytest
from pypdf import PdfReader

from cockpit.core import seed
from cockpit.core.engine import evaluate, to_prior
from cockpit.core.normalize import bank_fingerprint
from fixtures.generate import from_extraction, money, to_extraction

ROOT = Path(__file__).resolve().parent.parent / "fixtures"
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
BY_KEY = {e["key"]: e for e in MANIFEST}
DIGITAL = [e["key"] for e in MANIFEST if e["kind"] == "digital"]


def _text(key):
    with pdfplumber.open(ROOT / BY_KEY[key]["file"]) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def test_manifest_shape():
    demo = sorted((e for e in MANIFEST if e["demo"]), key=lambda e: e["order"])
    assert [e["key"] for e in demo] == ["H1", "S1", "S2", "D1", "E1", "E2"]
    assert [e["order"] for e in demo] == [1, 2, 3, 4, 5, 6]
    assert len(BY_KEY) == len(MANIFEST)
    for e in MANIFEST:
        assert set(e) >= {"key", "file", "kind", "demo", "order", "title", "description", "expected",
                          "extraction", "prerequisites"}
        assert all(p in BY_KEY and BY_KEY[p]["order"] < e["order"] for p in e["prerequisites"])


@pytest.mark.parametrize("key", list(BY_KEY))
def test_pdf_opens_and_hash_matches(key):
    path = ROOT / BY_KEY[key]["file"]
    assert len(PdfReader(path).pages) == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == BY_KEY[key]["extraction"]["file_hash"]


@pytest.mark.parametrize("key", DIGITAL)
def test_digital_text_layer_grounds_every_amount(key):
    ex, text = BY_KEY[key]["extraction"], _text(key)
    assert ex["invoice_number"] in text
    amounts = [ex["total_cents"], ex["subtotal_cents"], ex["tax_cents"], ex["freight_cents"]]
    amounts += [c for l in ex["lines"] for c in (l["unit_price_cents"], l["amount_cents"])]
    missing = [money(c) for c in amounts if money(c) not in text]
    assert not missing, f"not in text layer: {missing}"
    acct = re.search(r"(?:Account No\.|Remit to account)\s*(\d{10})", text)
    assert acct and bank_fingerprint(acct.group(1)) == ex["bank_hash"]


def test_scan_has_no_text_layer():
    e = BY_KEY["E2"]
    assert e["kind"] == "scan"
    assert _text("E2").strip() == ""
    with pdfplumber.open(ROOT / e["file"]) as pdf:
        assert len(pdf.pages[0].images) == 1 and not pdf.pages[0].chars


def test_altered_template_uses_synonym_labels():
    text = _text("E1")
    for label in ("Inv #", "Bill Date", "Your PO", "EIN", "Amount Due", "Remit to account"):
        assert label in text
    assert "Invoice Number:" not in text
    assert BY_KEY["E1"]["extraction"]["file_hash"] != BY_KEY["H1"]["extraction"]["file_hash"]


@pytest.mark.parametrize("key", list(seed.FIXTURES))
def test_demo_extraction_equals_seed(key):
    ex = dict(BY_KEY[key]["extraction"])
    want = to_extraction(seed.FIXTURES[key])
    for k in ("file_hash", "bank_hash"):
        ex.pop(k), want.pop(k)
    assert ex == want
    assert (BY_KEY[key]["expected"]["outcome"], BY_KEY[key]["expected"]["reason"]) == seed.EXPECTED[key]


@pytest.mark.parametrize("key", list(BY_KEY))
def test_bank_hash_is_printed_account(key):
    e = BY_KEY[key]
    assert e["extraction"]["bank_hash"] and e["extraction"]["bank_hash"].startswith("acct-")


@pytest.mark.parametrize("key", list(BY_KEY))
def test_engine_decides_as_manifest_expects(key):
    ctx = seed.context()
    for p in BY_KEY[key]["prerequisites"]:
        inv = from_extraction(BY_KEY[p]["extraction"])
        ctx.history.append(to_prior(inv, evaluate(inv, ctx), f"run-{p}"))
    d = evaluate(from_extraction(BY_KEY[key]["extraction"]), ctx)
    exp = BY_KEY[key]["expected"]
    assert (d.outcome, d.reasons[0]) == (exp["outcome"], exp["reason"]), d.verdict
