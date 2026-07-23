# Running org-analyser from GitHub Actions

Run the pipeline in CI instead of on your laptop. It's manually triggered — you
pick the target and provider each time, and download the report when it's done.

Workflow file: [`org-analyser.yml`](org-analyser.yml).

---

## 0. Fork it (to run it yourself)

Repo: **<https://github.com/data927/org-analyser>**

1. **Fork** it to your own GitHub profile (top-right **Fork** button).
2. In your fork: **Actions** tab → **"I understand my workflows, go ahead and
   enable them"**. Actions are **disabled by default on forks** — this is required.
3. Add your own secrets (step 1 below). **Secrets never copy over on a fork** —
   each person adds their own.

Then run it (steps 2–3 below). To scan an org, your token must have access to it.

---

## 1. Add secrets (one-time)

Go to the repo → **Settings → Secrets and variables → Actions → Secrets tab →
New repository secret**. Add only the ones you need.

> Use **repository secrets** — *not* Environments. The workflow has no
> `environment:`, so environment secrets would be invisible to it.

| Secret | When you need it |
|---|---|
| `ORG_ANALYSER_GITHUB_TOKEN` | Any GitHub target. Classic PAT, **`repo`** scope, with access to the org/repos you're scanning. |
| `ORG_ANALYSER_OPENAI_KEY` | `llm_provider = openai` (and the base for `gemini`). |
| `ORG_ANALYSER_AZURE_OPENAI_ENDPOINT`<br>`ORG_ANALYSER_AZURE_OPENAI_API_KEY`<br>`ORG_ANALYSER_AZURE_OPENAI_DEPLOYMENT` | `llm_provider = azure-openai`. |
| `ORG_ANALYSER_OPENAI_API_VERSION` | Optional, Azure only. |
| `ORG_ANALYSER_GEMINI_KEY` | `llm_provider = gemini` (also needs `ORG_ANALYSER_OPENAI_KEY` as the base). |
| `ORG_ANALYSER_GITLAB_TOKEN` | GitLab targets (`read_api` PAT). |
| `ORG_ANALYSER_BITBUCKET_TOKEN` / `ORG_ANALYSER_BITBUCKET_USERNAME` | Bitbucket targets. |

Minimum for a GitHub run: `ORG_ANALYSER_GITHUB_TOKEN` + one LLM provider's secret(s).

---

## 2. Run it

Repo → **Actions** tab → pick **org-analyser** in the left sidebar →
**Run workflow** (top-right) → fill in the inputs → **Run workflow**.

> The "Run workflow" button only appears when the workflow is on the **default
> branch** (`main`). Merge changes to `main` before they take effect.

### Inputs

| Input | What to enter |
|---|---|
| **target_type** | `github-org` = whole org · `github-repo` = specific repos. (Plus GitLab/Bitbucket equivalents.) |
| **target_value** | For `-org`/`-group`/`-workspace`: the org name, e.g. `plan-ai`.<br>For `-repo`/`-project`: `owner/repo` paths, comma-separated, e.g. `plan-ai/app, plan-ai/api`. |
| **llm_provider** | `openai` (default), `azure-openai`, or `gemini`. Must match the secret(s) you added. |
| **gitlab_host** | Blank unless self-hosted GitLab (e.g. `gitlab.example.com`). |
| **extra_args** | Leave the default (`--workers 8`). Advanced flags go here. |

---

## 3. Find the output for a run

Repo → **Actions** tab → click the workflow run you want (each run is listed by
time/trigger) → scroll to the **Artifacts** section at the bottom of the run
summary → download **`org-analyser-report-<run_id>`**.

The zip contains `outputs/org-analyser-runs/<run-name>/`:
- CSV / JSON / XLSX reports and `manifest.json`
- `FAILURES.md` — any repo that failed or was skipped (e.g. one you can't access)

The artifact uploads **even if the run fails**, so you always get partial
results and `FAILURES.md`.

---

## Good to know

- **Green with failures is normal.** The job is marked ✅ if the run completed
  with at least one repo analysed; repos that failed/were skipped are listed in
  `FAILURES.md`. It only goes ❌ on a real abort (bad token, no repo reachable).
- **Inaccessible repos don't block the run.** A private repo the token can list
  but not clone is skipped and reported — not fatal. To *include* it, give the
  token access to that repo (there's no exclude flag).
- **6-hour cap.** GitHub-hosted runners kill a job at 6h. A full large org can
  exceed that — scope to specific repos, or use a self-hosted runner.
- **Re-running:** just dispatch again. Each run is independent and gets its own
  artifact.
