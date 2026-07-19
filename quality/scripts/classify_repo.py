#!/usr/bin/env python3
"""
classify_repo.py — assign a repository (or a sub-tree of a monorepo) to one or
more of the eight quality classes, for the repo-quality-score skill.

Usage:
    python classify_repo.py <repo-stats-json> [--git-stats <git-stats-json>]
                            [--threshold FLOAT]

Positional arguments:
    repo-stats-json     Path to a JSON file produced by repo_stats.py (use "-" to
                        read from stdin). The `class_signals` block is the input.

Options:
    --git-stats PATH    Optional path to a git_stats.py JSON file. Currently only
                        used to enrich the output metadata; classification is based
                        on the static tree signals.
    --threshold FLOAT   Minimum normalized confidence for a class to be "detected"
                        (default 0.18). A class also needs a minimum absolute signal
                        strength (raw >= 2.0) to be detected, so weak noise doesn't
                        register.

Environment variables: none. Read-only, no network, no secrets.

The eight classes: frontend, backend, fullstack, ml, ai_research,
data_engineering, security, infra.

Output (JSON to stdout):
    {
      "class_confidence": {class: 0-1, ...},     # normalized over atomic classes
      "raw_scores": {class: float, ...},
      "primary_class": "...",
      "suggested_classes": ["..."],              # after the fullstack-collapse rule
      "is_monorepo": bool,                        # >= 2 suggested classes
      "notes": ["..."]
    }

ml vs ai_research and the fullstack collapse are heuristic starting points. The
skill's agent confirms or overrides them, and for true monorepos re-runs
repo_stats.py / classify_repo.py per sub-directory (apps/*, packages/*, services/*)
to get a real per-component breakdown.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ATOMIC_CLASSES = [
    "frontend", "backend", "ml", "ai_research",
    "data_engineering", "security", "infra",
]

FRONTEND_FRAMEWORK_MARKERS = {
    "React", "Vue", "Vue CLI", "Angular", "SvelteKit", "Astro", "Gatsby",
    "Next.js", "Nuxt", "Remix", "Tailwind CSS",
}
BACKEND_FRAMEWORK_MARKERS = {
    "Express", "NestJS", "FastAPI", "Flask", "Django", "Fastify", "Hono",
    "Koa", "Maven (Java)", "Ruby (Bundler)", "WSGI (Flask/Django)",
    "ASGI (FastAPI/Django Channels)", "tRPC",
}


def capped(value: float, per: float, cap: float) -> float:
    """Linear contribution `value * per`, clamped to `cap`. Keeps any single
    high-count signal from dominating the classification."""
    return min(value * per, cap)


def compute_raw_scores(stats: dict) -> dict[str, float]:
    cs = stats.get("class_signals", {}) or {}
    hits = cs.get("dep_keyword_hits", {}) or {}
    frameworks = set(stats.get("detected_frameworks", []) or [])
    project_type = stats.get("project_type", "")

    def n(group: str) -> int:
        return len(hits.get(group, []))

    fe_markers = len(FRONTEND_FRAMEWORK_MARKERS & frameworks)
    be_markers = len(BACKEND_FRAMEWORK_MARKERS & frameworks)

    frontend = (
        capped(n("frontend_frameworks"), 1.5, 6.0)
        + capped(cs.get("ui_component_file_count", 0), 0.1, 5.0)
        + capped(cs.get("css_loc_ratio", 0.0) * 20.0, 1.0, 3.0)
        + capped(fe_markers, 1.0, 3.0)
    )

    backend = (
        capped(n("backend_frameworks"), 1.5, 6.0)
        + capped(n("orm_db"), 1.0, 4.0)
        + capped(be_markers, 1.0, 3.0)
        + (3.0 if project_type == "API service" else 0.0)
    )

    ml = (
        capped(n("ml_libs"), 2.0, 8.0)
        + capped(cs.get("notebook_count", 0), 0.3, 4.0)
        + capped(n("experiment_tracking"), 1.0, 4.0)
    )

    ai_research = (
        capped(n("experiment_tracking"), 2.0, 6.0)
        + capped(cs.get("notebook_count", 0), 0.5, 5.0)
        + capped(n("ml_libs"), 1.0, 4.0)
    )

    data_engineering = (
        capped(n("data_eng"), 2.5, 9.0)
        + capped(cs.get("sql_file_count", 0), 0.3, 4.0)
        + capped(cs.get("sql_loc", 0) / 200.0, 1.0, 3.0)
        + capped(cs.get("data_file_count", 0), 0.5, 2.0)
    )

    security = capped(n("security_libs"), 3.0, 10.0)

    infra = (
        (4.0 if cs.get("terraform_present") else 0.0)
        + capped(cs.get("terraform_file_count", 0), 0.3, 4.0)
        + capped(cs.get("k8s_manifest_count", 0), 0.5, 4.0)
        + (2.0 if cs.get("helm_present") else 0.0)
        + (2.0 if cs.get("pulumi_present") else 0.0)
        + (2.0 if cs.get("ansible_present") else 0.0)
        + capped(n("infra_libs"), 1.0, 3.0)
        + capped(cs.get("dockerfile_count", 0), 0.5, 1.5)
    )

    return {
        "frontend": round(frontend, 3),
        "backend": round(backend, 3),
        "ml": round(ml, 3),
        "ai_research": round(ai_research, 3),
        "data_engineering": round(data_engineering, 3),
        "security": round(security, 3),
        "infra": round(infra, 3),
    }


def classify(stats: dict, threshold: float) -> dict:
    raw = compute_raw_scores(stats)
    total = sum(raw.values())
    notes: list[str] = []

    if total <= 0:
        confidence = {c: 0.0 for c in ATOMIC_CLASSES}
        primary = "backend"
        notes.append(
            "No strong class signals found — likely a general-purpose library/CLI. "
            "Defaulted primary to 'backend' (general code). Agent should confirm."
        )
        suggested = ["backend"]
        return {
            "class_confidence": confidence,
            "raw_scores": raw,
            "primary_class": primary,
            "suggested_classes": suggested,
            "is_monorepo": False,
            "notes": notes,
        }

    confidence = {c: round(v / total, 4) for c, v in raw.items()}
    primary = max(raw, key=lambda c: raw[c])

    # Detected = normalized confidence over threshold AND meaningful raw strength.
    detected = [
        c for c in ATOMIC_CLASSES
        if confidence[c] >= threshold and raw[c] >= 2.0
    ]
    if not detected:
        detected = [primary]

    # Fullstack-collapse rule: a repo with strong frontend AND backend and nothing
    # else is one full-stack app, not a two-component monorepo.
    suggested = list(detected)
    if set(detected) == {"frontend", "backend"}:
        suggested = ["fullstack"]
        notes.append(
            "Strong frontend + backend signals with no other class — collapsed to "
            "a single 'fullstack' class."
        )
    elif {"frontend", "backend"}.issubset(set(detected)):
        notes.append(
            "Frontend + backend both present alongside other classes — treated as a "
            "monorepo. Re-run per sub-directory for a true component breakdown."
        )

    if "ml" in detected and "ai_research" in detected:
        notes.append(
            "Both ml and ai_research signals present — these overlap. Pick one as the "
            "dominant class per component based on whether the emphasis is "
            "production/serving (ml) or experiments/reproduction (ai_research)."
        )

    is_monorepo = len(suggested) >= 2

    return {
        "class_confidence": confidence,
        "raw_scores": raw,
        "primary_class": primary,
        "suggested_classes": suggested,
        "is_monorepo": is_monorepo,
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify a repo into one or more quality classes."
    )
    parser.add_argument(
        "repo_stats_json",
        help="Path to repo_stats.py JSON output, or '-' for stdin.",
    )
    parser.add_argument("--git-stats", help="Optional git_stats.py JSON path.")
    parser.add_argument("--threshold", type=float, default=0.18)
    args = parser.parse_args()

    if args.repo_stats_json == "-":
        stats = json.load(sys.stdin)
    else:
        path = Path(args.repo_stats_json)
        if not path.exists():
            print(json.dumps({"error": f"path not found: {path}"}))
            return 1
        stats = json.loads(path.read_text(encoding="utf-8", errors="ignore"))

    result = classify(stats, args.threshold)
    result["repo_name"] = stats.get("repo_name")
    result["repo_path"] = stats.get("repo_path")
    result["primary_language"] = stats.get("primary_language")
    result["total_loc"] = stats.get("total_loc")

    if args.git_stats:
        gpath = Path(args.git_stats)
        if gpath.exists():
            result["git_stats_path"] = str(gpath)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
