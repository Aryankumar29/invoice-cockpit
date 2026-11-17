"""Snapshot every decision the pipeline makes on the bundled fixtures, processed in demo order in one store (so
split-PO ledgers and duplicates interact), with the free local extractor. Used to prove that the LangGraph
rebuild decides exactly like the v1 pipeline: same outcome, reason codes and packet SHA-256 for every invoice.

    python scripts/decision_snapshot.py out.json [--pdf-dir DIR ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))


def stable_hash(packet, label: dict) -> str | None:
    """Packet SHA-256 with run ids (random per run, cited when a packet points at an earlier invoice) replaced by
    each run's position, so two snapshots of the same inputs are comparable."""
    if packet is None:
        return None
    s = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    for rid, name in label.items():
        s = s.replace(rid, name)
    return hashlib.sha256(s.encode()).hexdigest()


def snapshot(pdfs: list[Path]) -> list[dict]:
    from cockpit.pipeline import Pipeline, Worker
    from cockpit.store import Store
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        os.environ["COCKPIT_REPLAY_DIR"] = str(Path(d) / "replay")
        store = Store(Path(d) / "db")
        pipe = Pipeline(store, explainer=lambda p: p["verdict"], stage_delay=0)
        rids = []
        for p in pdfs:
            data = p.read_bytes()
            rids.append(store.create_run(p.name, hashlib.sha256(data).hexdigest(), data)[0])
        Worker(pipe).drain()
        label = {rid: f"run#{i}" for i, rid in enumerate(rids)}
        out = []
        for p, rid in zip(pdfs, rids):
            r = store.run(rid)
            det = r["detail"] or {}
            out.append({"file": p.name, "status": r["status"], "outcome": r["outcome"], "reasons": r["reasons"],
                        "packet_hash": stable_hash(det.get("packet"), label), "extraction_source": r["extraction_source"],
                        "stages": [f"{e['stage']}:{e['status']}" for e in store.events(rid)
                                   if e["status"] != "started"]})
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--pdf-dir", action="append", default=[])
    a = ap.parse_args()
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(k, None)
    os.environ["COCKPIT_EXTRACTION"] = "local"
    manifest = json.loads((ROOT / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    pdfs = [ROOT / "fixtures" / c["file"] for c in sorted(manifest, key=lambda c: c.get("order", 99))]
    for d in a.pdf_dir:
        pdfs += sorted(Path(d).glob("*.pdf"))
    rows = snapshot(pdfs)
    Path(a.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"{len(rows)} decisions -> {a.out}")


if __name__ == "__main__":
    main()
