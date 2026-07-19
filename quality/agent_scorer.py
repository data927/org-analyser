#!/usr/bin/env python3
"""
Agent rubric scorer — evidence-based dimension scoring with scoring_notes.md.
Used by the agent pipeline (NOT signal_scorer.py from the heuristic batch).
"""

from __future__ import annotations

import re

from quality.signal_scorer import (
    CORE,
    SCORERS,
    SPECIALIST,
    _clamp,
    _parse_test_ratio,
    score_b,
    score_c,
    score_d,
    score_e,
    score_f,
    score_h,
    score_i,
    score_k,
    score_m,
)


def score_n_enhanced(stats: dict) -> tuple[float, str]:
    cs = stats.get("class_signals") or {}
    sql_loc = cs.get("sql_loc") or 0
    sql_files = cs.get("sql_file_count") or 0
    docker = bool(stats.get("has_dockerfile") or stats.get("has_docker_compose"))
    specs = stats.get("test_spec_files") or 0
    base, ev = SCORERS["N"](stats) if "N" in SCORERS else (35.0, "")
    score = base
    if sql_files:
        score += min(20, sql_files * 2)
    if docker:
        score += 10
    if specs >= 10:
        score += 10
    return _clamp(score), (
        f"{ev}; sql_files={sql_files}, sql_loc={sql_loc}, docker={docker}, specs={specs}"
    )


def score_o_enhanced(stats: dict) -> tuple[float, str]:
    cs = stats.get("class_signals") or {}
    tf = cs.get("terraform_file_count") or 0
    k8s = cs.get("k8s_manifest_count") or 0
    helm = bool(cs.get("helm_present"))
    ansible = bool(cs.get("ansible_present"))
    score = 25.0
    if cs.get("terraform_present"):
        score += 15
    if tf > 50:
        score += 15
    elif tf > 10:
        score += 10
    if k8s:
        score += min(20, k8s // 10 + 5)
    if helm:
        score += 8
    if ansible:
        score += 5
    g1000 = stats.get("god_files_over_1000_loc") or 0
    score -= min(15, g1000 // 50)
    return _clamp(score), (
        f"terraform_files={tf}, k8s_manifests={k8s}, helm={helm}, "
        f"ansible={ansible}, god>1000={g1000}"
    )


def score_b_adjusted(stats: dict) -> tuple[float, str]:
    loc = stats.get("total_loc") or 0
    if loc == 0:
        return 5.0, "empty repo — no source to test"
    sc, ev = score_b(stats)
    return sc, ev


def score_k_adjusted(stats: dict, git: dict) -> tuple[float, str]:
    sc, ev = score_k(git)
    if (stats.get("total_loc") or 0) == 0:
        sc = min(sc, 15.0)
    return sc, ev


ENHANCED = {
    **SCORERS,
    "B": score_b_adjusted,
    "N": score_n_enhanced,
    "O": score_o_enhanced,
    "K": lambda s, g: score_k_adjusted(s, g),
}


def _monorepo_sizes(classify: dict, total_loc: int) -> dict[str, int]:
    raw = classify.get("raw_scores") or {}
    classes = classify.get("suggested_classes") or [classify.get("primary_class", "backend")]
    if not classify.get("is_monorepo") or len(classes) <= 1:
        primary = classify.get("primary_class", classes[0] if classes else "backend")
        return {primary: total_loc}
    total_raw = sum(raw.get(c, 0) for c in classes) or 1
    sizes = {}
    for cls in classes:
        sizes[cls] = max(1, int(total_loc * (raw.get(cls, 0) / total_raw)))
    return sizes


def build_scores_json(
    repo_stats: dict,
    git_stats: dict,
    classify: dict,
    repo_path: str,
) -> dict:
    git_repo = git_stats.get("repo_stats") or git_stats
    classes = classify.get("suggested_classes") or [classify.get("primary_class", "backend")]
    if not classes:
        classes = ["backend"]
    total_loc = repo_stats.get("total_loc") or 0
    size_map = _monorepo_sizes(classify, total_loc)

    class_entries = []
    all_evidence: list[dict] = []
    for cls in classes:
        dims: dict = {}
        evidence: dict = {}
        for dim in CORE:
            if dim == "K":
                sc, ev = ENHANCED[dim](repo_stats, git_repo)
            else:
                sc, ev = ENHANCED[dim](repo_stats)
            dims[dim] = round(sc, 1)
            evidence[dim] = ev
        for dim in SPECIALIST.get(cls, []):
            sc, ev = ENHANCED[dim](repo_stats)
            dims[dim] = round(sc, 1)
            evidence[dim] = ev
        all_evidence.append({"class": cls, "dimensions": evidence})
        class_entries.append({
            "name": cls,
            "confidence": (classify.get("class_confidence") or {}).get(cls),
            "size_loc": size_map.get(cls, total_loc),
            "dimensions": dims,
        })

    return {
        "repo_name": repo_stats.get("repo_name") or Path(repo_path).name,
        "repo_path": repo_path,
        "total_loc": total_loc,
        "classes": class_entries,
        "capacity_inputs": {
            "confirmed_candidate_count": git_stats.get("confirmed_candidate_count") or 0,
            "analyzed_commits": git_stats.get("analyzed_commits") or 0,
            "total_commits": git_repo.get("total_commits") or 0,
        },
        "_evidence_meta": all_evidence,
        "_classify_notes": classify.get("notes") or [],
    }


def write_scoring_notes(work_dir: Path, scores: dict) -> None:
    lines = [f"# {scores['repo_name']}", ""]
    for note in scores.get("_classify_notes") or []:
        lines.append(f"- classify: {note}")
    for block in scores.get("_evidence_meta") or []:
        lines.append(f"\n## Class: {block['class']}")
        for dim, ev in sorted(block.get("dimensions", {}).items()):
            sc = next(
                (c["dimensions"][dim] for c in scores["classes"] if c["name"] == block["class"]),
                "?",
            )
            lines.append(f"- **{dim}** ({sc}): {ev}")
    (work_dir / "scoring_notes.md").write_text("\n".join(lines) + "\n")


def scores_for_seal(scores: dict) -> dict:
    out = {k: v for k, v in scores.items() if not k.startswith("_")}
    return out
