# Heuristics & Detection Patterns

Reference for cross-language patterns used in the audit. Read the section(s) relevant to the repo being audited.

---

## Test file detection (any language)

Files are counted as test specs if they match any of:

| Pattern | Languages |
|---|---|
| `*.test.ts`, `*.test.tsx`, `*.test.js`, `*.test.jsx` | JS/TS |
| `*.spec.ts`, `*.spec.tsx`, `*.spec.js`, `*.spec.jsx` | JS/TS |
| `test_*.py`, `*_test.py` | Python |
| `*_test.go` | Go |
| `*Test.java`, `*Tests.java`, `*Test.kt` | Java/Kotlin |
| `*_spec.rb`, `*_test.rb` | Ruby |
| `*Test.php`, `*Test.cs` | PHP/C# |
| `*.test.rs` (inline `#[cfg(test)]` modules) | Rust |
| Any file under `__tests__/`, `tests/`, `spec/`, `test/` | Universal |

**Fixtures and snapshots** (exclude from spec count):
- Files under `tests/fixtures/`, `tests/__snapshots__/`, `__snapshots__/`
- Files with > 5000 LOC that consist mostly of array/object literals
- Auto-generated test data files (`.snap`, `.fixture.json`, scenario data files)

Always report both: "N total test files, M specs after excluding K fixture/snapshot files."

---

## Source file detection (any language)

Source files are code files that are not tests, configs, lockfiles, or generated. The script handles this, but manual inspection should exclude:
- `*.min.js`, `*.bundle.js` (minified/bundled)
- Files under `vendor/`, `node_modules/`, `.cache/`, `dist/`, `build/`, `out/`
- Auto-generated files (look for `// Code generated` or `# DO NOT EDIT` at top)
- Lockfiles: `*.lock`, `go.sum`, `package-lock.json`, etc.

---

## Language and framework detection

Look for these files to identify the primary stack:

| File / Pattern | Language / Framework |
|---|---|
| `package.json` + `tsconfig.json` | TypeScript/Node.js |
| `package.json` + `*.jsx` files | JavaScript/React |
| `next.config.*` | Next.js |
| `vite.config.*` | Vite (React/Vue/Svelte) |
| `nuxt.config.*` | Nuxt (Vue) |
| `svelte.config.*` | SvelteKit |
| `angular.json` | Angular |
| `pyproject.toml` or `setup.py` or `setup.cfg` | Python |
| `requirements.txt` | Python (basic) |
| `manage.py` | Django |
| `app.py` or `wsgi.py` or `asgi.py` or `fastapi` in deps | Flask/FastAPI |
| `go.mod` | Go |
| `Cargo.toml` | Rust |
| `pom.xml` | Java (Maven) |
| `build.gradle` | Java/Kotlin (Gradle) |
| `Gemfile` | Ruby |
| `composer.json` | PHP |
| `*.csproj` or `*.sln` | .NET/C# |
| `pubspec.yaml` | Dart/Flutter |
| `mix.exs` | Elixir |
| `rebar.config` | Erlang |

Identify the **primary language** (by LOC), secondary languages, and key frameworks. List all detected frameworks in the metadata section.

---

## Dependency count heuristics (without installing)

### JavaScript/TypeScript
```bash
# Direct production deps
cat package.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('dependencies',{})))"
# Direct dev deps
cat package.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('devDependencies',{})))"
# Total lockfile entries (transitive)
grep -c '"resolved"' package-lock.json 2>/dev/null || grep -c '^"' yarn.lock 2>/dev/null || grep -c '^/' pnpm-lock.yaml 2>/dev/null
```

### Python
```bash
# Count pinned deps in requirements.txt
grep -c '.' requirements.txt 2>/dev/null
# Or from pyproject.toml
grep -c '^\s' pyproject.toml 2>/dev/null  # rough
# From poetry.lock entries
grep -c '^name = ' poetry.lock 2>/dev/null
```

### Go
```bash
# Direct deps in go.mod
grep -c '^\s' go.mod 2>/dev/null
# Total transitive (go.sum has two lines per dep)
wc -l < go.sum 2>/dev/null | awk '{print int($1/2)}'
```

### Rust
```bash
grep -c '^\[\[package\]\]' Cargo.lock 2>/dev/null
```

---

## Lockfile detection table

| Language | Lockfile | Package manager |
|---|---|---|
| JavaScript | `package-lock.json` | npm |
| JavaScript | `yarn.lock` | Yarn |
| JavaScript | `pnpm-lock.yaml` | pnpm |
| Python | `poetry.lock` | Poetry |
| Python | `Pipfile.lock` | Pipenv |
| Python | `uv.lock` | uv |
| Go | `go.sum` | Go modules |
| Rust | `Cargo.lock` | Cargo |
| Ruby | `Gemfile.lock` | Bundler |
| PHP | `composer.lock` | Composer |
| .NET | `packages.lock.json` | NuGet |
| Java | `gradle.lockfile` | Gradle |

---

## External service / SDK detection

Grep source files (not test files) for these import patterns to identify external service usage:

