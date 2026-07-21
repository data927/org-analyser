#!/usr/bin/env python3
"""Heuristic dimension scorer from repo_stats + git_stats signals."""

from __future__ import annotations

import re


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _parse_test_ratio(ratio: str) -> float | None:
    if not ratio or ratio == "0 tests":
        return None
    m = re.match(r"1:(\d+)", ratio)
    return float(m.group(1)) if m else None


def score_b(stats: dict) -> tuple[float, str]:
    specs = stats.get("test_spec_files") or 0
    ratio = _parse_test_ratio(stats.get("test_source_ratio", ""))
    cov = bool(stats.get("coverage_tooling"))
    threshold = bool(stats.get("coverage_threshold"))
    ci_tests = bool(stats.get("ci_runs_tests"))
    fw = stats.get("test_framework") or []

    # Binary floor per dimension-catalog.md: "no framework, or <5 specs, ratio
    # <1:50" all map to ~0 regardless of coverage tooling/CI — those only earn
    # credit once there's a real test base to run them on.
    if specs < 5 or (ratio is not None and ratio > 50):
        return 5.0, (
            f"{specs} spec files, ratio={stats.get('test_source_ratio')} "
            f"(below rubric floor: <5 specs or ratio <1:50)"
        )
    score = 20.0
    if ratio is not None:
        if ratio <= 3:
            score += 40
        elif ratio <= 10:
            score += 25
        elif ratio <= 30:
            score += 10
        else:
            score += 0
    if cov:
        score += 15
    if threshold:
        score += 10
    if ci_tests:
        score += 15
    return _clamp(score), (
        f"{specs} spec files, ratio={stats.get('test_source_ratio')}, "
        f"coverage={cov}, ci_tests={ci_tests}"
    )


def _sample(value, n: int = 3):
    if isinstance(value, dict):
        return list(value.keys())[:n]
    if isinstance(value, (list, tuple)):
        return list(value)[:n]
    return value


def score_c(stats: dict) -> tuple[float, str]:
    linters = stats.get("linters_and_formatters") or {}
    g500 = stats.get("god_files_over_500_loc") or 0
    g1000 = stats.get("god_files_over_1000_loc") or 0
    ci_lint = bool(stats.get("ci_runs_lint"))
    median = stats.get("median_file_size_loc") or 0

    score = 15.0
    if linters:
        score += 25
    if ci_lint:
        score += 20
    score -= min(30, g1000 * 10 + g500 * 3)
    if median and median < 200:
        score += 10
    return _clamp(score), (
        f"linters={_sample(linters)}, god>500={g500}, god>1000={g1000}, ci_lint={ci_lint}"
    )


def score_d(stats: dict) -> tuple[float, str]:
    logging = stats.get("logging_framework") or []
    err = stats.get("error_tracking") or []
    health = bool(stats.get("has_health_endpoint"))
    metrics = bool(stats.get("has_metrics"))
    ptype = stats.get("project_type") or "unknown"
    fw = stats.get("detected_frameworks") or []

    # Base kept low: the 0-anchor is "no evidence of layering/typing/robustness
    # at all," so absence of every signal below should land near the floor,
    # not a generous midpoint the old 35 base gave every repo unconditionally.
    score = 15.0
    if logging:
        score += 20
    if err:
        score += 20
    if health:
        score += 15
    if metrics:
        score += 10
    if fw:
        score += 20
    if ptype in ("library", "cli"):
        score = max(score, 45.0)
    return _clamp(score), (
        f"type={ptype}, logging={logging}, error_tracking={err}, "
        f"health={health}, metrics={metrics}"
    )


def score_e(stats: dict) -> tuple[float, str]:
    lock_found = len(stats.get("lockfiles_found") or [])
    lock_exp = len(stats.get("lockfiles_expected") or [])
    update = stats.get("dep_update_tooling") or []
    # repo_stats.py emits the literal string "none" (not None/empty) when no
    # update tooling is detected — bool("none") is True, so a naive truthiness
    # check here would credit every repo with update tooling it doesn't have.
    has_update = bool(update) and update != "none"
    runtime = stats.get("direct_runtime_deps") or 0

    score = 20.0
    if lock_exp:
        score += 40 * (lock_found / lock_exp)
    elif lock_found:
        score += 25
    if has_update:
        score += 15
    if runtime and runtime < 80:
        score += 10
    return _clamp(score), (
        f"lockfiles {lock_found}/{lock_exp or '?'}, runtime_deps={runtime}, "
        f"update_tooling={update}"
    )


