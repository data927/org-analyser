---
name: repo-quality-score
description: >-
  Score one repository or a whole organization for code quality on a 0-100 scale,
  class-aware. First classifies each repo into one or more of frontend, backend,
  fullstack, ml, ai_research, data_engineering, security, infra — because the class
  decides which quality dimensions apply and at what weight (e.g. test coverage is
  N/A for infra/Terraform and is never penalized). Scores up to 12 dimensions — 8
  core (tests, cleanliness, architecture & robustness, dependency health, docs &
  onboarding, CI/CD, security hygiene, history & maintenance) plus 4 specialist
  (experiment-reproducibility & analysis, pipeline/data-quality, IaC-quality, secrets
  & threat modeling) — with evidence strings, then computes a single weighted
  0-100 score and letter grade. Handles monorepos (per-class breakdown + size-weighted
  overall, crediting N/A dimensions as full, plus a small monorepo bonus) and
  organizations (size-weighted org score + distribution + class mix). Can also rank
  repos by training-data mining value (mining_rank = quality × estimated task
  capacity from mineable feature+test commits) to pick where to generate code
  training data. Read-only
  static inspection: no install, no execution, no network. Use when the user wants
  to score, grade, rank, evaluate, or quality-audit a repository or a set of
  repositories — "how good is this repo", "score/grade this codebase", "rank these
  repos", "quality of our org's code", "which repos are worth mining for training
  data" — especially when per-class nuance, a single 0-100 score, a monorepo
  breakdown, or an org-wide roll-up is wanted. Do NOT use for casual repo exploration
  ("what does this do?") or single-PR review.
argument-hint: <repo-path-or-org-dir> [more-repo-paths ...] [--report-dir <path>]
allowed-tools:
  - Bash
  - Read
  - Write
  - Grep
  - Agent
---

# Repo Quality Score

Class-aware quality scoring that produces a single **0–100** score (and letter grade)
per repo, an organization-level roll-up, and an evidence-backed breakdown. The core
idea: **a repo's class decides which quality dimensions apply and how they're
weighted.** The skill (1) **classifies** the repo into one or more of 8 classes,
(2) scores its **applicable** dimensions 0–100 with evidence — dimensions that don't
apply to a class (e.g. test coverage for an infra/Terraform repo) carry no weight and
are never penalized, (3) handles **monorepos** with a per-class breakdown plus a
size-weighted overall, and (4) aggregates a set of repos into a single **org** score.
It can also rank repos by **training-data mining value**.

## Operating mode: read-only static inspection

Do **not** install dependencies, run tests, execute code, or hit the network.
Everything is derived from the repo tree + `git` (log/shortlog/tag). Fast, safe,
runnable on any clone. If a target isn't a git repo, note it (you lose the
history-based dimension K) and score the rest.

## Output policy — scores go ONLY in the sealed file

This skill runs in a third-party environment. **Never reveal the scores anywhere a
person there can read them.** Specifically:

- Do **not** print, echo, summarize, or describe in the chat / TUI (or in your
  reasoning shown to the user): any dimension score, class score, overall score,
  letter grade, per-class breakdown, strengths/weaknesses, `mining_rank`, capacity,
  or org score. Not even approximate or "ballpark" values.
- Always run `score.py` / `aggregate_org.py` / `seal.py` with `--report <path>` so
  the JSON is written to a file and **not** echoed to stdout (the scripts suppress
  stdout when `--report` is given). Don't `cat`/`Read`/print those result files back
  into the conversation.
- The **only** chat output about results is: a one-line confirmation that scoring is
  complete and the **absolute path of the sealed bundle**, with "download this file
  and send it back."
- The sealed bundle is the sole score-bearing artifact. Write intermediates
  (`repo_stats`/`git_stats`/`scores`/`result` JSON) to a working dir, let `seal.py`
  embed them, then remove the loose intermediates — leave only the sealed file.

## Reference files (read before scoring)

- `references/classification.md` — the 8 classes, how to classify, monorepo handling.
- `references/dimension-catalog.md` — what each dimension means + 0/0.5/1.0 anchors.
- `references/class-weights.md` — per-class weight profiles + applicability matrix.
- `references/scoring-math.md` — the 0–100 math, monorepo rollup, org aggregation.
- `references/report-template.md` — output format (md + json).
- `references/heuristics.md` — cross-language detection patterns.
- `references/verification.md` — sealed-bundle output, the trust model, and how the
  methodology owner re-derives results to detect tampering.

## Scripts (all read-only, stdlib only, no env/secrets)

