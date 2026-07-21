#!/usr/bin/env python3
"""
git_stats.py — extract commit / author / bot / conventional-commit / tag stats
from a git repository, for the repo-quality-score skill.

Usage:
    python git_stats.py <repo-path> [--limit N]

Positional arguments:
    repo-path           Path to the git repository to analyze.

Options:
    --limit N           How many recent commits to scan for the per-commit
                        feature/test classification (default 200). Repo-wide
                        stats (commit count, authors, tags, recency) always cover
                        full history regardless of this value.

Environment variables: none. This script reads no secrets and no credentials.

Emits JSON to stdout. Uses only `git` (invoked with fixed argument lists, never a
shell) and the Python standard library. Read-only: it never modifies the repo and
never hits the network.

The output feeds the host-agnostic activity/maintenance signals the skill scores:
total commits, human vs bot contributors, recency/staleness in days, repo span,
conventional-commit rate, and tags/releases count. PR/review data is deliberately
NOT collected — it is GitHub-specific and would not work for GitLab/Bitbucket
repos; commit and tag counts are the host-agnostic activity proxy instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BOT_NAME_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"\bdependabot\b",
        r"\brenovate\b",
        r"\bsnyk-bot\b",
        r"\bgithub-actions\b",
        r"\bmergify\b",
        r"\bpre-commit-ci\b",
        r"\bwhitesource\b",
        r"\bgreenkeeper\b",
        r"\bimgbot\b",
        r"\b\[bot\]\b",
    ]
]

CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)(\([^)]+\))?!?:\s",
    re.I,
)

# Subjects that signal "this is not a mineable change" regardless of file shape.
# Catches chore/revert/wip/dependency-bump/cleanup commits that incidentally
# touch a few code files. The pindrop iter-2 false positive was
# "Removed unnecessary ebextensions" — chore work that happened to look atomic.
NOISE_SUBJECT_RE = re.compile(
    r"^(chore|revert|wip|bump|deps?|dependabot|lint|style|format|prettier|typo|"
    r"merge|release|version|removed?|cleanup|cleanups?|delete|remove|rename|"
    r"upgrade|update\s+(deps|dep|dependenc|package|lock))[\s:(\[]",
    re.I,
)
# Subjects that signal a feature commit. We deliberately exclude bare "test"
# (it matches noise like "test push") — only the conventional-commit forms
# `test:` or `test(scope)` count, and they tend to be Class A or B anyway.
FEATURE_SUBJECT_RE = re.compile(
    r"^(feat|add|implement|introduce|support)\b|^test[:(]",
    re.I,
)

TEST_FILE_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"\.(test|spec)\.(ts|tsx|js|jsx|mjs)$",
        r"(^|/)(test_|tests/)",
        r"_test\.(go|py|rb)$",
        r"(^|/)__tests__/",
        r"(^|/)spec/",
        r"\.spec\.rb$",
        r"Test\.(java|kt|cs)$",
        r"Tests\.(java|kt|cs)$",
    ]
]

# Test-related infrastructure (configs, setup, fixtures, helpers). These are
# NEITHER test specs (they don't contribute to test_file_count for class B
# decomposition) NOR implementation (they don't justify Class A on their own).
# Keeping them out of impl is what matters most — so a commit that adds 9
# test specs + 2 vitest config files isn't mis-classified as Class A.
TEST_INFRA_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)vitest[^/]*\.config\.(ts|js|mjs)$",
        r"(^|/)jest\.config\.(ts|js|mjs|cjs|json)$",
        r"(^|/)playwright\.config\.(ts|js|mjs)$",
        r"(^|/)cypress\.config\.(ts|js)$",
        r"(^|/)karma\.conf\.(ts|js)$",
        r"(^|/)tests?/setup/",
        r"(^|/)tests?/fixtures/",
        r"(^|/)tests?/helpers/",
        r"(^|/)tests?/__mocks__/",
        r"(^|/)tests?/conftest\.py$",
        r"(^|/)conftest\.py$",
        r"(^|/)pytest\.ini$",
        r"(^|/)tox\.ini$",
        r"(^|/)\.mocharc\.(js|cjs|json|yml|yaml)$",
    ]
]

SCHEMA_FILE_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)schemas?/",
        r"(^|/)types?/",
        r"\.schema\.(ts|js|py)$",
        r"(^|/)migrations?/",
        r"(^|/)drizzle/",
        r"(^|/)prisma/",
        r"\.proto$",
        r"openapi\.(ya?ml|json)$",
    ]
]

DOCS_OR_CONFIG_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"\.md$",
        r"^\.github/",
        r"^\.gitlab/",
        r"\.ya?ml$",
        r"^Dockerfile",
        r"package(-lock)?\.json$",
        r"pnpm-lock\.yaml$",
        r"yarn\.lock$",
        r"poetry\.lock$",
        r"Cargo\.lock$",
    ]
]


def run_git(repo: Path, *args: str) -> str:
    """Run a git command in the given repo and return stdout (text)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        # Surface git errors as empty output rather than crashing — caller decides.
        sys.stderr.write(f"git error: {e.stderr}\n")
        return ""


