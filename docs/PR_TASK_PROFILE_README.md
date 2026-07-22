# PR Task-Profile Report

Classify every **merged** pull request / merge request into task profiles using two independent methods:

1. **Rules** — deterministic rulebook (fast, consistent, transparent)
2. **LLM** — language model judging the same extracted signals (better at nuance)

Supports **GitHub** (GraphQL), **GitLab** (REST), and **Bitbucket** (REST 2.0).

Module: [`analysis/pr_task_profile.py`](./analysis/pr_task_profile.py) (run via
`python3 -m analysis.pr_task_profile ...` after `pip install -e .`, or through
the unified `org-analyser` CLI, which calls this module directly).

---

## Task profiles

| Profile | Definition |
|---------|------------|
| `simple_fix` | 1–2 files, no meaningful human discussion (config, deps, typos). |
| `standard_feature_work` | 3–10 files, typically touches tests, normal review. |
| `rich_task` | Linked issue **and** substantive human review. |
| `other` | Does not cleanly fit the above. |
| `automated` | Bot-authored PRs (Dependabot, Renovate, etc.). |

Setup, tokens, and dependencies: see the root [`README.md`](./README.md). Needs
whichever of `GITHUB_TOKEN`/`GITLAB_TOKEN`/`BITBUCKET_TOKEN`(+`BITBUCKET_USERNAME`)
your targets require, plus an LLM credential (`OPENAI_API_KEY` or Azure) — the LLM
pass is mandatory.

---

## Usage

### GitHub targets

| Goal | Command |
|------|---------|
| One repo | `--repo owner/name` |
| Several repos | `--repo owner/a,owner/b` |
| All repos of an owner | `--repo owner` |
| Whole org | `--org my-org` |
| User's repos | `--user my-handle` |

### GitLab targets

| Goal | Command |
|------|---------|
| Whole group (incl. subgroups) | `--gitlab-group my-group` |
| Single project | `--gitlab-project group/project` |
| Self-hosted instance | add `--gitlab-host gitlab.example.com` (hostname or full `https://` URL) |

For self-hosted GitLab, `--gitlab-host` is required unless you export
`GITLAB_HOST` or run through `org-analyser`, which reads `gitlab_host` from
`config.yml`.

### Bitbucket targets

| Goal | Command |
|------|---------|
| One or more repos | `--bitbucket-repo workspace/repo` (repeatable / comma-separated) |

Bitbucket has no whole-workspace expansion here — pass each repo explicitly
(the unified `org-analyser --bitbucket-workspace ...` CLI expands a workspace
for you and calls this module with the resolved repo list).

### Examples

```bash
# GitHub org — all merged PRs, org-level summary + zip
python3 -m analysis.pr_task_profile --org your-org

# GitHub repo with tuning for large orgs
python3 -m analysis.pr_task_profile --org your-org --page-size 50 --max-workers 16 --sleep 0.5

# GitLab group
python3 -m analysis.pr_task_profile --gitlab-group my-group --max-workers 16 --sleep 0.3

# Self-hosted GitLab project
python3 -m analysis.pr_task_profile \
  --gitlab-project my-group/my-backend \
  --gitlab-host gitlab.example.com

# Bitbucket repos
python3 -m analysis.pr_task_profile --bitbucket-repo my-workspace/frontend --bitbucket-repo my-workspace/backend

# Mixed targets in one run
python3 -m analysis.pr_task_profile --org your-org --repo your-org/mobile-app
```

---

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | — | GitHub `owner/name` or bare owner. Repeatable / comma-separated. |
| `--org` | — | GitHub org login. Repeatable / comma-separated. |
| `--user` | — | GitHub user login. Repeatable / comma-separated. |
| `--gitlab-group` | — | GitLab group path. Repeatable / comma-separated. |
| `--gitlab-project` | — | GitLab project path. Repeatable / comma-separated. |
| `--gitlab-host` | `GITLAB_HOST` env, else `gitlab.com` | GitLab hostname or base URL (self-hosted instances). |
| `--bitbucket-repo` | — | Bitbucket `workspace/repo` path. Repeatable / comma-separated. |
| `--include-archived` | off | Include archived repos/projects. |
| `--no-forks` / `--include-forks` | forks excluded | Fork handling for GitHub expansion. |
| `--output-dir` | `outputs` | Base directory for run output. |
| `--model` | `gpt-4o-mini` | OpenAI model for LLM pass. |
| `--max-workers` | `6` | Parallel LLM calls. |
| `--page-size` | `50` | GitHub GraphQL page size (lower if 502 errors). |
| `--sleep` | `0.2` | Seconds between API pages. |
| `--verbose` | off | DEBUG console output. |

