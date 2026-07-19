#!/usr/bin/env python3
"""
aggregate_org.py — roll up per-repo quality scores into a single organization
score plus a distribution and class mix, for the repo-quality-score skill.

Usage:
    python aggregate_org.py <score-json-or-dir> [<score-json-or-dir> ...]
                            --report PATH [--as-of ISO] [--no-seal]

Positional arguments:
    score-json-or-dir   One or more paths. Each is a per-repo score.py output — a
                        sealed bundle (the default) or a raw result — or a directory
                        (every *.json directly inside it is read). Sealed inputs are
                        unwrapped for aggregation. Repos de-dup by repo_name (first wins).

Options:
    --report PATH       Write the output (sealed bundle by default) to PATH and print
                        only the path (scores are NOT echoed to stdout). Without
                        --report it prints to stdout. In the skill flow, ALWAYS pass
                        --report.
    --as-of ISO         Generation timestamp recorded in the bundle (default: now).
    --no-seal           Write the raw org rollup instead of a sealed bundle (debug).

Output: BY DEFAULT the org rollup is written as a sealed bundle that embeds every
per-repo input (sealed bundle or raw) as evidence, so the whole org result is one
re-derivable, tamper-evident artifact (see sealing.py / verify.py).

Environment variables: none. Read-only, no network, no secrets.

Org score model:
  * Headline org_score = size(LOC)-weighted mean of per-repo overall_score. A repo
    with no total_loc is weighted as 1. This reflects where the code actually lives.
  * Also reported: simple (unweighted) mean, and the distribution (min / max /
    median / population stdev / count).
  * Class mix: how many repos and how much LOC fall under each detected class.
  * Grade uses the same thresholds as a single repo (A >= 85 ... F < 40). Scores are
    on a 0-100 scale, matching score.py.

Task-mining ranking (when repos carried capacity through scoring):
  * mining_ranked: repos ordered by mining_rank = overall_score * capacity, i.e. by
    expected training-task value (quality x how many mineable tasks the repo holds).
  * total_expected_task_yield: sum of mining_rank across repos — the org's overall
    expected good-task yield. This is the headline number for ranking where to mine.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import quality.scripts.sealing as sealing

GRADE_THRESHOLDS = [
    (85.0, "A"),
    (70.0, "B"),
    (55.0, "C"),
    (40.0, "D"),
    (0.0, "F"),
]


def grade_for(score: float) -> str:
    for threshold, letter in GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return "F"


def collect_score_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        elif p.exists():
            files.append(p)
        else:
            sys.stderr.write(f"warning: path not found, skipping: {p}\n")
    return files


def load_repo_scores(files: list[Path]) -> tuple[list[dict], dict]:
    """Return (results, evidence). Inputs may be raw score.py results or sealed
    bundles (the default); sealed inputs are unwrapped for aggregation and embedded
    whole as evidence so the org bundle carries each repo's re-derivation chain."""
    seen: set[str] = set()
    repos: list[dict] = []
    evidence: dict = {}
    for f in files:
        try:
            data = sealing.load_json(f)
        except (json.JSONDecodeError, ValueError):
            sys.stderr.write(f"warning: not valid JSON, skipping: {f}\n")
            continue
        result = sealing.unwrap_result(data)
        if "overall_score" not in result:
            sys.stderr.write(f"warning: no overall_score, skipping: {f}\n")
            continue
        name = result.get("repo_name") or str(f)
        if name in seen:
            continue
        seen.add(name)
        repos.append(result)
        evidence[name] = data
    return repos, evidence


