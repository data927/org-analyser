#!/usr/bin/env python3
"""
score.py — compute a class-aware, 0-100 quality score for a single repository
(single-class or monorepo), for the repo-quality-score skill.

Usage:
    python score.py <scores-json> --report PATH
                    [--evidence <stats-json> ...] [--as-of ISO] [--no-seal]

Positional arguments:
    scores-json     Path to the agent-produced scores JSON (or "-" for stdin).
                    Schema:
                      {
                        "repo_name": "...",
                        "repo_path": "...",
                        "total_loc": 12345,
                        "classes": [
                          {
                            "name": "backend",          # one of the 8 classes
                            "confidence": 0.7,           # optional, informational
                            "size_loc": 9000,            # optional; for monorepo weighting
                            "dimensions": {              # dim_score per applicable dim
                              "B": 70,                   #   number in [0,100], OR
                              "D": {"score": 80},        #   {"score": 0-100}, OR
                              "I": {"subs": {"I1": 8, "I2": 6}}  # 0-10 sub-scores, averaged x10
                            }
                          }
                        ],
                        "monorepo_bonus_per_extra_class": 2.0,   # optional override (points)
                        "monorepo_bonus_cap": 5.0,               # optional override (points)
                        "capacity_inputs": {                     # optional; enables
                          "confirmed_candidate_count": 42,       #   mining-rank. All
                          "analyzed_commits": 200,               #   three come from
                          "total_commits": 1800                  #   git_stats.py.
                        }
                      }

Options:
    --report PATH   Write the output to PATH and print only the path (scores are NOT
                    echoed to stdout). Without --report, JSON is printed to stdout
                    (for piping). In the skill flow, ALWAYS pass --report so scores
                    never appear in the terminal.
    --evidence PATH Raw-signal JSON (repo_stats.py / git_stats.py output) to embed in
                    the sealed bundle. Repeatable. Pass the repo's repo_stats and
                    git_stats so the methodology owner can re-derive (see verify.py).
    --as-of ISO     Generation timestamp recorded in the bundle (default: now).
    --no-seal       Write the RAW result instead of a sealed bundle (debug only).

Output: BY DEFAULT the result is written as a sealed bundle (see sealing.py /
verify.py / references/verification.md) — it embeds the result + the --evidence raw
signals + provenance + an integrity digest. Use --no-seal only for debugging.

Environment variables: none. Read-only, no network, no secrets.

Scoring model (all scores on a 0-100 scale):
  * Each class has a weight profile over the dimension catalog (see PROFILES). A
    dimension absent from a class's profile is "not applicable" (N/A) to that class
    and is never penalized — e.g. test coverage (B) is N/A for infra.
  * Per-class score (the breakdown value) = weighted average of the dimension scores
    over that class's applicable dimensions, weights renormalized to sum to 1.
  * Single-class repo: overall = the one class's score.
  * Monorepo (>= 2 classes): overall = size(LOC)-weighted average of each class's
    *catalog-completed* score, where dimensions N/A to a class are credited as 100
    at their default weight (so the infra component's lack of tests counts as full
    credit, not zero), plus a small monorepo bonus (default 2 points per extra class,
    capped at 5).
  * Grade: A >= 85, B >= 70, C >= 55, D >= 40, else F.

Task-mining rank (for ranking repos by training-data value, not for grading):
  * capacity = (confirmed_candidate_count / analyzed_commits) * total_commits — an
    estimate of how many mineable feature+test tasks the repo could yield, from
    git_stats.py, extrapolated to full history.
  * mining_rank = (overall_score / 100) * capacity — expected good-task yield: the
    quality fraction times the volume axis. Emitted only when capacity_inputs (or an
    explicit capacity) is provided.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import quality.scripts.sealing as sealing

# Dimension catalog: key -> human-readable name. B-K are the 8 shared-core
# dimensions (apply to most classes); M-P are class-specialist dimensions (apply to
# one class). Keys are non-contiguous by design — the catalog is exactly these 12.
# Three of the core dimensions are composite: D (Architecture & Robustness) spans
# layering/typing/modularity AND runtime robustness (logging, error tracking, health
# checks); F (Docs & Onboarding) spans documentation AND reproducibility/setup; K
# (History & Maintenance) spans commit discipline AND recency/contributors/releases.
DIMENSIONS: dict[str, str] = {
    "B": "Test Coverage",
    "C": "Code Cleanliness",
    "D": "Architecture & Robustness",
    "E": "Dependency Health",
    "F": "Docs & Onboarding",
    "H": "CI/CD Maturity",
    "I": "Security Hygiene",
    "K": "History & Maintenance",
    "M": "Experiment Reproducibility & Analysis",
    "N": "Pipeline & Data Quality",
    "O": "IaC Quality",
    "P": "Secrets & Threat Modeling",
}

# Per-class relative weight profiles. Weights are renormalized to sum to 1 at
# scoring time, so only the relative emphasis matters. A dimension OMITTED from a
# profile is N/A for that class (not considered; never penalized).
#
# NOTE: This dict is the authoritative source for the weights. references/
# class-weights.md mirrors it for human reference — keep the two in sync.
PROFILES: dict[str, dict[str, float]] = {
    "backend": {
        "D": 0.24, "B": 0.16, "I": 0.12, "F": 0.12, "C": 0.10, "K": 0.10,
        "E": 0.08, "H": 0.08,
    },
    "frontend": {
        "D": 0.21, "C": 0.17, "F": 0.14, "B": 0.12, "E": 0.10, "H": 0.10,
        "I": 0.08, "K": 0.08,
    },
    "fullstack": {
        "D": 0.23, "B": 0.15, "F": 0.14, "I": 0.12, "C": 0.11, "K": 0.09,
        "E": 0.08, "H": 0.08,
    },
    "ml": {
        "F": 0.22, "M": 0.18, "D": 0.13, "C": 0.12, "K": 0.09, "B": 0.08,
        "E": 0.08, "H": 0.07, "I": 0.03,
    },
    "ai_research": {
        "F": 0.28, "M": 0.24, "C": 0.10, "D": 0.10, "K": 0.10, "B": 0.06,
        "E": 0.06, "H": 0.04, "I": 0.02,
    },
    "data_engineering": {
        "D": 0.22, "N": 0.20, "F": 0.15, "B": 0.10, "C": 0.08, "E": 0.08,
        "H": 0.08, "I": 0.06, "K": 0.03,
    },
    "security": {
        "P": 0.22, "I": 0.18, "D": 0.13, "F": 0.11, "E": 0.10, "B": 0.08,
        "H": 0.08, "C": 0.06, "K": 0.04,
    },
    "infra": {
        # No B (test coverage): infra/IaC has no conventional unit tests, so it is
        # N/A and never penalized. Quality lives in O, F, H, I instead.
        "O": 0.22, "F": 0.21, "D": 0.15, "I": 0.14, "H": 0.12, "E": 0.08,
        "K": 0.04, "C": 0.04,
    },
}

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


def default_weights() -> dict[str, float]:
    """Default weight per dimension = mean of its weight across the classes that
    use it. Used to re-introduce N/A dimensions (credited 100) when rolling up a
    monorepo, so the credit is applied at a realistic magnitude."""
    sums: dict[str, float] = {d: 0.0 for d in DIMENSIONS}
    counts: dict[str, int] = {d: 0 for d in DIMENSIONS}
    for profile in PROFILES.values():
        for d, w in profile.items():
            sums[d] += w
            counts[d] += 1
    return {d: (sums[d] / counts[d]) if counts[d] else 0.0 for d in DIMENSIONS}


def resolve_dim_score(value) -> float:
    """Accept a number 0-100, {"score": 0-100}, or {"subs": {name: 0-10}} (0-10
    sub-scores, averaged then scaled to 0-100) and return a 0-100 dimension score."""
    if isinstance(value, (int, float)):
        return max(0.0, min(100.0, float(value)))
    if isinstance(value, dict):
        if "score" in value:
            return max(0.0, min(100.0, float(value["score"])))
        if "subs" in value and value["subs"]:
            subs = list(value["subs"].values())
            return max(0.0, min(100.0, (sum(float(s) for s in subs) / len(subs)) * 10.0))
    raise ValueError(f"unrecognized dimension score value: {value!r}")


def score_class(class_obj: dict, warnings: list[str]) -> dict:
    name = class_obj["name"]
    if name not in PROFILES:
        raise ValueError(f"unknown class '{name}'. Valid: {sorted(PROFILES)}")
    profile = PROFILES[name]
    provided = class_obj.get("dimensions", {}) or {}

    for d in provided:
        if d not in DIMENSIONS:
            warnings.append(f"[{name}] unknown dimension key '{d}' ignored.")
        elif d not in profile:
            warnings.append(
                f"[{name}] dimension '{d}' is N/A for this class — provided score ignored."
            )

    dim_scores: dict[str, float] = {}
    weight_sum = 0.0
    weighted = 0.0
    for d, w in profile.items():
        if d in provided:
            s = resolve_dim_score(provided[d])
        else:
            s = 0.0
            warnings.append(
                f"[{name}] applicable dimension '{d}' ({DIMENSIONS[d]}) has no score — "
                f"treated as 0.0 (no evidence)."
            )
        dim_scores[d] = round(s, 1)
        weighted += w * s
        weight_sum += w

    class_score = weighted / weight_sum if weight_sum else 0.0
    return {
        "name": name,
        "confidence": class_obj.get("confidence"),
        "size_loc": class_obj.get("size_loc"),
        "applicable_dimensions": sorted(profile),
        "dimension_scores": dim_scores,
        "class_score": round(class_score, 1),
        "grade": grade_for(class_score),
    }


def catalog_completed_score(
    class_obj: dict, union_dims: set[str], wdef: dict[str, float]
) -> float:
    """Score for a class over the union dimension set, crediting N/A dims as 100
    at their default weight. This is the per-class contribution to the monorepo
    overall (the "treat missing dimensions like tests-in-infra as full, not 0" rule)."""
    name = class_obj["name"]
    profile = PROFILES[name]
    provided = class_obj.get("dimensions", {}) or {}
    weighted = 0.0
    weight_sum = 0.0
    for d in union_dims:
        if d in profile:
            w = profile[d]
            s = resolve_dim_score(provided[d]) if d in provided else 0.0
        else:
            w = wdef[d]
            s = 100.0  # N/A for this class -> credited as full
        weighted += w * s
        weight_sum += w
    return weighted / weight_sum if weight_sum else 0.0


def compute_capacity(scores: dict, warnings: list[str]):
    """Estimate task-mining capacity for the repo: how many mineable training tasks
    it could yield. Proxy = atomic feature+test commits, extrapolated to full history:

        capacity = (confirmed_candidate_count / analyzed_commits) * total_commits

    Inputs come from git_stats.py and are passed in the scores JSON as
    `capacity_inputs: {confirmed_candidate_count, analyzed_commits, total_commits}`.
    A precomputed `capacity` (number) in the scores JSON overrides the estimate.

    Returns (capacity_or_None, basis_str, detail_dict).
    """
    if "capacity" in scores:
        return float(scores["capacity"]), "explicit_override", {}
    ci = scores.get("capacity_inputs")
    if not ci:
        return None, None, {}
    analyzed = int(ci.get("analyzed_commits") or 0)
    confirmed = float(ci.get("confirmed_candidate_count") or 0)
    total = int(ci.get("total_commits") or analyzed)
    if analyzed <= 0:
        warnings.append(
            "capacity_inputs.analyzed_commits is 0 — cannot estimate mining capacity."
        )
        return None, None, {}
    rate = confirmed / analyzed
    capacity = rate * total
    detail = {
        "confirmed_candidate_count": confirmed,
        "analyzed_commits": analyzed,
        "total_commits": total,
        "candidate_rate": round(rate, 4),
    }
    return capacity, "mineable_commits_extrapolated", detail


def compute(scores: dict) -> dict:
    warnings: list[str] = []
    classes = scores.get("classes", [])
    if not classes:
        raise ValueError("scores JSON has no 'classes'.")

    total_loc = scores.get("total_loc")
    per_class = [score_class(c, warnings) for c in classes]
    is_monorepo = len(classes) > 1

    result: dict = {
        "repo_name": scores.get("repo_name"),
        "repo_path": scores.get("repo_path"),
        "total_loc": total_loc,
        "is_monorepo": is_monorepo,
        "classes": per_class,
        "warnings": warnings,
    }

    if not is_monorepo:
        overall = per_class[0]["class_score"]
        result["monorepo_bonus"] = 0.0
    else:
        # Monorepo rollup.
        wdef = default_weights()
        union_dims: set[str] = set()
        for c in classes:
            union_dims |= set(PROFILES[c["name"]])

        sizes = []
        for c in classes:
            size = c.get("size_loc")
            if size is None:
                size = (total_loc / len(classes)) if total_loc else 1.0
            sizes.append(float(size))
        size_total = sum(sizes) or float(len(classes))

        overall_base = 0.0
        for c, size in zip(classes, sizes):
            cc = catalog_completed_score(c, union_dims, wdef)
            overall_base += (size / size_total) * cc
            for pc in per_class:
                if pc["name"] == c["name"]:
                    pc["catalog_completed_score"] = round(cc, 1)
                    pc["size_weight"] = round(size / size_total, 4)

        # Bonus and scores are on the 0-100 scale: default 2 points per extra class,
        # capped at 5 points; overall capped at 100.
        per_extra = scores.get("monorepo_bonus_per_extra_class", 2.0)
        bonus_cap = scores.get("monorepo_bonus_cap", 5.0)
        bonus = min(bonus_cap, per_extra * (len(classes) - 1))
        overall = min(100.0, overall_base + bonus)

        result["union_dimensions"] = sorted(union_dims)
        result["overall_base"] = round(overall_base, 1)
        result["monorepo_bonus"] = round(bonus, 1)

    result["overall_score"] = round(overall, 1)
    result["overall_grade"] = grade_for(overall)

    # Task-mining capacity and ranking score. Capacity is a repo-wide estimate of how
    # many mineable training tasks the repo could yield, computed once per repo (git
    # history is repo-wide). mining_rank = expected good-task yield = quality fraction
    # (overall/100) x capacity.
    capacity, basis, detail = compute_capacity(scores, warnings)
    if capacity is not None:
        result["capacity"] = round(capacity, 2)
        result["capacity_basis"] = basis
        result["capacity_detail"] = detail
        result["mining_rank"] = round((overall / 100.0) * capacity, 2)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute a class-aware 0-100 quality score for one repository."
    )
    parser.add_argument("scores_json", help="Path to scores JSON, or '-' for stdin.")
    parser.add_argument("--report", help="Write the output (sealed bundle by default) here.")
    parser.add_argument("--evidence", action="append", default=[],
                        help="raw-signal JSON to embed in the sealed bundle. Repeatable.")
    parser.add_argument("--as-of", help="generation timestamp recorded in the bundle.")
    parser.add_argument("--no-seal", action="store_true",
                        help="write the raw result instead of a sealed bundle (debug).")
    args = parser.parse_args()

    if args.scores_json == "-":
        scores = json.load(sys.stdin)
    else:
        path = Path(args.scores_json)
        if not path.exists():
            print(json.dumps({"error": f"path not found: {path}"}))
            return 1
        scores = json.loads(path.read_text(encoding="utf-8", errors="ignore"))

    result = compute(scores)

    # Seal by default: embed the result + raw-signal evidence + provenance + digest.
    if args.no_seal:
        output = result
    else:
        evidence: dict = {}
        for ev in args.evidence:
            p = Path(ev)
            if p.exists():
                evidence[p.name] = sealing.load_json(p)
            else:
                sys.stderr.write(f"warning: evidence not found, skipping: {p}\n")
        if not evidence:
            sys.stderr.write(
                "warning: no --evidence given; sealing with empty evidence "
                "(re-derivation will be limited). Pass repo_stats/git_stats JSONs.\n"
            )
        output = sealing.build_bundle(result, evidence, args.as_of)

    text = json.dumps(output, indent=2)
    # When writing to a file, do NOT echo to stdout — scores must not appear in the
    # terminal / tool output. Only the file path goes to stderr.
    if args.report:
        out = Path(args.report).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        sys.stderr.write(f"\nRESULT FILE (download this and send it back): {out}\n")
        if not args.no_seal:
            sys.stderr.write(f"  sha256: {output['integrity']['digest']}\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