Every run always scans **all** resolved repos and **all** merged PRs/MRs in each repo, and always creates a zip archive of the org-level deliverables.

---

## Output

Each run creates a timestamped directory: `outputs/scan_<YYYYMMDD_HHMMSS>/`

### Primary deliverables (org-level, repo rows)

| File | Description |
|------|-------------|
| **`org_summary.csv`** | One row per repository with PR counts and task-profile percentages (rules + LLM). Final row is weighted org total. |
| **`org_summary.json`** | Same repo-level data as structured JSON, plus metadata, org total, combined summary, and failures. |
| **`scan_<timestamp>.log`** | Full run log — repos scanned, API pages, retries, per-repo summaries, failures. |
| **`failures.json`** | Written only when repos fail (fetch or classification errors). |
| **`scan_<timestamp>.zip`** | Archive containing `org_summary.csv`, `org_summary.json`, the log, and `failures.json` (if any). |

### Additional detail files

| File | Description |
|------|-------------|
| `combined_report.json` | Full metadata + all PR-level results. |
| `combined_per_pr.csv` | Every PR/MR with both labels and extracted signals. |
| `repos/<slug>.json` / `.csv` | Per-repo reports. |

### `org_summary.csv` columns

`repository`, `platform`, `total_prs`, `agreement_rate_pct`, `llm_error_count`,
`rules_simple_fix_pct`, `rules_standard_feature_work_pct`, `rules_rich_task_pct`, `rules_other_pct`, `rules_automated_pct`,
`llm_simple_fix_pct`, `llm_standard_feature_work_pct`, `llm_rich_task_pct`, `llm_other_pct`, `llm_automated_pct`

### `org_summary.json` structure

```jsonc
{
  "metadata": { "run_id": "...", "targets": {...}, "repositories_failed": {...} },
  "org_total": { "repository": "org total", "total_prs": 1234, ... },
  "combined_summary": { "rules": {...}, "llm": {...}, "agreement": {...} },
  "repositories": [ /* one object per repo, same fields as CSV rows */ ],
  "failures": { "owner/repo": "fetch failed: ..." }
}
```

---

## How it works

1. **Resolve targets** → de-duplicated list of GitHub repos, GitLab projects, or Bitbucket repos.
2. **Fetch merged PRs/MRs** via GitHub GraphQL, GitLab REST, or Bitbucket REST 2.0 (with checkpoint/resume support).
3. **Extract signals** — file count, tests, linked issues, discussion, reviewers, bots.
4. **Classify twice** — rules and LLM on the same signals.
5. **Write outputs** — org-level repo summary (CSV + JSON), per-PR detail, log, and zip archive.

Checkpoints are stored under `<output-dir>/checkpoints/` so interrupted runs can resume.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Token not set | Export `GITHUB_TOKEN`, `GITLAB_TOKEN`, `BITBUCKET_TOKEN`, or `OPENAI_API_KEY`. |
| GitLab 401 on gitlab.com | Token is from a self-hosted instance — set `--gitlab-host` or `GITLAB_HOST`. Check the log line `host=...`. |
| Bitbucket "Token is invalid" | Atlassian API tokens (`ATATT…`) need `BITBUCKET_USERNAME` set to your Atlassian account **email**, not username. |
| GraphQL 502/503/504 | Lower `--page-size`, raise `--sleep`; script retries automatically. |
| Rate limits | Lower `--max-workers`, raise `--sleep`. |
| Slow GitLab scan | 2 API calls per MR; expect hours for large groups. |
| `llm_category = error` | Transient API failure; re-run affected repos. |

---

## Interpreting results

- **High agreement rate** → rules and LLM concur; labels are more trustworthy.
- **`top_disagreements`** → best PRs to spot-check manually.
- **Neither label is ground truth** — agreement means consistency, not correctness.