| Service | JavaScript/TypeScript | Python |
|---|---|---|
| AWS | `from '@aws-sdk/`, `from 'aws-sdk'` | `import boto3`, `from boto3` |
| GCP | `from '@google-cloud/` | `from google.cloud` |
| Azure | `from '@azure/` | `from azure` |
| OpenAI | `from 'openai'`, `openai.` | `from openai`, `import openai` |
| Anthropic | `from '@anthropic-ai/'` | `from anthropic` |
| Stripe | `from 'stripe'` | `from stripe` |
| Twilio | `from 'twilio'` | `from twilio` |
| SendGrid | `from '@sendgrid/'` | `from sendgrid` |
| Slack | `from '@slack/'` | `from slack_sdk` |
| Firebase | `from 'firebase'` | `from firebase_admin` |
| Supabase | `from '@supabase/'` | `from supabase` |
| PostgreSQL | `from 'pg'`, `from 'postgres'` | `import psycopg`, `from sqlalchemy` |
| Redis | `from 'redis'`, `from 'ioredis'` | `from redis` |
| MongoDB | `from 'mongoose'`, `from 'mongodb'` | `from pymongo` |
| Auth0 | `from 'auth0'`, `from '@auth0/'` | `from auth0` |
| Sentry | `from '@sentry/'` | `from sentry_sdk` |

For Go, Rust, Java — grep for the package import path in the relevant module files.

---

## Security signal patterns (grep in non-test source files)

These patterns are not definitive proof of an issue but warrant inspection:

```bash
# Potential hardcoded secrets (adjust for language)
grep -rn --include="*.ts" --include="*.js" --include="*.py" \
  -E '(sk-[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{36}|password\s*=\s*["\x27][^"\x27]{3,}|api[_-]?key\s*[:=]\s*["\x27][^"\x27]{10,})' \
  --exclude-dir={node_modules,.git,vendor,dist,build} .

# Committed .env files (not .env.example)
find . -name ".env" -not -name ".env.example" -not -name ".env.test" -not -path "*/.git/*"

# SQL-injection risk: string concatenation in queries
grep -rn --include="*.ts" --include="*.js" --include="*.py" \
  -E '(query\s*\+|execute\s*\(.*\+|raw\s*\()' .
```

---

## File size and complexity heuristics

From `repo_stats.py` output:
- **God file threshold**: > 500 LOC for source files (> 1000 LOC is severe)
- **Healthy average**: < 200 LOC per source file
- **Complexity proxy**: number of functions per file (grep `^(export )?function|def |func ` — files with > 20 functions are candidates for splitting)

Directory depth heuristic: `find . -type d | awk -F/ '{print NF}' | sort -n | tail -5` — repos deeper than 6–7 levels in their source tree tend to have modularity issues.

---

## Observability pattern detection

```bash
# Structured logging libs (JS)
grep -rn "pino\|winston\|bunyan\|loglevel\|log4js" package.json

# Structured logging libs (Python)
grep -rn "structlog\|loguru\|logging.getLogger" requirements.txt pyproject.toml

# Error tracking
grep -rn "@sentry/\|sentry-sdk\|rollbar\|honeybadger\|bugsnag" package.json requirements.txt

# Health endpoints
grep -rn '"/health"\|"/ping"\|"/readiness"\|"/liveness"' --include="*.ts" --include="*.js" --include="*.py" .

# Prometheus/metrics
grep -rn "prom-client\|prometheus_client\|statsd" package.json requirements.txt
```

---

## CI/CD config locations

Check all of these:
- `.github/workflows/*.yml` (GitHub Actions)
- `.circleci/config.yml`
- `.gitlab-ci.yml`
- `Jenkinsfile`
- `azure-pipelines.yml`
- `.travis.yml`
- `bitbucket-pipelines.yml`
- `.buildkite/pipeline.yml`
- `cloudbuild.yaml` (GCP Cloud Build)
- `.woodpecker.yml`

---

## Class-specialist detection (for classification)

`repo_stats.py`'s `class_signals` block already collects most of this (dependency
keyword hits, notebook/terraform/k8s/SQL/UI-component counts). Use these patterns
when confirming or overriding `classify_repo.py`.

| Class | Tell-tale deps / files | Tell-tale dirs / patterns |
|---|---|---|
| frontend | react, vue, svelte, @angular, next, nuxt, tailwindcss, @mui | `components/`, `pages/`, `.tsx/.vue/.svelte`, high CSS ratio |
| backend | express, fastapi, flask, django, @nestjs, gin, spring; ORM (prisma, sqlalchemy, gorm) | `routes/`, `controllers/`, `services/`, `handlers/`, migrations |
| ml | torch, tensorflow, jax, scikit-learn, transformers, keras, xgboost | `train.py`, `models/`, `checkpoints/`, `.ipynb` |
| ai_research | + wandb, mlflow, hydra-core, accelerate, deepspeed | `experiments/`, `ablations/`, `notebooks/`, `.tex`, results tables in README |
| data_engineering | airflow, dagster, prefect, dbt, pyspark, kafka, great-expectations, pandera | `dags/`, `models/` (dbt), `pipelines/`, heavy `.sql` |
| security | bandit, semgrep, trufflehog, nuclei, scapy, pwntools, cryptography, impacket | scanners, `exploits/`, `payloads/`, CTF writeups, `SECURITY.md` |
| infra | Terraform (`.tf`), Pulumi (`Pulumi.yaml`), CloudFormation, kubernetes, ansible | `*.tf`, K8s manifests (`apiVersion:`+`kind:`), `Chart.yaml`, `playbooks/`, `modules/` |

ml vs ai_research: production/serving emphasis → ml; experiments/reproduction
emphasis → ai_research. fullstack: strong frontend **and** backend in one cohesive
app. See `classification.md`.
