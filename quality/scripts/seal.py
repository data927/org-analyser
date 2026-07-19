#!/usr/bin/env python3
"""
seal.py — wrap a repo-quality-score result into a re-derivable, tamper-evident
"sealed bundle" for download.

NOTE: score.py and aggregate_org.py already SEAL THEIR OUTPUT BY DEFAULT, so in the
normal skill flow you do not call this script. Use it only to (re-)seal a raw result
JSON, or to attach evidence after the fact.

Usage:
    python seal.py <result-json> --evidence <stats-json> [--evidence <stats-json> ...]
                   [--as-of <ISO-8601>] [--report <out-path>]

Positional arguments:
    result-json         A score.py / aggregate_org.py result (raw, or already sealed —
                        if already sealed it is re-sealed with the given evidence).

Options:
    --evidence PATH     Raw-signal JSON to embed as evidence — a repo_stats.py and/or
                        git_stats.py output (or a per-repo sealed bundle, for an org).
                        Repeatable. These are the inputs the score was derived from;
                        embedding them lets the methodology owner re-derive and
                        cross-check (see verify.py).
    --as-of ISO         Generation timestamp to record (ISO-8601). Defaults to now.
    --report PATH       Write the sealed bundle to PATH and print only the absolute
                        path + digest (the bundle, which contains scores, is NOT
                        echoed to stdout). Without --report it prints to stdout.

Environment variables: none. Read-only, no network, no secrets.

TRUST MODEL (read this — it is not what it looks like):
    The bundle carries a SHA-256 over its own canonical payload. That digest is a
    CHANGE-DETECTION aid only — it is NOT a trust anchor. A bundle produced on a
    machine the third party controls can be edited and re-digested by that party, so
    a self-contained client cannot prove its own integrity.

    Integrity instead comes from RE-DERIVATION on trusted infrastructure: the bundle
    embeds the exact inputs (repo identity + git HEAD SHA), the raw signals, the
    per-dimension scores, and the weights, so the methodology owner can re-clone at
    that SHA, re-run the pipeline, and diff. verify.py does this. Tampering with the
    headline number or the signals is caught by the re-run; it does not depend on the
    client returning a correct digest.

    This bundle does NOT hide the methodology: a self-contained skill ships its
    weights in cleartext and they are readable in this environment. Hiding the weights
    would require server-side scoring, which this skill deliberately does not do.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import quality.scripts.sealing as sealing


def main() -> int:
    parser = argparse.ArgumentParser(description="Seal a repo-quality-score result for download.")
    parser.add_argument("result_json", help="score.py / aggregate_org.py output JSON.")
    parser.add_argument("--evidence", action="append", default=[],
                        help="raw-signal JSON (repo_stats / git_stats / per-repo bundle). Repeatable.")
    parser.add_argument("--as-of", help="generation timestamp (ISO-8601). Default: now.")
    parser.add_argument("--report", help="write the sealed bundle to this path.")
    args = parser.parse_args()

    result_path = Path(args.result_json)
    if not result_path.exists():
        print(json.dumps({"error": f"result not found: {result_path}"}))
        return 1
    result = sealing.unwrap_result(sealing.load_json(result_path))

    evidence: dict = {}
    for ev in args.evidence:
        p = Path(ev)
        if not p.exists():
            sys.stderr.write(f"warning: evidence not found, skipping: {p}\n")
            continue
        evidence[p.name] = sealing.load_json(p)

    bundle = sealing.build_bundle(result, evidence, args.as_of)
    text = json.dumps(bundle, indent=2)
    if args.report:
        out = Path(args.report).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        sys.stderr.write(f"\nSEALED RESULT FILE (download this and send it back): {out}\n")
        sys.stderr.write(f"  sha256: {bundle['integrity']['digest']}\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