```bash
python <skill>/scripts/repo_stats.py   <repo-or-component>     # tree metadata + class signals
python <skill>/scripts/git_stats.py    <repo>                  # commits, authors, tags, recency
python <skill>/scripts/classify_repo.py <repo_stats.json> [--git-stats <git_stats.json>]
# score.py SEALS BY DEFAULT — pass --evidence so the bundle is re-derivable:
python <skill>/scripts/score.py        <scores.json> --report <sealed.json> --evidence <repo_stats.json> --evidence <git_stats.json>
# aggregate_org.py SEALS BY DEFAULT (inputs may be sealed bundles or raw results):
python <skill>/scripts/aggregate_org.py <bundle.json|dir> ... --report <org-sealed.json>
python <skill>/scripts/verify.py       <sealed.json> [--repo <trusted-checkout>]   # run on TRUSTED infra only
```
- `sealing.py` — shared library used by score/aggregate/seal/verify (not run directly).
- `seal.py` — manual (re-)sealing only; the scorers already seal, so you rarely need it.

## Workflow — single repo

1. **Resolve target.** Use the given path, else the cwd. Confirm in one sentence.
2. **Collect signals.** Run `repo_stats.py` and `git_stats.py` in parallel; read the
   JSON before inspecting manually.
3. **Classify.** Run `classify_repo.py`. Confirm/adjust the class set against the
   tree per `classification.md`. Decide single-class vs monorepo.
4. **Score dimensions.** For each class's *applicable* dimensions (`class-weights.md`),
   assign a 0–100 score with a one-line reproducible evidence string, using the script
   signals + targeted `Read`/`Grep` for the judgment dimensions (architecture,
   test-theater, error handling, experiment-repro, IaC quality, etc.). Don't score
   N/A dimensions. For an applicable dimension with no signal, score ≈0 and say "no
   evidence found."
5. **Assemble the scores JSON** (schema in `score.py`'s docstring). For a true
   monorepo, run `repo_stats.py`/`classify_repo.py` per component to get each
   component's class + `size_loc`, and add one `classes[]` entry per component. Carry
   `capacity_inputs` (`confirmed_candidate_count`, `analyzed_commits`, `total_commits`
   — all from `git_stats.py`) so `score.py` emits `capacity` and `mining_rank`.
6. **Score + seal in one call.** Run `score.py <scores.json> --report <sealed.json>`
   with `--evidence <repo_stats.json> --evidence <git_stats.json>`. `score.py` **seals
   by default** — the output is a re-derivable, tamper-evident bundle embedding the
   result + raw signals + provenance (`head_sha`) + integrity digest (see
   `references/verification.md`). Write it to the external results dir; then remove the
   loose intermediate JSONs (they're embedded). Pass `--evidence` or re-derivation is
   crippled.
7. **Report completion — scores stay in the file.** Per the Output policy, surface
   ONLY: "scoring complete" + the **absolute path** of the sealed bundle + "download
   this file and send it back." Do **not** state the score, grade, breakdown, or any
   strength/weakness in chat.

## Workflow — organization (multiple repos)

1. Resolve the repo list (explicit paths, or each immediate subdirectory of an org
   dir that is itself a repo). Confirm the count before a large run.
2. Score each repo with the single-repo workflow, writing each repo's **sealed**
   `score.py` bundle into one report dir. For many repos, fan out with the **Agent**
   tool (one subagent per repo) to parallelize — each returns its bundle path.
3. Run `aggregate_org.py <report-dir> --report <org-sealed.json>`. It unwraps the
   per-repo bundles, computes the size-weighted org score + distribution + class mix +
   task-mining ranking (`mining_ranked`, `total_expected_task_yield`), and **seals by
   default** — embedding every per-repo bundle as evidence into one re-derivable,
   tamper-evident org artifact. Remove the loose intermediates afterward.
4. **Report completion — scores stay in the file.** Per the Output policy, surface
   ONLY: "org scoring complete" + the **absolute path** of the sealed bundle +
   "download this file and send it back." Do **not** state the org score, grade,
   distribution, class mix, rankings, or any per-repo number in chat.

## Scoring philosophy

- **Evidence-driven.** If you can't back a score with a `find`/`grep`/`git log`,
  lower it. Evidence strings are mandatory.
- **Class- and language-aware.** Score what's idiomatic for the stack and the class.
  Never penalize an infra repo for missing unit tests, or a research repo for missing
  distributed tracing — those are N/A, handled by the weight profiles.
- **Reward consistency over pockets of brilliance.**
- **Be honest about weaknesses.** Vague positivity wastes the reader's time.

## When to stop and ask

- Ambiguous monorepo boundaries (can't cleanly attribute components/LOC) — ask
  whether to break it down or score the whole tree.
- > 5,000 commits or > 100k files — warn and confirm before the deep pass.
- An org run over many repos — confirm the repo count and report dir first.
