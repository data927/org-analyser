# Classification

Before scoring, assign the repo to one or more of the eight classes. The class set
decides which dimensions apply and at what weight (`class-weights.md`).

## The eight classes

| Class | What it is | Strongest signals |
|---|---|---|
| **frontend** | Browser/UI app, thin or no server | React/Vue/Svelte/Angular, `.tsx/.jsx/.vue/.svelte`, high CSS ratio, Tailwind/MUI |
| **backend** | API / service, no significant UI | Express/FastAPI/Flask/Django/NestJS/Gin/Spring, ORM/DB deps, `routes/`/`services/` |
| **fullstack** | One app with strong UI *and* server | frontend + backend signals together (Next.js + API routes + DB) |
| **ml** | ML engineering / training / serving | torch/tf/jax/sklearn/transformers, training scripts, checkpoints, some serving/infra |
| **ai_research** | Experiments / reproduction-focused | ML libs + notebooks + experiment tracking (W&B/MLflow), papers/ablations, thin prod tooling |
| **data_engineering** | ETL/ELT, pipelines, warehousing | Airflow/Dagster/Prefect/dbt/Spark/Kafka, SQL-heavy, data validation |
| **security** | Security tooling / research / CTF | bandit/semgrep/nuclei/scapy/pwntools/cryptography, scanners, exploit code |
| **infra** | Infrastructure-as-code, platform | Terraform/Pulumi/CloudFormation, K8s manifests, Helm, Ansible, Dockerfiles dominant |

## How to classify

1. Run `classify_repo.py` on the `repo_stats.py` JSON. It returns a confidence
   distribution, a `primary_class`, `suggested_classes`, and `is_monorepo`. These
   are a **starting point**, not the final word.
2. Sanity-check against the tree. `classify_repo.py` is signal-driven and can be
   fooled (e.g. a backend repo that vendors a small admin UI). Adjust the class set
   using your read of the directory structure, README, and entry points.
3. **ml vs ai_research** is the most common ambiguity. Decide by *emphasis*:
   - Production/serving, deployment, inference APIs, MLOps → **ml**.
   - Experiments, ablations, paper reproduction, notebooks-as-deliverable → **ai_research**.
4. **fullstack vs frontend+backend monorepo.** If frontend and backend are one
   cohesive app (shared build, Next.js-style), classify as **fullstack** (one
   class). If they're separate deployables (`apps/web` + `apps/api`), treat as a
   **monorepo** with two components.

## Monorepos (multiple classes)

A repo is multi-class when ≥2 classes are genuinely present. Two sub-cases:

- **Shadow multi-class** — one tree, mixed signals (e.g. a backend with embedded
  Terraform for its own deploy). If one class clearly dominates and the others are
  incidental, score as a single class. Use judgment.
- **True monorepo** — distinct components under `apps/*`, `packages/*`, `services/*`,
  workspace members (pnpm/yarn/turbo/nx), or top-level project dirs. Here you want a
  real per-component breakdown:
  1. Identify each component directory.
  2. Run `repo_stats.py <component-dir>` and `classify_repo.py` **per component** to
     get its class and its `total_loc` (use that as `size_loc`).
  3. Score each component's applicable dimensions (use `git_stats.py` at the repo
     root for shared history-based dimensions A/K — git history is repo-wide).
  4. Assemble one `classes[]` entry per component in the scores JSON, then run
     `score.py`. It produces the per-class breakdown **and** the size-weighted
     overall, credits N/A dimensions as 1.0, and adds the monorepo bonus.

Don't invent components. If you can't cleanly attribute LOC to a component, fall back
to scoring the whole tree under its `suggested_classes` with the whole-repo
`total_loc` split evenly (that's `score.py`'s default when `size_loc` is omitted).

## Low-signal repos

A general-purpose library or CLI may trip none of the class detectors.
`classify_repo.py` defaults these to `backend` (general code) with a note. That's
usually right for scoring purposes — the backend profile is the most
general-purpose. Override to a better-fitting class if the tree warrants it.
