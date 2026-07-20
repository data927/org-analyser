# org-analyser

Org/repo codebase analysis pipeline: merged-PR counts, PR task-profile,
codebase profiler, eval-kit, and sealed repo quality score — one command,
one or many repos, across GitHub, GitLab, Bitbucket, or local checkouts.

Subpackages: `analysis/` (PR counts, task-profile, vendor-CSV analyzer),
`profiler/` (codebase intake sheet), `eval/` (LLM repo evaluation),
`quality/` (sealed quality score), `mirror/` (org/group replication).

## Install

Needs Python 3.10+, `git`, `scc`, and Node.js/`npx`:

```bash
brew install git scc node                     # macOS
choco install git nodejs scc -y                # Windows (Chocolatey)
sudo apt-get install -y git nodejs npm         # Ubuntu/Debian — get scc from its releases page
```

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip                  # need pip>=21.3
pip install -e .
cp config.example.yml config.yml
```

## Configure

Edit `config.yml` → fill in `tokens:` for whichever platform(s) you use, and
optionally a default target so `org-analyser` runs with zero flags.
`config.yml` is gitignored; never commit real tokens.

| Token | Where to get it |
|---|---|
| `github-data-token` | [github.com/settings/tokens](https://github.com/settings/tokens) (classic, `repo` scope) |
| `gitlab_token` | Personal access token with `read_api` from your GitLab instance — [gitlab.com](https://gitlab.com/-/user_settings/personal_access_tokens) or your self-hosted **Profile → Access tokens** page |
| `bitbucket_token` | app password / access token / API token — see `profiler/README.md`; optional for public repos |
| `openai_key` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) — or use Azure below instead |
| `AZURE_OPENAI_*` | For Azure OpenAI (chat **and** batch): set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, and `AZURE_OPENAI_DEPLOYMENT` under `tokens:` instead of `openai_key` |

Set `gitlab_host` in `config.yml` (or pass `--gitlab-host`) when your repos
live on a **self-hosted GitLab** instance instead of gitlab.com. Use the
hostname only (`gitlab.example.com`) or a full base URL
(`https://gitlab.example.com`). Every GitLab phase — repo discovery, merged-PR
counts, cloning, PR task-profile, and eval — uses this host.

```yaml
gitlab_group: my-group          # or gitlab_project: ["my-group/repo-a"]
gitlab_host: gitlab.example.com # not gitlab.com
tokens:
  gitlab_token: glpat-...
```

Credentials are read from `config.yml` **only** — matching environment variables
(`OPENAI_API_KEY`, `AZURE_OPENAI_*`, etc.) are deliberately ignored, so keep
every key in `config.yml`.

Then validate before spending real time on a run:

```bash
org-analyser check --github-org <ORG_NAME>
org-analyser check --gitlab-group <GROUP> --gitlab-host gitlab.example.com
```

Checks tokens, tools, disk space, clone auth, and the LLM endpoint live, and
aborts with zero clones made if anything's wrong. For self-hosted GitLab, pass
the same `--gitlab-host` you use for the real run (or set `gitlab_host` in
`config.yml` so check picks it up automatically).

## Run

```bash
org-analyser --github-org <ORG_NAME> --workers 10        # whole org
org-analyser --github-repo <OWNER>/<REPO>                 # single repo
org-analyser --local-repos-dir ./repos --workers 4         # local checkouts
org-analyser --github-org <ORG_NAME> --skip-pr-task-profile # skip only the PR task-profile phase

# Self-hosted GitLab (set gitlab_host in config.yml to avoid repeating the flag)
org-analyser --gitlab-group <GROUP> --gitlab-host gitlab.example.com
org-analyser --gitlab-project <GROUP>/<PROJECT> --gitlab-host gitlab.example.com
```

Same flags work for `--gitlab-group`/`--gitlab-project` and
`--bitbucket-workspace`/`--bitbucket-repo`. Any flag can be set as a default
in `config.yml` instead — including `gitlab_host` for self-hosted instances.
`org-analyser run --help` lists everything else
(retries, clone depth, skip flags, etc.) — most runs don't need them.

## If a run fails

Every phase (per repo and org-level) is tracked in the run's `state.db`.
A failure never loses completed work and never aborts the whole run.

```bash
org-analyser status                 # what finished, what failed, in the latest run
org-analyser resume                 # redo only what failed/didn't finish
org-analyser retry --repo <owner/repo> --phase eval-kit --force   # redo one thing on purpose
```

All three default to the most recent run under `--output-dir`; pass a run
directory to target an older one.

## Outputs

Runs land under `outputs/org-analyser-runs/<run-name>/`: logs, `state.db`,
CSV/JSON/XLSX outputs, `manifest.json`, and (once every phase succeeds) a
zip archive. `FAILURES.md` appears in the run folder if anything failed.
Old run folders are pruned after `--retention-days` (default 90) — bundles
carry contributor names and per-author stats.

## CI

```bash
org-analyser check --github-org "$ORG" --tokens-file "$TOKENS" || exit 1
org-analyser --github-org "$ORG" --tokens-file "$TOKENS" --skip-quality-score --quiet
```

`--quiet` (`-q`) trims console output to the start line, the final summary,
and errors — full detail still goes to the run's `pipeline.log`, nothing is
lost. Exit code is `0` only if every repo/phase fully succeeded; `1`
otherwise (the run is still resumable — rerun the same command, or
`org-analyser resume --quiet`, to pick up only what's missing).

## Troubleshooting

- **`SSL: CERTIFICATE_VERIFY_FAILED`** — fixed via `certifi`; rerun after `pip install -e .`. Self-hosted GitLab with a private CA may still need your system trust store configured.
- **Auth / 404** — check the token's access and the `owner/repo` spelling; `org-analyser check` catches this before a real run.
- **GitLab 401 against `gitlab.com`** — your token is for a self-hosted instance but `gitlab_host` is still the default. Set `gitlab_host` in `config.yml` or pass `--gitlab-host gitlab.example.com`. The PR task-profile log line `host=gitlab.com` is the tell.
- **Config not picked up** — run from the repo root, or set `ORG_ANALYSER_CONFIG=/path/to/config.yml`.

## More docs

`ORG_PIPELINE_README.md` (full pipeline), `PR_TASK_PROFILE_README.md` (PR
classification), `SECURITY_AND_COMPLIANCE.md` (credential handling and
redaction model).
