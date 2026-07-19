# Output, Sealing & Verification

This skill runs in a **third-party environment**. Two consequences shape the design:

1. The result must come back as a **downloadable file** with its full path surfaced.
2. Nothing computed there can be *trusted* on its own — a party that controls the
   machine controls both the output and any checksum the output carries. So integrity
   comes from **re-deriving the result on trusted infrastructure**, not from a digest
   the third party returns.

## What is (and isn't) protected

- **Tampering with results → detectable.** Re-running the deterministic pipeline on a
  trusted checkout at the recorded commit and diffing catches edited scores and
  fabricated signals (see "Verification" below).
- **Methodology leaking → NOT prevented.** A self-contained skill ships its
  class-weights and heuristics in cleartext; they are readable in the third-party
  environment. Hiding them would require server-side scoring, which this skill does
  not do. Treat the weights as visible; rely on re-derivation for trust, not secrecy.
- **Dishonest *judgment* scores** (the agent's per-dimension scores) are not fully
  re-derivable — they are judgment, not computation. They are checked by re-scoring
  with a trusted agent run and by spot-reading the embedded evidence strings. The
  signal-derived and arithmetic parts ARE re-derivable and are checked automatically.

## Output: the sealed bundle

After scoring, wrap the result with `scripts/seal.py` into a **sealed bundle** and
write it to an external results directory (default: the current working directory,
never inside the target repo). `seal.py` (and `score.py` / `aggregate_org.py` with
`--report`) print the **absolute path** as:

```
SEALED RESULT FILE (download this and send it back): /abs/path/to/<name>.sealed.json
  sha256: <hex>
```

Surface that exact path to the user in chat with a one-line "download this file and
send it back" instruction.

The bundle embeds everything needed to re-derive:
- `provenance` — repo identity + `head_sha` + commit count/dates (what to re-clone).
- `evidence` — the raw `repo_stats.py` / `git_stats.py` signals the score was built on.
- `result` — the scored output (per-dimension scores, weights, overall, mining_rank).
- `tool_version` — SHA-256 of the skill's scripts (detects a modified client).
- `integrity` — a SHA-256 over the canonical payload. **Change-detection only**, not a
  trust anchor (a client can recompute it after editing).

## Verification (run on TRUSTED infrastructure, not in the third-party env)

1. Re-clone the repo at `provenance[].head_sha` from your own trusted copy of the
   git bundle.
2. Run:
   ```
   python scripts/verify.py <returned-bundle.json> --repo <trusted-checkout>
   ```
   It performs four checks and prints a per-check PASS/FAIL table + a verdict:
   - **integrity.digest** — payload unchanged since sealing (catches naive edits).
   - **tool_version.scripts_digest** — client ran unmodified scripts (WARN if not).
   - **re-collection diff** — re-runs `repo_stats.py`/`git_stats.py` and diffs stable
     signals (LOC, file/test counts, god files, CI flags, secrets, committed `.env`,
     class signals, commit/author/tag stats, mineable-commit count, `head_sha`).
     Catches fabricated signals even when the digest was recomputed.
   - **score_math.overall_score** — recomputes the weighted score from the embedded
     per-dimension scores with THIS copy of the weights. Catches headline edits.
3. Exit code is non-zero if any check FAILs.

`recency_days` is intentionally excluded from the diff (it depends on wall-clock at
collection time); `last_commit` is compared instead, and `seal.py` records
`generated_at` so any time-derived value is reproducible.

### Worked tamper examples (both detected)
- *Naive*: edit `result.overall_score` → `integrity.digest` FAIL **and**
  `score_math.overall_score` FAIL.
- *Sophisticated*: hide a committed `.env` / shrink god-file counts in `evidence` and
  recompute the digest → `integrity.digest` PASSes but the **re-collection diff**
  FAILs (`env_files_committed`, `god_files_over_500_loc` disagree with the trusted
  re-run).

## Limits (state these honestly)
- Re-derivation needs a **trusted copy of the repo at the recorded commit**. Keep the
  delivered git bundles; verify against those.
- A client who edits internally-consistent **judgment** scores (and matches the
  headline) passes the automatic checks — catch this by re-scoring with a trusted
  agent run or auditing the evidence strings.
- `tool_version` and `integrity` are forgeable by a self-contained client; they are
  convenience/triage signals. The re-collection diff and score-math recompute are the
  load-bearing checks.
