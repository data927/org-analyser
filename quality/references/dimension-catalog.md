# Dimension Catalog

Every dimension is scored **0–100** with a one-line evidence string. Anchors describe
what 0, 50, and 100 look like — interpolate in between. Score the *intent and
idiom* for the language/stack, not a fixed checklist (a Go repo with `go vet` +
`golangci-lint` scores like a TS repo with ESLint + `tsc --strict`).

The catalog has two tiers (keys are non-contiguous by design — it is exactly these 12):
- **Core (B, C, D, E, F, H, I, K)** — 8 dimensions that apply to most classes. Three
  are deliberately composite to keep the rubric lean: **D** spans architecture *and*
  runtime robustness (logging, error tracking, health); **F** spans documentation
  *and* reproducibility/onboarding; **K** spans commit discipline *and*
  recency/contributors/releases.
- **Specialist (M, N, O, P)** — apply mainly to one class. (frontend and fullstack
  have no specialist dimension; they are scored on core dimensions only.)

Which dimensions are *applicable* to each class (and at what weight) is defined in
`class-weights.md` and enforced by `scripts/score.py`. A dimension that is N/A to a
class is **never scored and never penalized** for that class.

> Evidence is mandatory. Every score needs a one-liner someone could reproduce with
> a `grep`, `find`, or `git log`. "Looks clean" is not evidence. If there is no
> signal for an *applicable* dimension, score it low (≈0) and say "no evidence
> found" — don't leave it blank.

---

## Core dimensions

### B. Test Coverage
Framework configured, test:source ratio, breadth across modules, coverage
enforcement, tests assert real behavior (not theater), independence from live
services. Signals: `repo_stats.py` → `test_spec_files`, `test_source_ratio`,
`coverage_tooling`, `coverage_threshold`, `ci_runs_tests`.
- **0** no framework, or <5 specs, ratio <1:50.
- **50** framework configured, ratio ~1:10–1:5, no enforced threshold, some mocking-heavy tests.
- **100** ratio ≥1:3, breadth ≥70% of modules, enforced coverage in CI, integration tests hit real local resources.

### C. Code Cleanliness
Lint/format config (and enforced in CI), file-size distribution (god files),
dead-code signals, duplication, naming consistency. Signals: `repo_stats.py` →
`linters_and_formatters`, `median_file_size_loc`, `god_files_over_500_loc`,
`god_files_over_1000_loc`.
- **0** no lint/format config; multiple >1000-LOC files; copy-paste clusters; mixed conventions.
- **50** linter exists but not enforced; a few >500-LOC files; occasional duplication.
- **100** lint+format enforced in CI; no source file >500 LOC; DRY; consistent idiomatic naming.

### D. Architecture & Robustness
Layering/separation of concerns, type discipline + schemas, modularity/extension
points, circular-dependency signals, error-handling discipline, **and runtime
robustness — structured logging, error tracking, health checks/metrics**. Signals:
directory structure; spot-check for layer leakage, typed
errors, cycles; `repo_stats.py` → `logging_framework`, `error_tracking`,
`has_health_endpoint`, `has_metrics`. *Score observability relative to project type —
a library/CLI needs little; a service needs a lot.*
- **0** logic/data/transport tangled, untyped, cycles, swallowed errors; only `print`/`console.log`, no health/metrics for a service.
- **50** partial layering and typing; some extension points; logging present but unstructured; top-level error tracking only.
- **100** clear layers; strong typing + schemas; features touch few well-defined files; consistent typed errors; structured logging with context; error tracking + health/metrics where the service warrants.

### E. Dependency Health
Direct/transitive package count (relative to project type), lockfile discipline,
dev/prod separation, freshness, private/patched-dep signals. Signals:
`repo_stats.py` → `direct_runtime_deps`, `total_transitive_deps`, `lockfiles_found`
vs `lockfiles_expected`, `dep_update_tooling`.
- **0** no lockfile or drifted; flat dep list; key deps several majors behind; private/git deps.
- **50** lockfile present for main PM; mostly separated; some staleness.
- **100** every PM has a committed lockfile; clean dev/prod split; deps current; update tooling configured.

### F. Docs & Onboarding
**Documentation** (README completeness, inline/API doc coverage, changelog,
contributing guide) **plus reproducibility** (containerization/devcontainer,
`.env.example` completeness with no `.env` committed, self-contained setup) — the two
together answer "can a new dev understand this *and* get it running." Signals:
`repo_stats.py` → `readme_loc`, `readme_sections`, `changelog`, `contributing_guide`,
`has_dockerfile`, `has_docker_compose`, `has_devcontainer`, `env_example_file`,
`env_vars_missing_from_example`.
- **0** no/boilerplate README, no doc comments; no container, no `.env.example`, no real setup steps.
- **50** README explains project + install but lacks architecture/env; APIs partly documented; prod-only Dockerfile or incomplete `.env.example`.
- **100** README covers what/why/install/run/test/env/architecture; public APIs documented; curated changelog + contributing guide; `docker compose up` (or devcontainer) brings up app + deps; clone-install-run is self-contained.