def score_f(stats: dict) -> tuple[float, str]:
    readme_loc = stats.get("readme_loc") or 0
    sections = stats.get("readme_sections") or []
    changelog = bool(stats.get("changelog"))
    contrib = bool(stats.get("contributing_guide"))
    docker = bool(stats.get("has_dockerfile") or stats.get("has_docker_compose"))
    devc = bool(stats.get("has_devcontainer"))
    env_ex = bool(stats.get("env_example_file"))
    missing_env = len(stats.get("env_vars_missing_from_example") or [])

    score = 10.0
    if readme_loc > 50:
        score += 15
    if readme_loc > 200:
        score += 15
    score += min(15, len(sections) * 3)
    if changelog:
        score += 10
    if contrib:
        score += 5
    if docker or devc:
        score += 15
    if env_ex:
        score += 10
    score -= min(20, missing_env * 2)
    return _clamp(score), (
        f"readme_loc={readme_loc}, sections={len(sections)}, docker={docker}, "
        f"env_example={env_ex}, missing_env={missing_env}"
    )


def score_h(stats: dict) -> tuple[float, str]:
    if not stats.get("ci_present"):
        return 5.0, "no CI config detected"
    score = 25.0
    if stats.get("ci_runs_lint"):
        score += 15
    if stats.get("ci_runs_typecheck"):
        score += 15
    if stats.get("ci_runs_tests"):
        score += 25
    if stats.get("ci_has_deploy"):
        score += 20
    systems = stats.get("ci_systems") or []
    return _clamp(score), f"ci={systems}, lint/type/test/deploy flags from repo_stats"


def score_i(stats: dict) -> tuple[float, str]:
    secrets = stats.get("hardcoded_secret_hits") or 0
    env_committed = stats.get("env_files_committed") or []
    audit = bool(stats.get("dep_audit_in_ci"))
    validation = stats.get("input_validation_patterns") or []
    val_n = len(validation) if isinstance(validation, (list, tuple)) else int(validation or 0)

    # Binary cliff per dimension-catalog.md: any real secret in source, or a
    # committed .env, floors the score to 0 regardless of validation/audit —
    # those only matter once the repo has none.
    if secrets or env_committed:
        score = 0.0
    else:
        score = 50.0
        score += min(35, val_n * 7)
        if audit:
            score += 15
    return _clamp(score), (
        f"secret_hits={secrets}, env_committed={len(env_committed)}, "
        f"dep_audit={audit}, validation_patterns={val_n}"
    )


def score_k(git: dict) -> tuple[float, str]:
    recency = git.get("recency_days")
    authors = git.get("human_authors") or 0
    tags = git.get("semver_tag_count") or 0
    conv = git.get("conventional_rate_last_200") or 0
    bot = git.get("bot_commit_ratio") or 0

    # Recency is the rubric's lead signal at every anchor ("active in last 3
    # months" / "6-18 months" / ">2 years ago"), so it sets the base tier
    # rather than a flat +/-10 offset that author/tag bonuses could swamp.
    stale = recency is not None and recency > 730
    if recency is not None and recency <= 90:
        score = 55.0
    elif recency is not None and recency <= 365:
        score = 40.0
    elif stale:
        score = 15.0
    else:
        score = 25.0
    if authors >= 10:
        score += 20
    elif authors >= 5:
        score += 12
    elif authors >= 3:
        score += 6
    if tags >= 5:
        score += 15
    elif tags >= 1:
        score += 8
    score += conv * 20
    score -= bot * 30
    if stale:
        # >2 years dead is disqualifying per the rubric's own 0-anchor — cap
        # so a big contributor/tag count can't paper over a dead repo.
        score = min(score, 25.0)
    return _clamp(score), (
        f"recency_days={recency}, human_authors={authors}, semver_tags={tags}, "
        f"conv_rate={conv:.2f}, bot_ratio={bot:.2f}"
    )


def score_m(stats: dict) -> tuple[float, str]:
    cs = stats.get("class_signals") or {}
    hits = cs.get("dep_keyword_hits") or {}
    exp = hits.get("experiment_tracking") or []
    notebooks = cs.get("notebook_count") or 0
    score = 25.0 + min(40, len(exp) * 15) + min(35, notebooks * 5)
    return _clamp(score), f"experiment_tracking={exp}, notebooks={notebooks}"


