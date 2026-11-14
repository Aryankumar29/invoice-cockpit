"""Integration: every fixture PDF through the real extractor, pipeline and API, on a fresh reset each."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cockpit.api import create_app
from cockpit.pipeline import Pipeline, Worker
from cockpit.store import Store

MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "manifest.json"
pytest.importorskip("cockpit.extraction")
if not MANIFEST.exists():
    pytest.skip("fixtures not generated", allow_module_level=True)
CASES = json.loads(MANIFEST.read_text(encoding="utf-8"))
BY_KEY = {c["key"]: c for c in CASES}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # local extractor only: free and deterministic
    monkeypatch.setenv("COCKPIT_REPLAY_DIR", str(tmp_path / "replay"))
    pipe = Pipeline(Store(tmp_path), stage_delay=0)
    c = TestClient(create_app(pipeline=pipe, start_worker=False))
    c.drain = Worker(pipe).drain
    return c


def run(client, key) -> dict:
    rid = client.post(f"/api/fixtures/{key}/run").json()["run_id"]
    client.drain()
    return client.get(f"/api/runs/{rid}").json()


@pytest.mark.parametrize("case", CASES, ids=[c["key"] for c in CASES])
def test_fixture_outcome_through_api(client, case):
    if case["kind"] == "scan" and not shutil.which("tesseract"):
        pytest.skip("tesseract not installed (runs in Docker)")
    for pre in case["prerequisites"]:
        assert run(client, pre)["outcome"] == BY_KEY[pre]["expected"]["outcome"], pre
    d = run(client, case["key"])
    assert (d["outcome"], d["reasons"][0]) == (case["expected"]["outcome"], case["expected"]["reason"]), d["verdict"]
    assert d["extraction_source"] in ("local_text", "local_ocr")


def test_demo_script_in_order_on_one_database(client):
    """The six demo invoices run back to back without a reset."""
    for case in sorted((c for c in CASES if c["demo"]), key=lambda c: c["order"]):
        if case["kind"] == "scan" and not shutil.which("tesseract"):
            continue
        d = run(client, case["key"])
        assert (d["outcome"], d["reasons"][0]) == (case["expected"]["outcome"], case["expected"]["reason"]), case["key"]


def test_unreadable_scan_without_ocr_goes_to_review(client):
    if shutil.which("tesseract"):
        pytest.skip("tesseract present")
    d = run(client, "E2")
    assert d["outcome"] == "needs_review"
