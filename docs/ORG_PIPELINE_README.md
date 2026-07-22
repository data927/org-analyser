# Org Pipeline

One command runs analysis pipelines for **one GitHub org**, **one GitLab group/project**,
**one Bitbucket workspace/repo**, or a **folder of local/downloaded repos** per invocation:

1. **Merged PR counts** — fresh API fetch for every repo *(skipped in local mode)*
2. **PR task-profile report** — rules + LLM classification (`Standard Feature Work %`, `Rich Task %`, `Other %`, `Automated %`)
3. **Codebase profiler** — vendor intake sheet (`codebase_sheet.filled.xlsx`)
4. **Repo analyzer** — LLM-usage detection, training-data-quality scoring, and CI/test analysis per repo (`analysis/repo_analyzer.py`, local-clone mode)
5. **Data eval-kit** — full repository evaluation with **mandatory LLM** (quality, taxonomy, PR rubrics)
6. **Repo quality score** — sealed 0–100 heuristic scoring per repo + org rollup *(pass `--skip-quality-score` to skip)*

Output is a timestamped run folder and a **zip** containing all reports and logs.

Setup, tokens, and run examples: see the root [`README.md`](./README.md).
For **self-hosted GitLab**, set `gitlab_host` in `config.yml` or pass
`--gitlab-host` on every run (including `org-analyser check`). Without it,
API calls and clones default to gitlab.com and tokens from your instance will
401.
There are **no** `--limit`/`--max-repos`/`--max-prs` options — every discovered
repo is processed. This doc covers what a run actually does internally.

An optional `--repos-manifest repos-manifest.json` maps local folder names to
remote repos for PR-based phases in `--local-repos-dir` mode, e.g.:

```json
{ "frontend": "your-org/frontend", "backend": "gitlab:my-group/my-backend" }
```

Without one, the pipeline uses each folder name as the repo id and tries to
parse `origin` from git remotes. `--local-only` skips remote lookups entirely.

---

## What each run does

1. **Preflight** — verifies tokens, LLM credential (OpenAI or Azure), git, scc, node
2. **Discover repos** — lists org/group/workspace repos via API, or subfolders under `--local-repos-dir`
3. **Merged PR counts** — refetches counts from the API *(skipped for local mode)*
4. **PR task-profile** — org-level `org_summary.csv` / `org_summary.json` under `pr-task-profile/` *(skipped in local mode without remote mapping)*
5. **Per repo (parallel)** — for each repo:
   - **Remote mode:** delete any prior clone and **fresh clone** (token passed via a short-lived `git config` header, never embedded in the URL or written to `.git/config`)
   - **Local mode:** use existing checkout in place (no clone, source not deleted)
   - Run codebase profiler → append row to xlsx
   - Run repo analyzer (local-clone mode) → per-repo CSV + detail JSON
   - Run eval-kit with full LLM (unless `--skip-f2p`)
   - Run repo-quality-score collect → classify → seal *(unless `--skip-quality-score`)*
6. **Org quality rollup** — `org.sealed.json` + summary CSV/JSON *(unless `--skip-quality-score`)*
7. **Remove clones** — remote clones deleted before packaging; **local source folders are never deleted**
8. **Zip** — reports and logs only, packaged as `<run-name>.zip`

If one repo fails a phase after retries, the run **continues** with the next repo. Check `manifest.json` and per-repo logs under `logs/`.

---

## Output layout

```
outputs/org-analyser-runs/
└── org-analyser-your-org-20260627T120000Z/
    ├── manifest.json
    ├── org-analyser-your-org-20260627T120000Z.zip
    ├── logs/
    │   ├── pipeline.log
    │   └── pr-task-profile.log
    │   └── github/your-org/<repo>/
    │       ├── clone.log
    │       ├── codebase-profiler.log
    │       ├── repo-analyzer.log
    │       ├── eval-kit.log
    │       └── repo-quality-score.log
    ├── merged-pr-counts/
    │   ├── github_your-org.csv
    │   ├── summary.csv
    │   └── manifest.json
    ├── pr-task-profile/
    │   └── scan_<timestamp>/
    │       ├── org_summary.csv
    │       └── org_summary.json
    ├── codebase-profiler/
    │   └── codebase_sheet.filled.xlsx
    ├── repo-analyzer/
    │   └── <org>/<repo>/<repo>.csv (+ <repo>_detail.json)
    ├── eval-kit/
    │   └── <org>/<repo>/*.json
    └── repo-quality-score/
        ├── repos/*.sealed.json
        ├── org.sealed.json
        ├── summary.csv
        └── summary.json
```

---

## Runtime and disk

- Large orgs can take **many hours** or days depending on repo count, size, and LLM latency.
- Every repo is **fully cloned** during processing (unless you set `--clone-depth`), then **clones are deleted** before the zip is created.
- Plan for temporary disk space during the run, not in the final deliverable.

---

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| Script exits immediately | LLM credential missing; required platform token missing |
| Clone failures | Token scopes; repo access; logs in `logs/.../clone.log` |
| GitLab 401 / wrong host | `gitlab_host` in `config.yml` or `--gitlab-host` must match your instance, not gitlab.com |
| Profiler warnings | Install `scc` and Node.js; see profiler log |
| Repo-analyzer failures | Repo log under `repo-analyzer.log`; runs local-only, no token needed |
| Eval-kit failures | LLM credential valid; repo log under `eval-kit.log` |
| Bitbucket auth errors | See `profiler/README.md` → Authentication for the token-type-to-env-var mapping (Atlassian API token vs. app password vs. access token) |
| Partial run | Normal for large orgs — inspect `manifest.json` summary |

---

## Components (not replaced)

`cli.py` orchestrates existing packages in this repo:

- `analysis/merged_prs.py` / `analysis/pr_task_profile.py`
- `analysis/repo_analyzer.py` — LLM-usage detection, training-data-quality scoring, CI/test analysis; see [`PR_TASK_PROFILE_README.md`](./PR_TASK_PROFILE_README.md) for PR task-profile details
- `profiler/`
- `eval/repo_evaluator.py`
- `quality/` *(unless `--skip-quality-score`)*