# Data-validation tools within repo_stats' combined "data_eng" keyword list;
# everything else in that list is orchestration/processing.
_N_DATA_QUALITY = {"great-expectations", "great_expectations", "pandera"}


def _n_signals(stats: dict) -> tuple[list, list]:
    """Split repo_stats' single dep_keyword_hits["data_eng"] list into
    (pipeline/orchestration, data-quality) — there are no separate
    "pipeline_orchestration"/"data_quality" keys in the evidence."""
    hits = (stats.get("class_signals") or {}).get("dep_keyword_hits") or {}
    tools = hits.get("data_eng") or []
    dq = [t for t in tools if t in _N_DATA_QUALITY]
    pipeline = [t for t in tools if t not in _N_DATA_QUALITY]
    return pipeline, dq


def score_n(stats: dict) -> tuple[float, str]:
    pipeline, dq = _n_signals(stats)
    # Rubric 0-anchor: "ad-hoc scripts; no orchestration; no data validation"
    # — with neither signal the score sits at the floor; SQL/script volume
    # alone earns nothing.
    if not pipeline and not dq:
        return 10.0, "no orchestration or data-validation evidence"
    score = 15.0
    if pipeline:
        score = 45.0 + min(15, (len(pipeline) - 1) * 8)
    score += min(30, len(dq) * 15)
    return _clamp(score), f"pipeline={pipeline}, data_quality={dq}"


def score_o(stats: dict) -> tuple[float, str]:
    cs = stats.get("class_signals") or {}
    hits = cs.get("dep_keyword_hits") or {}
    iac = hits.get("iac") or []
    score = 30.0 + min(70, len(iac) * 10)
    return _clamp(score), f"iac_signals={iac}"


def score_p(stats: dict) -> tuple[float, str]:
    cs = stats.get("class_signals") or {}
    hits = cs.get("dep_keyword_hits") or {}
    sec = hits.get("security_tooling") or []
    secrets = stats.get("hardcoded_secret_hits") or 0
    score = 40.0 + min(40, len(sec) * 10) - min(30, secrets * 10)
    return _clamp(score), f"security_tooling={sec}, secret_hits={secrets}"


SPECIALIST = {
    "ml": ["M"],
    "ai_research": ["M"],
    "data_engineering": ["N"],
    "infra": ["O"],
    "security": ["P"],
}

CORE = ["B", "C", "D", "E", "F", "H", "I", "K"]

SCORERS = {
    "B": score_b,
    "C": score_c,
    "D": score_d,
    "E": score_e,
    "F": score_f,
    "H": score_h,
    "I": score_i,
    "K": lambda s, g: score_k(g),
    "M": score_m,
    "N": score_n,
    "O": score_o,
    "P": score_p,
}


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

    class_entries = []
    for cls in classes:
        dims: dict = {}
        for dim in CORE:
            if dim == "K":
                sc, ev = SCORERS[dim](repo_stats, git_repo)
            else:
                sc, ev = SCORERS[dim](repo_stats)
            dims[dim] = {"score": round(sc, 1), "evidence": ev}
        for dim in SPECIALIST.get(cls, []):
            sc, ev = SCORERS[dim](repo_stats)
            dims[dim] = {"score": round(sc, 1), "evidence": ev}
        class_entries.append({
            "name": cls,
            "confidence": classify.get("class_confidence", {}).get(cls),
            "size_loc": repo_stats.get("total_loc") or 0,
            "dimensions": {k: v["score"] for k, v in dims.items()},
            "_evidence": {k: v["evidence"] for k, v in dims.items()},
        })

    if len(class_entries) == 1:
        loc = repo_stats.get("total_loc") or 0
    else:
        loc = repo_stats.get("total_loc") or 0

    return {
        "repo_name": repo_stats.get("repo_name") or repo_path.split("/")[-1],
        "repo_path": repo_path,
        "total_loc": loc,
        "classes": [{k: v for k, v in c.items() if k != "_evidence"} for c in class_entries],
        "capacity_inputs": {
            "confirmed_candidate_count": git_stats.get("confirmed_candidate_count") or 0,
            "analyzed_commits": git_stats.get("analyzed_commits") or 0,
            "total_commits": git_repo.get("total_commits") or 0,
        },
        "_meta": {
            "classify_notes": classify.get("notes", []),
            "dimension_evidence": [c["_evidence"] for c in class_entries],
        },
    }
