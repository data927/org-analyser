#!/usr/bin/env python3
"""
verify.py — re-derive and tamper-check a sealed repo-quality-score bundle, for the
methodology owner to run on TRUSTED infrastructure (not in the third-party env).

Usage:
    python verify.py <sealed-bundle.json> [--repo <path>] [--tolerance F]

Positional arguments:
    sealed-bundle.json  A bundle produced by seal.py and returned by the third party.

Options:
    --repo PATH         Path to a trusted checkout of the scored repository (re-clone
                        at the bundle's recorded head_sha first). When given, the
                        verifier re-runs repo_stats.py / git_stats.py here and diffs
                        the re-collected signals against the bundle's embedded signals.
                        Without it, only the digest, tool-version, and score-math
                        checks run.
    --tolerance F       Absolute tolerance for float comparisons (default 0.5).

Environment variables: none. Read-only, no network, no secrets.

WHY THIS IS THE TRUST ANCHOR:
    A self-contained client cannot prove its own integrity (it controls both the file
    and any digest it emits). So verification does not trust the returned digest — it
    RE-DERIVES: re-runs the deterministic collectors on a trusted checkout at the
    recorded commit and diffs, and recomputes the weighted score from the embedded
    per-dimension scores using THIS copy of the weights. Tampering with the headline
    number (score math) or the raw signals (re-collection diff) is caught here.

    What re-derivation cannot fully check: the agent-assigned dimension scores are
    judgment, not deterministic. Their plausibility is checked by spot-reading the
    embedded evidence strings and by re-scoring with a trusted agent run.

Exit code: 0 if all automatic checks PASS, 1 if any FAIL.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import quality.scripts.sealing as sealing

SCRIPTS_DIR = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("score", SCRIPTS_DIR / "score.py")
score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score)

# Stable (deterministic) signals to diff. recency_days is intentionally excluded
# (it depends on wall-clock at collection time).
STABLE_REPO_FIELDS = [
    "primary_language", "total_loc", "total_source_files", "test_spec_files",
    "god_files_over_500_loc", "god_files_over_1000_loc", "median_file_size_loc",
    "has_lint_config", "ci_present", "ci_runs_tests", "ci_runs_lint", "ci_has_deploy",
    "direct_runtime_deps", "direct_dev_deps", "hardcoded_secret_hits",
    "dep_audit_in_ci", "has_dockerfile", "has_docker_compose",
    "has_health_endpoint", "has_metrics",
]
STABLE_GIT_FIELDS = [
    "head_sha", "total_commits", "human_authors", "bot_commit_ratio",
    "conventional_rate_last_200", "tag_count", "first_commit", "last_commit",
]


def run_json(cmd: list[str]):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(p.stdout)


def find_evidence(bundle: dict):
    repo_stats = git_stats = None
    for obj in bundle.get("evidence", {}).values():
        if isinstance(obj, dict) and "total_loc" in obj and "class_signals" in obj:
            repo_stats = repo_stats or obj
        if isinstance(obj, dict) and "repo_stats" in obj and "confirmed_candidate_count" in obj:
            git_stats = git_stats or obj
    return repo_stats, git_stats


def cmp_fields(label, embedded, fresh, fields, tol, checks):
    for f in fields:
        a = embedded.get(f)
        b = fresh.get(f)
        ok = (abs(a - b) <= tol) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else (a == b)
        checks.append((f"{label}.{f}", "PASS" if ok else "FAIL",
                       "" if ok else f"embedded={a!r} re-collected={b!r}"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-derive and tamper-check a sealed bundle.")
    parser.add_argument("bundle")
    parser.add_argument("--repo")
    parser.add_argument("--tolerance", type=float, default=0.5)
    args = parser.parse_args()

    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8", errors="ignore"))
    checks: list[tuple[str, str, str]] = []

    # 1. Integrity digest (change-detection): recompute over payload minus integrity.
    stated = bundle.get("integrity", {}).get("digest")
    recomputed = sealing.recompute_digest(bundle)
    checks.append(("integrity.digest",
                   "PASS" if stated == recomputed else "FAIL",
                   "" if stated == recomputed else f"stated={stated} recomputed={recomputed}"))

    # 2. Tool-version: did the client run unmodified scripts (vs our known-good copy)?
    ours = sealing.tool_version()["scripts_digest"]
    theirs = bundle.get("tool_version", {}).get("scripts_digest")
    checks.append(("tool_version.scripts_digest",
                   "PASS" if ours == theirs else "WARN",
                   "" if ours == theirs else f"ours={ours[:12]}.. bundle={str(theirs)[:12]}.. (client may have modified scripts, or version differs)"))

    result = bundle.get("result", {})
    repo_stats_ev, git_stats_ev = find_evidence(bundle)

    # 3. Re-collection diff (needs a trusted checkout).
    if args.repo:
        if repo_stats_ev:
            fresh_rs = run_json(["python3", str(SCRIPTS_DIR / "repo_stats.py"), args.repo])
            cmp_fields("repo_stats", repo_stats_ev, fresh_rs, STABLE_REPO_FIELDS, args.tolerance, checks)
            # class signals (scalars + keyword-hit sets)
            ecs = (repo_stats_ev.get("class_signals") or {})
            fcs = (fresh_rs.get("class_signals") or {})
            for f in ["terraform_file_count", "k8s_manifest_count", "notebook_count",
                      "ui_component_file_count", "sql_file_count"]:
                ok = ecs.get(f) == fcs.get(f)
                checks.append((f"class_signals.{f}", "PASS" if ok else "FAIL",
                               "" if ok else f"embedded={ecs.get(f)} re-collected={fcs.get(f)}"))
            eh = {k: sorted(v) for k, v in (ecs.get("dep_keyword_hits") or {}).items()}
            fh = {k: sorted(v) for k, v in (fcs.get("dep_keyword_hits") or {}).items()}
            checks.append(("class_signals.dep_keyword_hits", "PASS" if eh == fh else "FAIL",
                           "" if eh == fh else "keyword hits differ"))
            e_env = bool(repo_stats_ev.get("env_files_committed"))
            f_env = bool(fresh_rs.get("env_files_committed"))
            checks.append(("repo_stats.env_files_committed(bool)", "PASS" if e_env == f_env else "FAIL",
                           "" if e_env == f_env else f"embedded={e_env} re-collected={f_env}"))
        if git_stats_ev:
            fresh_gs = run_json(["python3", str(SCRIPTS_DIR / "git_stats.py"), args.repo])
            cmp_fields("git_stats", git_stats_ev["repo_stats"], fresh_gs["repo_stats"],
                       STABLE_GIT_FIELDS, args.tolerance, checks)
            for f in ["confirmed_candidate_count", "analyzed_commits"]:
                ok = abs((git_stats_ev.get(f) or 0) - (fresh_gs.get(f) or 0)) <= args.tolerance
                checks.append((f"git_stats.{f}", "PASS" if ok else "FAIL",
                               "" if ok else f"embedded={git_stats_ev.get(f)} re-collected={fresh_gs.get(f)}"))
    else:
        checks.append(("re_collection", "SKIP", "no --repo given; signal re-derivation not run"))

    # 4. Score math: recompute overall from the embedded per-dimension scores.
    if "classes" in result and "overall_score" in result:
        sc = {"total_loc": result.get("total_loc"),
              "classes": [{"name": c["name"], "size_loc": c.get("size_loc"),
                           "dimensions": c.get("dimension_scores", {})} for c in result["classes"]]}
        gi = (git_stats_ev or {})
        if gi:
            sc["capacity_inputs"] = {
                "confirmed_candidate_count": gi.get("confirmed_candidate_count"),
                "analyzed_commits": gi.get("analyzed_commits"),
                "total_commits": gi.get("repo_stats", {}).get("total_commits"),
            }
        recomputed_res = score.compute(sc)
        ok = abs(recomputed_res["overall_score"] - result["overall_score"]) <= args.tolerance
        checks.append(("score_math.overall_score", "PASS" if ok else "FAIL",
                       "" if ok else f"stated={result['overall_score']} recomputed={recomputed_res['overall_score']}"))
    elif "org_score" in result:
        repos = result.get("repos", [])
        wtot = sum((r.get("total_loc") or 1) for r in repos) or 1
        recomputed_org = round(sum(r["overall_score"] * (r.get("total_loc") or 1) for r in repos) / wtot, 1)
        ok = abs(recomputed_org - result["org_score"]) <= args.tolerance
        checks.append(("score_math.org_score", "PASS" if ok else "FAIL",
                       "" if ok else f"stated={result['org_score']} recomputed={recomputed_org}"))

    failed = [c for c in checks if c[1] == "FAIL"]
    print(f"{'CHECK':<42}{'RESULT':<7}DETAIL")
    print("-" * 80)
    for name, res, detail in checks:
        print(f"{name:<42}{res:<7}{detail}")
    print("-" * 80)
    verdict = "TAMPER DETECTED" if failed else "VERIFIED (all automatic checks passed)"
    print(f"VERDICT: {verdict}  ({len(failed)} FAIL, "
          f"{sum(1 for c in checks if c[1]=='WARN')} WARN, "
          f"{sum(1 for c in checks if c[1]=='PASS')} PASS)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
