#!/usr/bin/env python3
"""Replay frozen employer responses through the real resolver, with no network.

This corpus is a development regression benchmark. It is not a holdout and
must not be presented as an estimate of overall production accuracy.
"""
import argparse
import contextlib
import copy
import gzip
import hashlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import employer_degree as employer
import job_bot as bot

CORPUS = ROOT / "tests/fixtures/routing_audit.json.gz"


def load_corpus():
    data = json.loads(gzip.decompress(CORPUS.read_bytes()))
    for case in data["cases"]:
        raw = case["response"]
        actual = hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None
        if actual != case["response_sha256"]:
            raise ValueError("Corrupt response for audit case " + str(case["job"]["audit_id"]))
    return data


def replay():
    corpus = load_corpus()
    results = []
    # Catch accidental live requests, including future fallback implementations.
    with patch("socket.socket", side_effect=AssertionError("Audit replay must stay offline")), \
         patch.multiple(bot, EMPLOYER_LOOKUP=True, MAX_EMPLOYER_LOOKUPS=1, DEGREE_LOOKUP=False):
        for case in corpus["cases"]:
            job = copy.deepcopy(case["job"])

            def fetch(url, **kwargs):
                if url != case["request_url"]:
                    raise AssertionError("Unexpected employer endpoint: " + url)
                if case["response"] is None:
                    raise TimeoutError("Recorded employer fetch failed")
                return case["response"]

            with patch.object(employer, "fetch_public_text", side_effect=fetch), contextlib.redirect_stdout(io.StringIO()):
                bot.resolve_degrees([job], {})
            label = case["reference_label"]
            expected = None if label == "unclear" else "grad" if label == "grad" else "bachelors" if job["kind"] == "internship" else "newgrad"
            results.append({
                "audit_id": job["audit_id"], "company": job["company"],
                "url": job["url"], "reference_label": label,
                "expected_channel": expected, "actual_channel": bot.route(job),
                "degree": job["degree"], "degree_source": job["degree_src"],
                "lookup_status": job.get("degree_evidence", {}).get("status"),
                "evidence": job.get("degree_evidence", {}).get("evidence"),
                "correct": bot.route(job) == expected if expected else None,
            })
    scored = [r for r in results if r["correct"] is not None]
    grad = [r for r in scored if r["expected_channel"] == "grad"]
    non_grad = [r for r in scored if r["expected_channel"] != "grad"]
    predicted_grad = [r for r in scored if r["actual_channel"] == "grad"]
    tp = sum(r["correct"] for r in grad)
    return {
        "benchmark": "2026-10-30 recorded routing audit (development regression corpus)",
        "limitation": corpus["review"], "network_requests": 0,
        "cases": len(results), "scored": len(scored), "excluded_unclear": len(results) - len(scored),
        "correct": sum(r["correct"] for r in scored),
        "accuracy_pct": 100 * sum(r["correct"] for r in scored) / len(scored),
        "graduate_required": len(grad), "graduate_correct": tp,
        "graduate_recall_pct": 100 * tp / len(grad),
        "graduate_precision_pct": 100 * tp / len(predicted_grad) if predicted_grad else None,
        "non_graduate_required": len(non_grad),
        "non_graduate_retained": sum(r["correct"] for r in non_grad),
        "non_graduate_retention_pct": 100 * sum(r["correct"] for r in non_grad) / len(non_grad),
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save full results as JSON")
    args = parser.parse_args()
    report = replay()
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    sys.exit(0 if report["correct"] == report["scored"] else 1)