def aggregate(repos: list[dict]) -> dict:
    if not repos:
        return {"error": "no valid per-repo score files found"}

    rows = []
    for r in repos:
        loc = r.get("total_loc") or 0
        rows.append({
            "repo_name": r.get("repo_name"),
            "overall_score": float(r.get("overall_score", 0.0)),
            "overall_grade": r.get("overall_grade") or grade_for(float(r.get("overall_score", 0.0))),
            "total_loc": loc,
            "is_monorepo": bool(r.get("is_monorepo")),
            "classes": [c.get("name") for c in r.get("classes", [])],
            "capacity": r.get("capacity"),
            "mining_rank": r.get("mining_rank"),
        })

    scores = [row["overall_score"] for row in rows]
    weights = [row["total_loc"] if row["total_loc"] else 1 for row in rows]
    weight_total = sum(weights)
    org_score = sum(s * w for s, w in zip(scores, weights)) / weight_total

    # Class mix: repo count and LOC per class.
    class_repo_count: dict[str, int] = {}
    class_loc: dict[str, int] = {}
    for row in rows:
        for cname in row["classes"]:
            class_repo_count[cname] = class_repo_count.get(cname, 0) + 1
            class_loc[cname] = class_loc.get(cname, 0) + (row["total_loc"] or 0)

    ranked = sorted(rows, key=lambda x: x["overall_score"], reverse=True)

    # Task-mining ranking: repos ordered by mining_rank (quality x capacity), and the
    # org's total expected good-task yield = sum of mining_rank. Only repos that
    # carried capacity_inputs through scoring have a mining_rank.
    mining_rows = [row for row in rows if row.get("mining_rank") is not None]
    mining_ranked = sorted(mining_rows, key=lambda x: x["mining_rank"], reverse=True)
    total_expected_yield = round(sum(row["mining_rank"] for row in mining_rows), 2)

    return {
        "org_score": round(org_score, 4),
        "org_grade": grade_for(org_score),
        "repo_count": len(rows),
        "monorepo_count": sum(1 for row in rows if row["is_monorepo"]),
        "total_loc": sum(row["total_loc"] for row in rows),
        "total_expected_task_yield": total_expected_yield,
        "repos_with_capacity": len(mining_rows),
        "mining_ranked": [
            {"repo_name": row["repo_name"], "mining_rank": row["mining_rank"],
             "overall_score": row["overall_score"], "capacity": row["capacity"]}
            for row in mining_ranked
        ],
        "distribution": {
            "simple_mean": round(statistics.fmean(scores), 4),
            "size_weighted_mean": round(org_score, 4),
            "min": round(min(scores), 4),
            "max": round(max(scores), 4),
            "median": round(statistics.median(scores), 4),
            "stdev": round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0,
        },
        "class_mix": {
            "repo_count_by_class": dict(sorted(class_repo_count.items(), key=lambda x: -x[1])),
            "loc_by_class": dict(sorted(class_loc.items(), key=lambda x: -x[1])),
        },
        "best_repos": ranked[:5],
        "worst_repos": ranked[-5:][::-1],
        "repos": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate per-repo quality scores into an org score."
    )
    parser.add_argument(
        "paths", nargs="+",
        help="per-repo score.py outputs (sealed bundles or raw) and/or dirs of them.",
    )
    parser.add_argument("--report", help="Write the output (sealed bundle by default) here.")
    parser.add_argument("--as-of", help="generation timestamp recorded in the bundle.")
    parser.add_argument("--no-seal", action="store_true",
                        help="write the raw org rollup instead of a sealed bundle (debug).")
    args = parser.parse_args()

    files = collect_score_files(args.paths)
    repos, evidence = load_repo_scores(files)
    result = aggregate(repos)

    # Seal by default: embed each per-repo input (sealed bundle or raw result) as
    # evidence so the whole org result is one re-derivable, tamper-evident artifact.
    if args.no_seal or "error" in result:
        output = result
    else:
        output = sealing.build_bundle(result, evidence, args.as_of)

    text = json.dumps(output, indent=2)
    # When writing to a file, do NOT echo to stdout — scores must not appear in the
    # terminal / tool output. Only the file path goes to stderr.
    if args.report:
        out = Path(args.report).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        sys.stderr.write(f"\nRESULT FILE (download this and send it back): {out}\n")
        if not args.no_seal and "error" not in result:
            sys.stderr.write(f"  sha256: {output['integrity']['digest']}\n")
    else:
        print(text)
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
