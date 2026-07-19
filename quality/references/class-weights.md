# Class Weight Profiles

The eight classes and their per-dimension weight profiles. **`scripts/score.py`
`PROFILES` is the authoritative source** — this file mirrors it for humans. If you
change one, change both.

Weights are *relative*; `score.py` renormalizes each profile to sum to 1 at scoring
time. A blank cell means the dimension is **N/A** for that class: it is not scored
and never penalized. (In a monorepo rollup, an N/A dimension is credited as 100 at
its default weight — see `scoring-math.md`.)

The 8 core dimensions are B, C, D, E, F, H, I, K; the 4 specialist dimensions are
M, N, O, P. Three core dimensions are composite: **D** spans architecture *and*
runtime robustness (logging/error-tracking/health), **F** spans documentation *and*
reproducibility/onboarding, **K** spans commit discipline *and*
recency/contributors/releases. frontend and fullstack have **no specialist
dimension** — they are scored on core dimensions only.

| Dim | Name | frontend | backend | fullstack | ml | ai_research | data_eng | security | infra |
|---|---|---|---|---|---|---|---|---|---|
| B | Test Coverage | 0.12 | 0.16 | 0.15 | 0.08 | 0.06 | 0.10 | 0.08 | — |
| C | Code Cleanliness | 0.17 | 0.10 | 0.11 | 0.12 | 0.10 | 0.08 | 0.06 | 0.04 |
| D | Architecture & Robustness | 0.21 | 0.24 | 0.23 | 0.13 | 0.10 | 0.22 | 0.13 | 0.15 |
| E | Dependency Health | 0.10 | 0.08 | 0.08 | 0.08 | 0.06 | 0.08 | 0.10 | 0.08 |
| F | Docs & Onboarding | 0.14 | 0.12 | 0.14 | 0.22 | 0.28 | 0.15 | 0.11 | 0.21 |
| H | CI/CD Maturity | 0.10 | 0.08 | 0.08 | 0.07 | 0.04 | 0.08 | 0.08 | 0.12 |
| I | Security Hygiene | 0.08 | 0.12 | 0.12 | 0.03 | 0.02 | 0.06 | 0.18 | 0.14 |
| K | History & Maintenance | 0.08 | 0.10 | 0.09 | 0.09 | 0.10 | 0.03 | 0.04 | 0.04 |
| M | Experiment Reproducibility & Analysis | — | — | — | 0.18 | 0.24 | — | — | — |
| N | Pipeline & Data Quality | — | — | — | — | — | 0.20 | — | — |
| O | IaC Quality | — | — | — | — | — | — | — | 0.22 |
| P | Secrets & Threat Modeling | — | — | — | — | — | — | 0.22 | — |

## Why the notable cells

- **infra has no B (Test Coverage).** Terraform/K8s/Helm code has no conventional
  unit tests, so penalizing its absence is wrong. Infra quality lives in **O (IaC
  Quality)** — fmt/validate/plan, policy scanning, module structure — and in **F**
  and **H** instead.
- **D (Architecture & Robustness)** is the heaviest core dimension across most
  classes because it spans layering/typing/modularity *and* runtime robustness
  (error handling, logging, health checks).
- **F (Docs & Onboarding)** carries README/API-docs *and* reproducibility (containers,
  env, setup). It dominates **ml** and **ai_research** — a research repo's value is a
  reproducible, documented experiment.
- **K (History & Maintenance)** applies to every class (every git repo has history):
  recency/staleness, bus factor, commit discipline, and release/tag cadence. It's
  light for infra/security/data-eng where release-cadence expectations are lower.
- **ml / ai_research** down-weight **I** and lead with **M (Experiment
  Reproducibility & Analysis)** — covering reproducibility plus data analysis,
  performance analysis, and (for tabular data) feature engineering + selection —
  alongside **F**.
- **data_engineering** leads with **D** and **N (Pipeline & Data Quality)**.
- **security** leads with **P (Secrets & Threat Modeling — vault/secret-manager use +
  threat model)** and **I**, and weights **E** higher (supply-chain surface).
- **frontend / fullstack** have no specialist dimension; their removed Q/R weight was
  redistributed into core (notably **D** and **C**).

## Editing or adding a class

1. Edit `PROFILES` in `scripts/score.py` (authoritative).
2. Mirror the change in this table.
3. If you add a class, also add its detection signals to `classify_repo.py` and
   `classification.md`.
4. Re-run a smoke test (`python scripts/score.py <a sample scores.json>`).