def is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists() or run_git(repo, "rev-parse", "--git-dir").strip() != ""


def is_bot_author(name: str, email: str) -> bool:
    haystack = f"{name} {email}"
    return any(p.search(haystack) for p in BOT_NAME_PATTERNS)


def pseudonymize(email: str) -> str:
    """Stable pseudonym for a committer address.

    Same address always yields the same id, so contributor counts and
    cross-repo joins are unchanged; the address itself never leaves the host.
    """
    if not email:
        return ""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"anon:{digest[:16]}"


def matches_any(path: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(path) for p in patterns)


def classify_files(files: list[str]) -> dict:
    """Classify a commit's files into test/test-infra/impl/schema/docs buckets.

    Order matters: test specs win over test-infra (some config files match
    both patterns); both win over impl. Schema files are tagged for the
    has_schema signal but still counted as impl for the size check.
    """
    test_files = [f for f in files if matches_any(f, TEST_FILE_PATTERNS)]
    test_infra = [
        f
        for f in files
        if f not in test_files and matches_any(f, TEST_INFRA_PATTERNS)
    ]
    schema_files = [f for f in files if matches_any(f, SCHEMA_FILE_PATTERNS)]
    docs_or_config = [f for f in files if matches_any(f, DOCS_OR_CONFIG_PATTERNS)]
    impl_files = [
        f
        for f in files
        if not matches_any(f, TEST_FILE_PATTERNS)
        and not matches_any(f, TEST_INFRA_PATTERNS)
        and not matches_any(f, DOCS_OR_CONFIG_PATTERNS)
    ]
    return {
        "test_files": test_files,
        "test_infra_files": test_infra,
        "schema_files": schema_files,
        "docs_or_config_files": docs_or_config,
        "impl_files": impl_files,
    }


def directory_diversity(files: list[str]) -> int:
    """Count distinct top-level directories touched."""
    return len({f.split("/", 1)[0] for f in files if "/" in f})


def parse_commits(repo: Path, limit: int) -> list[dict]:
    """Get the last `limit` commits with full metadata + numstat."""
    fmt = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s"
    raw = run_git(
        repo,
        "log",
        f"-{limit}",
        f"--pretty=format:{fmt}",
        "--numstat",
        "--no-merges",
    )
    if not raw:
        return []

    commits = []
    current = None
    for line in raw.splitlines():
        if not line:
            continue
        if "\x1f" in line:
            if current:
                commits.append(current)
            sha, abbrev, author_name, author_email, iso_date, subject = line.split("\x1f", 5)
            current = {
                "sha": sha,
                "abbrev": abbrev,
                "author_name": author_name,
                "author_email": author_email,
                "date": iso_date,
                "subject": subject,
                "files": [],
                "additions": 0,
                "deletions": 0,
            }
        else:
            # numstat line: "<add>\t<del>\t<path>"
            parts = line.split("\t")
            if len(parts) == 3 and current is not None:
                add, dele, path = parts
                # binary files show "-\t-\t"
                try:
                    a = int(add) if add != "-" else 0
                    d = int(dele) if dele != "-" else 0
                except ValueError:
                    a, d = 0, 0
                current["additions"] += a
                current["deletions"] += d
                current["files"].append(path)
    if current:
        commits.append(current)
    return commits


