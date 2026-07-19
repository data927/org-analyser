# Scoring Math

All scores are on a **0–100** scale. `scripts/score.py` and
`scripts/aggregate_org.py` implement everything here; this is the spec they follow.

## 1. Dimension score

Each applicable dimension gets a score in 0–100 (see `dimension-catalog.md` anchors).
You may supply it three ways in the scores JSON:
- a bare number (`"B": 70`),
- `{"score": 70}`,
- `{"subs": {"B1": 8, "B2": 6}}` — 0–10 sub-scores, averaged then scaled ×10.

## 2. Class score (the per-class breakdown value)

For class *c* with weight profile `W_c` (from `class-weights.md`), over its
**applicable** dimensions only:

```
class_score_c = Σ_{d ∈ applicable(c)} (W_c[d] / ΣW_c) · dim_score[d]
```

Weights are renormalized to sum to 1, so N/A dimensions simply don't dilute the
score. An applicable dimension with no score provided is treated as **0** (no
evidence = no credit) and a warning is emitted.

## 3. Repo score

**Single-class repo** → `overall_score = class_score_c`. No bonus.

**Monorepo (≥2 classes):**

1. Let `U` = union of applicable dimensions across all detected classes.
2. For each class *c*, compute a **catalog-completed** score over `U`:
   - For `d` applicable to *c*: weight `W_c[d]`, score `dim_score[d]`.
   - For `d` in `U` but **N/A** to *c*: weight `Wdef[d]`, score **100** (credited as
     full — this is the "treat tests-in-infra as full, not 0" rule).
   - `Wdef[d]` = mean of `W_c[d]` across the classes that use `d` (a realistic
     default magnitude), computed by `score.py`.
   ```
   cc_c = Σ_{d ∈ U} (w / Σw) · score_cd
   ```
3. Size-weight by LOC (`size_loc` per class; defaults to an even split of
   `total_loc`):
   ```
   overall_base = Σ_c (size_c / Σ size) · cc_c
   ```
4. Monorepo bonus: `bonus = min(cap, per_extra · (num_classes − 1))`, defaults
   `per_extra = 2`, `cap = 5` (points). Rewards the coordination/tooling overhead of
   a well-run multi-component repo.
   ```
   overall_score = min(100, overall_base + bonus)
   ```

The per-class breakdown shows **both** `class_score` (honest quality of that
component's applicable dimensions) and `catalog_completed_score` (its contribution
to the overall). They differ because the latter credits N/A dimensions as 100.

## 4. Grade

| Grade | Range |
|---|---|
| A | ≥ 85 |
| B | 70 – 84.9 |
| C | 55 – 69.9 |
| D | 40 – 54.9 |
| F | < 40 |

## 5. Org score

`aggregate_org.py` over the per-repo `score.py` outputs:

```
org_score = Σ_r (loc_r / Σ loc) · overall_score_r        # size-weighted (headline)
```

A repo with no `total_loc` is weighted as 1. Also reported:
- **simple_mean** (unweighted) — so a few huge repos don't fully hide the small ones,
- **distribution** — min / max / median / population stdev / count,
- **class_mix** — repo count and LOC per class across the org,
- **best_repos / worst_repos** — ranked, to direct attention.

Use the size-weighted `org_score` as the headline; read it alongside the
distribution. A high mean with a low `min` and high `stdev` means the org has weak
repos hidden behind strong ones — call that out.

## 6. Task-mining capacity & rank (for ranking, not grading)

The 0–100 quality score is deliberately **size-independent** — a small pristine repo
can score 100. When the goal is to *rank repos by how much training data they can
yield*, combine quality with a separate **volume** axis. Quality and volume stay
decoupled; their product ranks.

**Capacity** = estimated number of mineable feature+test tasks, from `git_stats.py`,
extrapolated to full history (so long-history repos aren't truncated by the
`--limit` scan window):

```
candidate_rate = confirmed_candidate_count / analyzed_commits
capacity       = candidate_rate · total_commits
```

This proxy counts atomic feature+test commits — the raw material for SWE-style tasks
— so it ignores boilerplate/generated/config LOC that inflates a raw-LOC count.

**Mining rank** (per repo) — expected good-task yield:
```
mining_rank = (overall_score / 100) · capacity
```
The quality *fraction* times the volume axis. A repo ranks high only when it is
*both* good enough (clean, low-noise tasks) *and* big enough (many tasks). No
threshold/gate is applied — every repo gets a rank.

**Org level** (`aggregate_org.py`):
- `mining_ranked` — repos ordered by `mining_rank`,
- `total_expected_task_yield` = Σ `mining_rank` — the org's expected good-task yield.

Note: for a pure ranking, any constant calibration factor inside capacity cancels
out (`rank(q·LOC) == rank(q·LOC/1000)`); the ordering depends only on the *choice* of
proxy (here, mineable commits), not on a tasks-per-unit constant. Capacity is emitted
only when the scores JSON carries `capacity_inputs` (the three `git_stats.py` fields)
or an explicit `capacity`.
