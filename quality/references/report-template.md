# Report Templates

> **These are views the methodology owner renders FROM the sealed bundle on trusted
> infrastructure — they are NOT produced or printed in the third-party environment.**
> Per the Output policy in `SKILL.md`, the only artifact written there is the sealed
> bundle, and no scores are shown in the chat/TUI. Render these layouts after a repo's
> `verify.py` check passes, from the bundle's `result` (and embedded `evidence`).

The machine-readable data is the `result` block inside the sealed bundle (the
`score.py` / `aggregate_org.py` output). The layouts below are the human-readable
summaries derived from it.

---

## Per-repo markdown (`repo-quality-score.<repo_name>.md`)

```markdown
# Repo Quality Score: {repo_name}

**Date:** {date}  ·  **Path:** {path}
**Overall:** {overall_score} / 100  ({overall_grade}){monorepo_suffix}
**Mining rank:** {mining_rank}  (quality {overall_score}/100 × capacity {capacity} est. tasks)

> {monorepo_suffix} is " · monorepo: N components, +{bonus} bonus" when applicable.
> The mining-rank line is shown only when capacity was estimated (git history
> available). Capacity = (confirmed_candidate / analyzed_commits) × total_commits.

## Snapshot (facts, unscored)
| Field | Value |
|---|---|
| Class(es) | {classes_with_confidence} |
| Primary language | {primary_language} |
| Frameworks | {frameworks} |
| Source LOC | {total_loc} |
| Code files / test files | {code_files} / {test_files} |
| Test:source ratio | {test_source_ratio} |
| Commits | {total_commits} |
| Human contributors | {human_contributors} (bot ratio {bot_ratio}) |
| Tags / releases | {tag_count} (latest {latest_tag}) |
| Staleness | {recency_days} days since last commit |
| Repo age | {span_days} days |

## Score by class
| Class | Size (LOC) | Class score | Grade | (Overall contribution) |
|---|---|---|---|---|
| {class} | {size_loc} | {class_score} | {grade} | {catalog_completed_score} |
| **Overall** | — | — | **{overall_grade}** | **{overall_score}** |

## Dimension detail
For each scored class, a table of its applicable dimensions:

### {class} — {class_score} ({grade})
| Dim | Name | Score | Evidence |
|---|---|---|---|
| {key} | {name} | {score} | {one-line reproducible evidence} |

(N/A dimensions for this class are listed once: "N/A (not penalized): {keys}".)

## Top strengths
1. **{dimension}** ({score}): {why}

## Top weaknesses (highest leverage)
1. **{dimension}** ({score}): {what to fix}

## Honest assessment
{2–4 candid sentences. Name structural problems concretely. If a dimension is N/A
for the class, say so — don't imply a gap that isn't real for this kind of repo.}
```

---

## Org markdown (`repo-quality-score.org.md`)

```markdown
# Org Quality Score

**Date:** {date}  ·  **Repos:** {repo_count} ({monorepo_count} monorepos)
**Org score:** {org_score} / 100 ({org_grade})  — size-weighted
**Total expected task yield:** {total_expected_task_yield}  (Σ quality × capacity, {repos_with_capacity} repos)

## Distribution
| Stat | Value |
|---|---|
| Size-weighted mean | {size_weighted_mean} |
| Simple mean | {simple_mean} |
| Min / Median / Max | {min} / {median} / {max} |
| Stdev | {stdev} |
| Total LOC | {total_loc} |

> If min is low and stdev is high, weak repos are hidden behind strong ones — say so.

## Class mix
| Class | Repos | LOC |
|---|---|---|
| {class} | {repo_count} | {loc} |

## Repos (ranked by quality)
| Repo | Score | Grade | LOC | Class(es) |
|---|---|---|---|---|
| {repo_name} | {overall_score} | {overall_grade} | {total_loc} | {classes} |

## Repos (ranked by mining value)
Order by `mining_rank` = quality × capacity — where to mine training data first.
| Repo | Mining rank | Quality | Capacity (est. tasks) |
|---|---|---|---|
| {repo_name} | {mining_rank} | {overall_score} | {capacity} |

## Attention list
- **Weakest quality:** {worst_repos}
- **Strongest quality:** {best_repos}
- **Top mining targets:** {mining_ranked top N}

## Org assessment
{2–4 sentences: where quality concentrates, where the risk is, what to prioritize.}
```