def score_atomic_feature(commit: dict) -> dict:
    """Classify a commit as Class A (strict atomic feature+test), Class B (bulk
    test-add — decomposable), Class C-pre (impl-only feature, candidate for
    pairing with a later test commit — needs HEAD-tree verification by the
    agent), or noise. Returns the classification plus the `latent_tasks` count.

    Class C is "pre" here because git_stats can't see HEAD's test tree to
    confirm the feature is actually covered. The agent must verify before
    counting C-pre commits as real Class C tasks."""
    files = commit["files"]
    classified = classify_files(files)
    diversity = directory_diversity(files)
    is_bot = is_bot_author(commit["author_name"], commit["author_email"])
    subject = commit["subject"]
    is_noise_subject = bool(NOISE_SUBJECT_RE.match(subject))
    is_feature_subject = bool(FEATURE_SUBJECT_RE.match(subject))

    test_count = len(classified["test_files"])
    impl_count = len(classified["impl_files"])
    has_schema = len(classified["schema_files"]) > 0
    small_enough = len(files) <= 30
    focused = diversity <= 3
    not_bot = not is_bot
    not_noise = not is_noise_subject  # subject-line negative filter

    # Class A: strict atomic feature+test
    is_class_a = (
        small_enough
        and focused
        and test_count >= 1
        and impl_count >= 1
        and not_bot
        and not_noise
    )

    # Class B: bulk test-add (≥5 tests, <3 impl). Class A and B are exclusive.
    is_class_b = (
        not is_class_a
        and test_count >= 5
        and impl_count < 3
        and not_bot
        and not_noise
    )

    # Class C-pre: impl-only feature commit (looks like a feature added without
    # tests — typical "ship now, test later" pattern). Needs the agent to
    # verify a corresponding test exists in HEAD before counting toward yield.
    is_class_c_pre = (
        not is_class_a
        and not is_class_b
        and small_enough
        and focused
        and impl_count >= 1
        and test_count == 0
        and is_feature_subject
        and not_bot
    )

    if is_class_a:
        klass = "A"
        latent_tasks = 1
    elif is_class_b:
        klass = "B"
        latent_tasks = min(test_count, 15)
    elif is_class_c_pre:
        klass = "C-pre"
        latent_tasks = 1  # provisional; agent confirms or zeros it out
    else:
        klass = None
        latent_tasks = 0

    qualifies = klass is not None

    confidence = sum([qualifies, is_feature_subject, has_schema])

    return {
        "qualifies": qualifies,
        "klass": klass,
        "latent_tasks": latent_tasks,
        "confidence": confidence,
        "small_enough": small_enough,
        "focused": focused,
        "has_tests": test_count > 0,
        "has_impl": impl_count > 0,
        "has_schema": has_schema,
        "is_bot": is_bot,
        "is_noise_subject": is_noise_subject,
        "is_feature_subject": is_feature_subject,
        "diversity": diversity,
        "test_file_count": test_count,
        "impl_file_count": impl_count,
    }