### H. CI/CD Maturity
CI presence + scope (lint/type/test/deploy), pipeline quality (pinned actions,
caching, matrix), branch-protection signals. Signals: `repo_stats.py` →
`ci_present`, `ci_runs_tests`, `ci_runs_lint`, `ci_runs_typecheck`, `ci_has_deploy`.
- **0** no CI.
- **50** CI exists but single-step (just build, or just lint).
- **100** lint + type-check + tests on every PR; separate deploy; cached, pinned, structured.

### I. Security Hygiene
Hardcoded-secret signals, committed `.env`, dep-audit in CI, input validation at
boundaries. Signals: `repo_stats.py` → `hardcoded_secret_hits`,
`env_files_committed`, `dep_audit_in_ci`, `input_validation_patterns`.
- **0** real secrets in source or a committed `.env`; no validation at entry points.
- **50** no obvious hardcoded secrets; inconsistent validation; no dep audit.
- **100** secrets only via env/secret-manager; validation schema at all entry points; dep audit + alerts in CI.

### K. History & Maintenance
Recency/staleness, contributor diversity (bus factor), release/tag cadence, **and
commit discipline** (message quality, atomic commits, bot ratio). All git-derived.
Signals: `git_stats.py` → `recency_days`, `human_authors`,
top-author distribution, `total_commits`, `tag_count`, `semver_tag_count`,
`latest_tag`, `conventional_rate_last_200`, `bot_commit_ratio`.
- **0** last commit >2 years ago; single author; no tags; terse "wip"/giant mixed commits; >50% bot.
- **50** last commit 6–18 months; 3–8 contributors; occasional tags; ~half conventional commits.
- **100** active in last 3 months; ≥10 human contributors (several with >5 commits); regular semver releases; ≥80% single-concern conventional commits; <5% bot.

---

## Specialist dimensions

### M. Experiment Reproducibility & Analysis — *ML / AI research*
Two things: (a) **reproducibility** — can an experiment be re-run and a result
reproduced — and (b) **analysis rigor** — is the data and results work sound.
Signals: `class_signals.dep_keyword_hits.experiment_tracking`, `notebook_count`;
spot-check for the items below.

Reproducibility: seed-setting, config-as-code (Hydra/YAML/argparse configs), saved
checkpoints, a documented one-command "reproduce" path.

Analysis:
- **Data analysis (EDA)** — exploratory analysis, dataset stats/visualizations, data-
  quality and leakage checks before modeling.
- **Performance analysis** — evaluation beyond a single metric: baselines, ablations,
  error analysis, metric breakdowns, significance/CIs, benchmark comparisons.
- **If the data is tabular** — also **feature engineering** (derived features,
  transformations, encoding) and **feature selection** (importance, correlation
  pruning, selection methods), each with a documented rationale.

- **0** notebooks with hardcoded paths, no seeds/config/tracking; not reproducible; one metric reported with no analysis; (tabular) raw features dumped into a model.
- **50** some configs/seeds; tracking present but inconsistent; basic EDA + a couple of metrics; (tabular) some feature engineering but ad-hoc/unjustified selection.
- **100** seeded, config-driven runs + tracking + checkpoints + one-command reproduce; thorough EDA and data-quality checks; performance analysis with baselines/ablations/error analysis; (tabular) principled, documented feature engineering + selection.

### N. Pipeline & Data Quality — *Data Engineering*
Orchestration, idempotency, data validation, lineage, SQL hygiene. Signals:
`class_signals.dep_keyword_hits.data_eng`, `sql_file_count`, `sql_loc`; spot-check
for DAG definitions, idempotent writes, validation (Great Expectations / pandera /
dbt tests), partitioning, lineage/docs.
- **0** ad-hoc scripts; no orchestration; non-idempotent; no data validation.
- **50** orchestrated (Airflow/Dagster/dbt) but partial idempotency; some data tests; thin lineage.
- **100** declarative orchestration; idempotent + backfillable; data-quality tests gate the pipeline; documented lineage; tidy parameterized SQL.

### O. IaC Quality — *Infra*
Formatting/validation, module structure, policy scanning, plan-in-CI, no hardcoded
resources/state secrets. Signals: `class_signals.terraform_*`, `k8s_manifest_count`,
`helm_present`, `pulumi_present`, `ansible_present`; spot-check for `fmt`/`validate`,
reusable modules, `tfsec`/`checkov`, remote state, pinned providers.
- **0** monolithic copy-pasted resources; no fmt/validate; secrets/state in repo; no plan step.
- **50** some modules; fmt/validate run locally; providers loosely pinned; no policy scanning.
- **100** reusable pinned modules; `fmt`+`validate`+`plan` in CI; `tfsec`/`checkov` gating; remote encrypted state; no hardcoded secrets.

### P. Secrets & Threat Modeling — *Security*
Kept deliberately simple: proactive **secrets management** + a **threat model**.
(Distinct from I/Security Hygiene, which is the absence of leaks — hardcoded secrets,
committed `.env`. P is the positive practice.) Spot-check for:
- a secret manager / vault in use — HashiCorp Vault, AWS/GCP Secret Manager, Azure
  Key Vault, Doppler, SOPS, sealed-secrets — instead of plaintext config/env,
- a documented threat model / `SECURITY.md` / security design notes.
- **0** secrets in plaintext config or committed env; no threat model.
- **50** some secret-manager usage **or** a basic SECURITY.md, but inconsistent.
- **100** secrets sourced from a vault/secret manager throughout; documented threat model and secure-by-default posture.
