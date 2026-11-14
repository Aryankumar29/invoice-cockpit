"""Score extraction configurations against hand-labelled invoices (samples/_truth.json).

    .venv/Scripts/python scripts/eval_extraction.py                      # free configs only (no API)
    .venv/Scripts/python scripts/eval_extraction.py llm_low              # a model config (costs tokens)
    .venv/Scripts/python scripts/eval_extraction.py local_text local_ocr  # selected configs (no API cost)
    .venv/Scripts/python scripts/eval_extraction.py --list

Each config is one way of reading the document (local text parser, OCR, the LLM at a given reasoning effort,
the LLM on a scanned copy). Per invoice it records every field against the truth, whether the self-checks
(grounding, arithmetic) passed, the decision the rules would take, latency and tokens. The headline numbers:

  field accuracy   share of labelled fields read exactly right
  silent errors    wrong values that passed every self-check (the dangerous kind: nothing flags them)
  unsafe approvals auto-approved while a field was wrong (must be 0)

Results are written to samples/_eval/<timestamp>.json for comparison across tuning rounds.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"
FIELDS = ("invoice_number", "invoice_date", "due_date", "vendor_name", "currency", "po_reference",
          "subtotal", "tax", "freight", "total", "lines")
MONEY = {"subtotal": "subtotal_cents", "tax": "tax_cents", "freight": "freight_cents", "total": "total_cents"}

CONFIGS = {
    "local_text":     {"env": {"COCKPIT_EXTRACTION": "local"}, "scan": False},
    "local_ocr":      {"env": {"COCKPIT_EXTRACTION": "local"}, "scan": True},
    "llm_none":       {"env": {"OPENAI_REASONING_EFFORT": "none"}, "scan": False},
    "llm_low":        {"env": {"OPENAI_REASONING_EFFORT": "low"}, "scan": False},
    "llm_medium":     {"env": {"OPENAI_REASONING_EFFORT": "medium"}, "scan": False},
    "llm_low_scan":   {"env": {"OPENAI_REASONING_EFFORT": "low"}, "scan": True},
    # Production routing (cache -> local-first -> model) with a stub model that only counts the calls the real
    # pipeline would make. Free: measures how many invoices still need the API.
    "auto":           {"env": {"COCKPIT_EXTRACTION": "auto", "OPENAI_REASONING_EFFORT": "low"}, "scan": False},
    "dry_auto":       {"env": {"COCKPIT_EXTRACTION": "auto"}, "scan": False, "dry": True},
    "dry_auto_scan":  {"env": {"COCKPIT_EXTRACTION": "auto"}, "scan": True, "dry": True},
}
FREE = ["local_text", "local_ocr", "dry_auto", "dry_auto_scan"]


class _DryLLM:
    """Stands in for the model: every call is counted and then fails, so routing continues exactly as it
    would after a model failure. No network."""
    MODEL = "dry-run"
    calls = 0

    @staticmethod
    def model():
        return "dry-run"

    @staticmethod
    def make_client():
        return object()

    @classmethod
    def call(cls, client, pdf_bytes, instruction=None, retries=1):
        from cockpit.extraction.claude import LLMError
        cls.calls += 1
        raise LLMError("dry run: model not called", False)

    repair_instruction = staticmethod(lambda raw, problems: "")


def scanned_copy(pdf: bytes, dpi: int = 200) -> bytes:
    """Image-only copy (no text layer), JPEG-compressed like a typical scanner, to test the scan paths."""
    import pdfplumber
    from PIL import Image
    pages = []
    with pdfplumber.open(io.BytesIO(pdf)) as d:
        for p in d.pages:
            pages.append(p.to_image(resolution=dpi).original.convert("L"))
    buf = io.BytesIO()
    jpgs = []
    for im in pages:
        b = io.BytesIO()
        im.save(b, "JPEG", quality=70)
        jpgs.append(Image.open(io.BytesIO(b.getvalue())))
    jpgs[0].save(buf, "PDF", resolution=float(dpi), save_all=True, append_images=jpgs[1:])
    return buf.getvalue()


def _norm(s):
    import re
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def score_field(field: str, got, want) -> bool | None:
    """True/False, or None when the truth does not label this field."""
    accept = want if isinstance(want, list) else [want]
    if field == "vendor_name":
        from rapidfuzz import fuzz
        return bool(got) and max(fuzz.token_set_ratio(_norm(got), _norm(a)) for a in accept) >= 90
    if field in ("invoice_date", "due_date"):
        got = got.isoformat() if got else None
    if field in ("invoice_number", "po_reference"):
        return any((_norm(got) == _norm(a)) if a else not got for a in accept)
    if field == "lines":
        return sorted(got) == sorted(want)
    return got in accept


def extracted(inv) -> dict:
    out = {"invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date, "due_date": inv.due_date,
           "vendor_name": inv.vendor_name, "currency": inv.currency, "po_reference": inv.po_reference,
           "lines": [l.amount_cents for l in inv.lines]}
    out.update({k: getattr(inv, a) for k, a in MONEY.items()})
    return out


def run_config(name: str, cfg: dict, truth: dict) -> dict:
    from cockpit import usage
    from cockpit.api import load_env_file
    load_env_file()
    saved = {k: os.environ.get(k) for k in [*cfg["env"], "COCKPIT_REPLAY_DIR"]}
    os.environ.update(cfg["env"])
    os.environ["COCKPIT_REPLAY_DIR"] = tempfile.mkdtemp(prefix=f"eval-{name}-")  # never answer from cache
    import cockpit.extraction as ex
    from cockpit.core import seed
    real_provider = ex.provider
    if cfg.get("dry"):
        ex.provider = lambda: ("openai", _DryLLM)
    from cockpit.core.engine import evaluate
    rows = []
    try:
        for fname, want in truth.items():
            pdf = (SAMPLES / fname).read_bytes()
            if cfg["scan"]:
                pdf = scanned_copy(pdf)
            calls = []
            usage.set_sink(calls.append)
            dry_before = _DryLLM.calls
            t0 = time.perf_counter()
            res = ex.extract(pdf, f"{name}:{fname}", fname)
            secs = time.perf_counter() - t0
            usage.set_sink(None)
            got = extracted(res.invoice)
            want_v = {**want, "lines": want["line_amounts"]}
            fields = {f: score_field(f, got[f], want_v[f]) for f in FIELDS}
            g = res.grounding
            checks_passed = not g.get("ungrounded") and g.get("arithmetic_ok", False) and res.invoice.readable
            d = evaluate(res.invoice, seed.context())
            wrong = [f for f, ok in fields.items() if ok is False]
            rows.append({
                "file": fname, "source": res.source, "seconds": round(secs, 2),
                "tokens": sum(c["input_tokens"] + c["output_tokens"] + c["cache_read_tokens"] for c in calls),
                "calls": len(calls) + (_DryLLM.calls - dry_before), "fields": fields, "wrong": wrong,
                "got": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in got.items()},
                "checks_passed": checks_passed, "arithmetic_ok": g.get("arithmetic_ok"),
                "ungrounded": g.get("ungrounded", []),
                "silent_errors": wrong if checks_passed else [],
                "outcome": d.outcome, "reasons": d.reasons,
                "unsafe_approval": d.outcome == "auto_approved" and bool(wrong),
                "warnings": res.warnings[:6],
            })
    finally:
        ex.provider = real_provider
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    scored = [ok for r in rows for ok in r["fields"].values() if ok is not None]
    return {"config": name, "rows": rows,
            "field_accuracy": round(sum(scored) / len(scored), 3) if scored else None,
            "docs_all_right": sum(not r["wrong"] for r in rows),
            "silent_errors": sum(len(r["silent_errors"]) for r in rows),
            "unsafe_approvals": sum(r["unsafe_approval"] for r in rows),
            "per_field": {f: round(sum(r["fields"][f] for r in rows) / len(rows), 2) for f in FIELDS},
            "seconds_avg": round(sum(r["seconds"] for r in rows) / len(rows), 2),
            "tokens_total": sum(r["tokens"] for r in rows), "calls_total": sum(r["calls"] for r in rows)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("configs", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    a = ap.parse_args()
    if a.list:
        print("\n".join(CONFIGS))
        return 0
    truth = {k: v for k, v in json.loads((SAMPLES / "_truth.json").read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    names = a.configs or FREE  # model configs cost API tokens: name them explicitly
    results = []
    for n in names:
        r = run_config(n, CONFIGS[n], truth)
        results.append(r)
        print(f"\n=== {n}: field accuracy {r['field_accuracy']:.0%} | docs fully right {r['docs_all_right']}/"
              f"{len(truth)} | silent errors {r['silent_errors']} | unsafe approvals {r['unsafe_approvals']} | "
              f"{r['seconds_avg']}s avg | {r['tokens_total']:,} tokens / {r['calls_total']} calls")
        if not a.quiet:
            for row in r["rows"]:
                flag = "SILENT " if row["silent_errors"] else ("ok     " if not row["wrong"] else "flagged")
                print(f"  {flag} {row['file'][:34]:34} src={row['source']:10} {row['seconds']:5.1f}s "
                      f"wrong={','.join(row['wrong']) or '-':40} -> {row['outcome']} {row['reasons'][:3]}")
                for f in row["wrong"]:
                    print(f"           {f}: got {row['got'][f]!r}")
    out = SAMPLES / "_eval" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    print(f"\n{'config':14} {'accuracy':>8} {'docs ok':>8} {'silent':>7} {'unsafe':>7} {'sec':>6} {'tokens':>8}")
    for r in results:
        print(f"{r['config']:14} {r['field_accuracy']:8.0%} {r['docs_all_right']:>5}/{len(truth)} "
              f"{r['silent_errors']:7} {r['unsafe_approvals']:7} {r['seconds_avg']:6.1f} {r['tokens_total']:8,}")
    print(f"\nper-field accuracy:\n{'':14} " + " ".join(f"{f[:8]:>8}" for f in FIELDS))
    for r in results:
        print(f"{r['config']:14} " + " ".join(f"{r['per_field'][f]:8.0%}" for f in FIELDS))
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