def aggregate_repo_stats(repo: Path) -> dict:
    """Repo-wide stats: total commits, authors, dates, bot ratio, conventional rate."""
    total = run_git(repo, "rev-list", "--count", "HEAD").strip()
    total_commits = int(total) if total.isdigit() else 0

    # --max-count is applied BEFORE --reverse, so we can't combine them. Use the
    # rev-list root-finder instead, which is exact and cheap.
    root_sha = run_git(repo, "rev-list", "--max-parents=0", "HEAD").strip().splitlines()
    first = ""
    if root_sha:
        first = run_git(repo, "log", "-1", "--pretty=format:%aI", root_sha[0]).strip()
    last = run_git(repo, "log", "-1", "--pretty=format:%aI").strip()

    # Authors with counts and emails (for bot detection)
    shortlog = run_git(repo, "shortlog", "-sne", "HEAD")
    authors = []
    for line in shortlog.splitlines():
        line = line.strip()
        if not line:
            continue
        # format: "  <count>\t<name> <email>"
        m = re.match(r"^\s*(\d+)\s+(.*?)\s+<(.+)>\s*$", line)
        if not m:
            continue
        count, name, email = m.groups()
        authors.append({
            "name": name,
            "email": email,
            "commits": int(count),
            "is_bot": is_bot_author(name, email),
        })

    human_authors = [a for a in authors if not a["is_bot"]]
    bot_authors = [a for a in authors if a["is_bot"]]
    bot_commit_count = sum(a["commits"] for a in bot_authors)
    bot_ratio = bot_commit_count / total_commits if total_commits else 0.0

    # Conventional-commit rate over last 200 commits
    last_subjects = run_git(repo, "log", "-200", "--pretty=format:%s").splitlines()
    conv = sum(1 for s in last_subjects if CONVENTIONAL_RE.match(s))
    conv_rate = conv / len(last_subjects) if last_subjects else 0.0

    # Tags / releases. A tag is the host-agnostic proxy for a "release" — it works
    # the same on GitHub, GitLab, and Bitbucket. We report the count and the most
    # recent tag so the skill can score release/versioning discipline.
    tag_lines = [t for t in run_git(repo, "tag", "--list").splitlines() if t.strip()]
    latest_tag = run_git(
        repo, "describe", "--tags", "--abbrev=0"
    ).strip() or None
    semver_tags = sum(
        1 for t in tag_lines if re.match(r"^v?\d+\.\d+", t.strip())
    )

    # Recency in days
    recency_days = None
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            now = datetime.now(timezone.utc)
            recency_days = (now - last_dt.astimezone(timezone.utc)).days
        except ValueError:
            pass

    # Span in days
    span_days = None
    if first and last:
        try:
            first_dt = datetime.fromisoformat(first)
            last_dt = datetime.fromisoformat(last)
            span_days = (last_dt - first_dt).days
        except ValueError:
            pass

    head_sha = run_git(repo, "rev-parse", "HEAD").strip() or None

    # "Burst copy" fingerprint: a few commits, made in a <=2-day window, with no
    # merges and <=2 authors is the shape of a scaffolded/demo repo that was
    # created and abandoned rather than developed. Fed into demo detection
    # (score.py) alongside static name/README signals from repo_stats.py.
    mc = run_git(repo, "rev-list", "HEAD", "--min-parents=2", "--count").strip()
    merge_commit_count = int(mc) if mc.isdigit() else 0
    looks_like_burst_copy = bool(
        0 < total_commits <= 12
        and span_days is not None and span_days <= 2
        and merge_commit_count == 0
        and len(human_authors) <= 2
    )

    return {
        "head_sha": head_sha,
        "total_commits": total_commits,
        "first_commit": first or None,
        "last_commit": last or None,
        "span_days": span_days,
        "recency_days": recency_days,
        "merge_commit_count": merge_commit_count,
        "looks_like_burst_copy": looks_like_burst_copy,
        "human_authors": len(human_authors),
        "bot_authors": len(bot_authors),
        "bot_commit_count": bot_commit_count,
        "bot_commit_ratio": round(bot_ratio, 4),
        "conventional_rate_last_200": round(conv_rate, 4),
        "tag_count": len(tag_lines),
        "semver_tag_count": semver_tags,
        "latest_tag": latest_tag,
        # author_id, not email. Bot detection already ran against the real
        # address (is_bot below), and nothing downstream reads the address
        # itself -- so emitting it would put developer PII in the deliverable
        # for no analytical gain. The hash is stable, so authors can still be
        # deduplicated and joined across repos and across runs.
        "top_authors": [
            {
                "name": a["name"],
                "author_id": pseudonymize(a["email"]),
                "commits": a["commits"],
                "is_bot": a["is_bot"],
            }
            for a in authors[:15]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract git history stats for repo-quality-score.")
    parser.add_argument("repo", help="Path to the git repository")
    parser.add_argument("--limit", type=int, default=200, help="How many recent commits to analyze (default 200)")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.exists():
        print(json.dumps({"error": f"path not found: {repo}"}))
        return 1
    if not is_git_repo(repo):
        print(json.dumps({"error": "not a git repository", "path": str(repo)}))
        return 1

    stats = aggregate_repo_stats(repo)
    commits = parse_commits(repo, args.limit)

    analyzed = []
    for c in commits:
        cls = score_atomic_feature(c)
        analyzed.append({
            "sha": c["abbrev"],
            "full_sha": c["sha"],
            "subject": c["subject"],
            "author_name": c["author_name"],
            "date": c["date"],
            "files_changed": len(c["files"]),
            "additions": c["additions"],
            "deletions": c["deletions"],
            **cls,
        })

    class_a = [c for c in analyzed if c.get("klass") == "A"]
    class_b = [c for c in analyzed if c.get("klass") == "B"]
    class_c_pre = [c for c in analyzed if c.get("klass") == "C-pre"]

    # The "confident" candidate count uses A and B only — C-pre is provisional
    # and the agent must confirm by checking HEAD's test tree before counting.
    confirmed_candidates = sum(
        c.get("latent_tasks", 0) for c in analyzed if c.get("klass") in ("A", "B")
    )
    # Provisional includes C-pre at face value (will be discounted by the agent).
    provisional_candidates = confirmed_candidates + len(class_c_pre)

    output = {
        "schema_version": "3.0",
        "repo_path": str(repo),
        "repo_stats": stats,
        "analyzed_commits": len(analyzed),
        "class_a_count": len(class_a),
        "class_b_count": len(class_b),
        "class_c_pre_count": len(class_c_pre),
        "confirmed_candidate_count": confirmed_candidates,
        "provisional_candidate_count": provisional_candidates,
        "class_a_commits": class_a[:50],
        "class_b_commits": class_b[:50],
        "class_c_pre_commits": class_c_pre[:50],
        "all_recent_commits": analyzed,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
