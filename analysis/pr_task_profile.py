#!/usr/bin/env python3
"""
================================================================================
GitHub Pull Request Task-Profile Report
================================================================================

Classifies every merged pull request in a GitHub repository into one of four
task profiles, using TWO independent methods and reporting them side by side:

    1. Rules    - a deterministic rulebook (fast, consistent, transparent)
    2. LLM      - a language model judging the same extracted signals (nuanced)

Task profiles
-------------
    - simple_fix             : 1-2 files, no meaningful human discussion
                               (config changes, dependency bumps, typo fixes)
    - standard_feature_work  : 3-10 files, typically touches tests, normal review
    - rich_task              : linked issue + substantive human review
                               (most likely high-value human work)
    - other                  : does not cleanly fit the above
    - automated              : bot-authored PRs (set aside before counting)

Targets (what to scan)
----------------------
    Any mix of the following can be passed; all are de-duplicated:
    - a single repo            --repo owner/name
    - multiple repos           --repo owner/a,owner/b   (or repeat --repo)
    - every repo of an owner   --repo owner             (org OR user, auto-detected)
    - every repo of an org     --org my-org             (repeatable / comma-separated)
    - every repo of a user     --user my-handle         (repeatable / comma-separated)

Deliverables (written to a per-run directory: <output-dir>/<run_id>/)
--------------------------------------------------------------------
    org_summary.csv           : one row per repo (LLM + rules task-profile percentages)
    org_summary.json          : same repo-level data + org totals + metadata
    <run_id>.log              : detailed run log (every repo, PR, API page, retry, failures)
    failures.json             : written only when repos fail (fetch or classification errors)
    <run_id>.zip              : archive of org_summary csv/json, log, and failures (if any)
    combined_report.json      : metadata + combined summary + per-repo summaries + all PRs
    combined_per_pr.csv       : every PR across all repos (with a repository column)
    repos/<repo>.json         : per-repo full report
    repos/<repo>.csv          : per-repo PR table

Requirements
------------
    Python 3.9+
    pip install requests openai python-dotenv

Environment (loaded from .env automatically if present)
-------------------------------------------------------
    GITHUB_TOKEN     required  - classic or fine-grained token with repo read
    OPENAI_API_KEY   required  - the LLM pass is part of the pipeline

Examples
--------
    pr-task-profile --repo your-org/example-repo
    pr-task-profile --repo owner/a,owner/b
    pr-task-profile --org your-org --model gpt-4o
    pr-task-profile --user octocat --no-forks

--------------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import zipfile
import urllib.parse
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from platforms.base import request_with_retry
from platforms.bitbucket import bitbucket_headers
from platforms.bitbucket import paginate as bitbucket_platform_paginate
from platforms.github import github_headers
from platforms.github import paginate as github_platform_paginate
from platforms.gitlab import gitlab_headers
from platforms.gitlab import paginate as gitlab_platform_paginate

# Shared redacting OpenAI client. This script sends PR titles, bodies and human
# review comments, so it must not construct a bare OpenAI() client.
from llm.batch import DEFAULT_BATCH_THRESHOLD, BatchItem, BatchItemResult, run_batch_or_sync
from llm.llm_safety import llm_available, safe_openai

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Metadata / constants
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "1.2.1"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_URL = "https://api.github.com"
GITLAB_REST_URL = "https://gitlab.com/api/v4"
BITBUCKET_REST_URL = "https://api.bitbucket.org/2.0"
DEFAULT_MODEL = "gpt-4o-mini"
CATEGORIES = ["simple_fix", "standard_feature_work", "rich_task", "other"]

logger = logging.getLogger("pr_task_profile")


BOT_LOGINS = {
    "dependabot",
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "pre-commit-ci[bot]",
    "semantic-release-bot",
    "vercel[bot]",
    "netlify[bot]",
    "snyk-bot",
    "snyk[bot]",
    "codecov[bot]",
    "deepsource-autofix[bot]",
}

TEST_PATH_HINTS = (
    "/test/",
    "/tests/",
    "__tests__/",
    ".spec.",
    ".test.",
    "test_",
    "_test.",
    "pytest",
    "jest",
    "cypress",
)

GENERATED_PATH_HINTS = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pipfile.lock",
    "go.sum",
    "go.mod",
    "cargo.lock",
    ".generated.",
    ".pb.go",
    ".pb.ts",
    ".pb.js",
    "/dist/",
    "/build/",
    "/vendor/",
    "/generated/",
    "schema.generated",
)

TRIVIAL_COMMENT_EXACT = {
    "lgtm",
    "looks good",
    "approved",
    "ship it",
    "+1",
    "thanks",
    "done",
    "fixed",
    "nit",
    "rebase",
    "rebase please",
    "please rebase",
    "fix lint",
}

SUBSTANTIVE_TERMS = (
    "because",
    "edge case",
    "race condition",
    "security",
    "latency",
    "performance",
    "rollback",
    "migration",
    "data loss",
    "backward compatibility",
    "test coverage",
    "what happens",
    "why",
    "should we",
    "can we avoid",
    "null",
    "concurrency",
    "retry",
    "timeout",
    "api contract",
    "breaking change",
    "deadlock",
    "transaction",
    "idempotent",
    "cache",
    "scalability",
    "memory",
    "query plan",
    "index",
)

LINKED_ISSUE_REGEX = re.compile(
    r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+"
    r"(([\w.-]+/[\w.-]+)?#\d+)",
    re.IGNORECASE,
)

GRAPHQL_QUERY = """
query MergedPRs($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: MERGED,
      first: $pageSize,
      after: $cursor,
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        bodyText
        url
        createdAt
        mergedAt
        changedFiles
        additions
        deletions
        totalCommentsCount
        author { login __typename }
        labels(first: 20) { nodes { name } }
        commits { totalCount }
        files(first: 60) {
          pageInfo { hasNextPage }
          nodes { path }
        }
        closingIssuesReferences(first: 10) {
          nodes { number title url }
        }
        comments(first: 25) {
          pageInfo { hasNextPage }
          nodes { bodyText createdAt author { login __typename } }
        }
        reviews(first: 25) {
          pageInfo { hasNextPage }
          nodes {
            state
            bodyText
            createdAt
            author { login __typename }
          }
        }
        reviewThreads(first: 30) {
          pageInfo { hasNextPage }
          nodes {
            isResolved
            comments(first: 20) {
              pageInfo { hasNextPage }
              nodes { bodyText path createdAt author { login __typename } }
            }
          }
        }
      }
    }
  }
}
"""

LLM_SYSTEM_PROMPT = """You classify merged GitHub pull requests into exactly one task profile.

Categories (choose exactly one):
- simple_fix: 1-2 files, no meaningful human discussion. Config changes, dependency
  bumps, typo fixes, tiny mechanical edits.
- standard_feature_work: 3-10 files, usually touches tests, may have a linked issue.
  Normal implementation work without a deep design/review conversation.
- rich_task: has a linked issue AND substantive human review (real back-and-forth
  about correctness, edge cases, design, trade-offs). Most likely human-authored,
  high-value work.
- other: does not cleanly fit the three above (e.g. large mechanical/generated
  changes, lockfile-heavy churn, or ambiguous PRs).

Rules:
- Judge using BOTH PR size and discussion richness. They are independent axes;
  a small PR can still be a rich_task if there is a linked issue and substantive review.
- "Substantive review" means review comments that discuss correctness, edge cases,
  security, performance, design, or request real changes. "LGTM", "nit", "rebase",
  "thanks" are NOT substantive.
- Ignore bot accounts; they are pre-filtered out of the counts you receive.
- Be conservative about rich_task: require a linked issue and genuinely substantive
  discussion, not just a high comment count.

Respond ONLY with strict JSON, no prose:
{"category": "<one of simple_fix|standard_feature_work|rich_task|other>",
 "confidence": "<high|medium|low>",
 "reason": "<one short sentence>"}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(log_path: Path, verbose: bool) -> None:
    """Console (INFO, or DEBUG if --verbose) + file (always DEBUG)."""
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


# ─────────────────────────────────────────────────────────────────────────────
# GitHub fetch (with retry/backoff)
# ─────────────────────────────────────────────────────────────────────────────

def _wait_for_rate_limit(response: requests.Response, attempt: int, default: int = 60) -> int:
    reset = response.headers.get("X-RateLimit-Reset")
    if reset and str(reset).isdigit():
        wait = int(reset) - int(time.time()) + 5
        return max(wait, 10)
    retry_after = response.headers.get("Retry-After")
    if retry_after and str(retry_after).isdigit():
        return int(retry_after)
    return min(2 ** attempt, default)


def github_graphql(
    token: str,
    query: str,
    variables: Dict[str, Any],
    max_retries: int = 12,
) -> Dict[str, Any]:
    """POST a GraphQL query, retrying on GitHub's body-level RATE_LIMITED error.

    Network errors, HTTP 429/5xx, and header-based rate limits are handled by
    the shared `request_with_retry` policy. GraphQL has its own rate-limit
    signal on top of that -- a 200 OK response whose `errors` array carries
    `type: RATE_LIMITED` -- which is invisible to a plain HTTP-status-based
    retry policy, so it still needs its own loop here.
    """
    session = requests.Session()
    session.headers.update(github_headers(token))

    for attempt in range(1, max_retries + 1):
        response = request_with_retry(
            session,
            "POST",
            GITHUB_GRAPHQL_URL,
            json={"query": query, "variables": variables},
        )
        if response is None:
            raise RuntimeError("GitHub GraphQL failed: no response after retries")

        payload = response.json()
        if "errors" in payload:
            errors = payload["errors"]
            if any(err.get("type") == "RATE_LIMITED" for err in errors):
                wait = _wait_for_rate_limit(response, attempt)
                logger.warning(
                    "GitHub GraphQL rate limit. Waiting %ss (attempt %d/%d).",
                    wait, attempt, max_retries,
                )
                time.sleep(wait)
                continue
            raise RuntimeError(f"GraphQL errors: {json.dumps(errors, indent=2)}")
        return payload["data"]

    raise RuntimeError(f"GitHub GraphQL failed after {max_retries} retries (rate limited).")


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def fetch_merged_prs(
    token: str,
    repo: str,
    sleep_seconds: float,
    checkpoint_dir: Optional[Path] = None,
    page_size: int = 50,
) -> List[Dict[str, Any]]:
    if "/" not in repo:
        raise ValueError("Repo must be in owner/name format, e.g. owner/repo")

    owner, name = repo.split("/", 1)
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", repo)
    jsonl_path = state_path = None
    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = checkpoint_dir / f"{slug}_prs.jsonl"
        state_path = checkpoint_dir / f"{slug}_fetch_state.json"

    cursor: Optional[str] = None
    page = 0
    pr_count = 0
    all_prs_mem: List[Dict[str, Any]] = []

    if state_path and state_path.exists() and jsonl_path and jsonl_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("repo") == repo:
            cursor = state.get("cursor")
            page = int(state.get("page") or 0)
            pr_count = int(state.get("count") or 0)
            logger.info(
                "Resuming fetch for %s from page %d (%d PRs on disk)...",
                repo, page, pr_count,
            )

    logger.info("Fetching merged PRs for %s...", repo)
    while True:
        page += 1
        variables = {"owner": owner, "name": name, "cursor": cursor, "pageSize": page_size}
        data = github_graphql(token, GRAPHQL_QUERY, variables)
        pr_connection = data["repository"]["pullRequests"]

        nodes = pr_connection["nodes"] or []
        if jsonl_path is not None:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                for node in nodes:
                    f.write(json.dumps(node, ensure_ascii=False) + "\n")
        else:
            all_prs_mem.extend(nodes)

        pr_count += len(nodes)
        logger.info("  page %d -> %d PRs fetched so far", page, pr_count)

        page_info = pr_connection["pageInfo"]
        if not page_info["hasNextPage"]:
            logger.info("No more pages. Total fetched: %d PRs.", pr_count)
            if jsonl_path is not None:
                all_prs = _load_jsonl(jsonl_path)
                jsonl_path.unlink(missing_ok=True)
                if state_path:
                    state_path.unlink(missing_ok=True)
                return all_prs
            return all_prs_mem

        cursor = page_info["endCursor"]
        if state_path is not None:
            state_path.write_text(
                json.dumps(
                    {"repo": repo, "cursor": cursor, "page": page, "count": pr_count},
                    indent=2,
                ),
                encoding="utf-8",
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


# ─────────────────────────────────────────────────────────────────────────────
# GitLab fetch (merged MRs → GitHub-shaped PR dicts for shared pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def gitlab_rest_get(
    token: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 8,
) -> Any:
    session = requests.Session()
    session.headers.update(gitlab_headers(token))
    url = f"{GITLAB_REST_URL}{path}"
    response = request_with_retry(session, "GET", url, params=params, max_retries=max_retries)
    if response is None:
        raise RuntimeError(f"GitLab REST request failed (not found, no access, or exhausted retries): {url}")
    if response.status_code == 204 or not response.text:
        return None
    return response.json()


def gitlab_rest_paginated(
    token: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    session = requests.Session()
    session.headers.update(gitlab_headers(token))
    url = f"{GITLAB_REST_URL}{path}"
    return gitlab_platform_paginate(session, url, params=params)


def list_gitlab_group_projects(
    token: str,
    group: str,
    include_archived: bool,
) -> List[str]:
    encoded_group = urllib.parse.quote(group, safe="")
    logger.info("Listing GitLab projects for group %s...", group)
    raw = gitlab_rest_paginated(
        token,
        f"/groups/{encoded_group}/projects",
        {
            "include_subgroups": "true",
            "archived": "true" if include_archived else "false",
            "order_by": "last_activity_at",
        },
    )
    projects = [
        p["path_with_namespace"]
        for p in raw
        if p.get("path_with_namespace")
    ]
    logger.info("  %s: %d projects", group, len(projects))
    return projects


def _gitlab_actor(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not user:
        return {}
    login = user.get("username") or ""
    is_bot = bool(user.get("bot")) or login.endswith("[bot]") or "bot" in login.lower()
    return {"login": login, "__typename": "Bot" if is_bot else "User"}


def normalize_gitlab_mr(
    mr: Dict[str, Any],
    changes_payload: Optional[Dict[str, Any]],
    notes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    change_list = (changes_payload or {}).get("changes") or []
    paths = [
        ch.get("new_path") or ch.get("old_path")
        for ch in change_list
        if ch.get("new_path") or ch.get("old_path")
    ]

    issue_comment_nodes: List[Dict[str, Any]] = []
    review_nodes: List[Dict[str, Any]] = []
    thread_nodes: List[Dict[str, Any]] = []

    for note in notes:
        if note.get("system"):
            continue
        body = note.get("body") or ""
        if not body.strip():
            continue
        author = _gitlab_actor(note.get("author"))
        note_type = note.get("type") or ""
        if note_type == "DiffNote":
            path = (note.get("position") or {}).get("new_path") or ""
            thread_nodes.append(
                {
                    "isResolved": False,
                    "comments": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "bodyText": body,
                                "author": author,
                                "path": path,
                            }
                        ],
                    },
                }
            )
            review_state = (
                "CHANGES_REQUESTED"
                if "requested changes" in body.lower()
                else "COMMENTED"
            )
            review_nodes.append(
                {"state": review_state, "bodyText": body, "author": author}
            )
        else:
            issue_comment_nodes.append({"bodyText": body, "author": author})

    description = mr.get("description") or ""
    author = _gitlab_actor(mr.get("author"))

    return {
        "number": mr.get("iid"),
        "title": mr.get("title") or "",
        "bodyText": description,
        "url": mr.get("web_url"),
        "mergedAt": mr.get("merged_at"),
        "changedFiles": len(paths) or int(mr.get("changes_count") or 0),
        "additions": 0,
        "deletions": 0,
        "author": author,
        "labels": {"nodes": [{"name": x} for x in (mr.get("labels") or [])]},
        "commits": {"totalCount": 1},
        "files": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"path": p} for p in paths],
        },
        "closingIssuesReferences": {"nodes": []},
        "comments": {
            "pageInfo": {"hasNextPage": False},
            "nodes": issue_comment_nodes,
        },
        "reviews": {
            "pageInfo": {"hasNextPage": False},
            "nodes": review_nodes,
        },
        "reviewThreads": {
            "pageInfo": {"hasNextPage": False},
            "nodes": thread_nodes,
        },
    }


def fetch_merged_gitlab_mrs(
    token: str,
    project: str,
    sleep_seconds: float,
    checkpoint_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    encoded = urllib.parse.quote(project, safe="")
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"gitlab_{project}")
    jsonl_path = state_path = None
    done_iids: set = set()

    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = checkpoint_dir / f"{slug}_prs.jsonl"
        state_path = checkpoint_dir / f"{slug}_fetch_state.json"
        if jsonl_path.exists():
            for row in _load_jsonl(jsonl_path):
                iid = row.get("number")
                if iid is not None:
                    done_iids.add(int(iid))
            if done_iids:
                logger.info(
                    "Resuming GitLab fetch for %s (%d MRs on disk)...",
                    project, len(done_iids),
                )

    logger.info("Fetching merged MRs for GitLab %s...", project)
    mr_list = gitlab_rest_paginated(
        token,
        f"/projects/{encoded}/merge_requests",
        {"state": "merged", "order_by": "updated_at", "sort": "desc"},
    )

    fetched = 0
    for mr in mr_list:
        iid = mr.get("iid")
        if iid is None:
            continue
        if int(iid) in done_iids:
            continue

        changes = gitlab_rest_get(token, f"/projects/{encoded}/merge_requests/{iid}/changes")
        notes = gitlab_rest_paginated(
            token,
            f"/projects/{encoded}/merge_requests/{iid}/notes",
        )
        normalized = normalize_gitlab_mr(mr, changes, notes)

        if jsonl_path is not None:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(normalized, ensure_ascii=False) + "\n")
        done_iids.add(int(iid))
        fetched += 1
        if fetched % 25 == 0 or fetched == 1:
            logger.info("  enriched %d new MRs (%d total on disk)", fetched, len(done_iids))

        if state_path is not None:
            state_path.write_text(
                json.dumps(
                    {"project": project, "count": len(done_iids), "last_iid": iid},
                    indent=2,
                ),
                encoding="utf-8",
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    logger.info("Total merged MRs fetched for %s: %d", project, len(done_iids))
    if jsonl_path is not None and jsonl_path.exists():
        all_prs = _load_jsonl(jsonl_path)
        jsonl_path.unlink(missing_ok=True)
        if state_path:
            state_path.unlink(missing_ok=True)
        return all_prs
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Bitbucket (REST 2.0)
# ─────────────────────────────────────────────────────────────────────────────

def bitbucket_rest_get(
    token: str,
    url: str,
    username: str = "",
    max_retries: int = 8,
) -> Any:
    """GET an absolute Bitbucket URL, with the same retry discipline as GitLab."""
    session = requests.Session()
    session.headers.update(bitbucket_headers(token, username))
    response = request_with_retry(session, "GET", url, max_retries=max_retries)
    if response is None:
        raise RuntimeError(f"Bitbucket REST request failed (not found, no access, or exhausted retries): {url}")
    if response.status_code == 204 or not response.text:
        return None
    return response.json()


def bitbucket_rest_paginated(token: str, url: str, username: str = "") -> List[Any]:
    """Follow Bitbucket's `next` cursor, collecting every `values` item."""
    session = requests.Session()
    session.headers.update(bitbucket_headers(token, username))
    return bitbucket_platform_paginate(session, url)


def _bitbucket_actor(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not user:
        return {}
    login = user.get("nickname") or user.get("display_name") or ""
    is_bot = user.get("type") == "app" or "bot" in login.lower()
    return {"login": login, "__typename": "Bot" if is_bot else "User"}


def normalize_bitbucket_pr(
    pr: Dict[str, Any],
    diffstat: List[Dict[str, Any]],
    comments: List[Dict[str, Any]],
    participants: List[Dict[str, Any]],
) -> Dict[str, Any]:
    paths: List[str] = []
    additions = 0
    deletions = 0
    for entry in diffstat:
        new = (entry.get("new") or {}).get("path")
        old = (entry.get("old") or {}).get("path")
        p = new or old
        if p:
            paths.append(p)
        additions += int(entry.get("lines_added") or 0)
        deletions += int(entry.get("lines_removed") or 0)

    issue_comment_nodes: List[Dict[str, Any]] = []
    review_nodes: List[Dict[str, Any]] = []
    thread_nodes: List[Dict[str, Any]] = []
    for c in comments:
        if c.get("deleted"):
            continue
        body = ((c.get("content") or {}).get("raw") or "").strip()
        if not body:
            continue
        author = _bitbucket_actor(c.get("user"))
        inline = c.get("inline")
        if inline:
            path = inline.get("path") or ""
            thread_nodes.append({
                "isResolved": bool(c.get("resolved")),
                "comments": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"bodyText": body, "author": author, "path": path}],
                },
            })
            review_nodes.append({"state": "COMMENTED", "bodyText": body, "author": author})
        else:
            issue_comment_nodes.append({"bodyText": body, "author": author})

    # Bitbucket "approved" participants -> approving reviews.
    for part in participants:
        if part.get("approved"):
            review_nodes.append({
                "state": "APPROVED",
                "bodyText": "",
                "author": _bitbucket_actor(part.get("user")),
            })

    return {
        "number": pr.get("id"),
        "title": pr.get("title") or "",
        "bodyText": (pr.get("description") or "") if isinstance(pr.get("description"), str)
                    else ((pr.get("rendered") or {}).get("description", {}) or {}).get("raw", ""),
        "url": ((pr.get("links") or {}).get("html") or {}).get("href"),
        "mergedAt": pr.get("updated_on"),
        "changedFiles": len(paths),
        "additions": additions,
        "deletions": deletions,
        "author": _bitbucket_actor(pr.get("author")),
        "labels": {"nodes": []},
        "commits": {"totalCount": int(pr.get("comment_count", 0) >= 0)},
        "files": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"path": p} for p in paths],
        },
        "closingIssuesReferences": {"nodes": []},
        "comments": {
            "pageInfo": {"hasNextPage": False},
            "nodes": issue_comment_nodes,
        },
        "reviews": {
            "pageInfo": {"hasNextPage": False},
            "nodes": review_nodes,
        },
        "reviewThreads": {
            "pageInfo": {"hasNextPage": False},
            "nodes": thread_nodes,
        },
    }


def fetch_merged_bitbucket_prs(
    token: str,
    repo: str,
    sleep_seconds: float,
    username: str = "",
    checkpoint_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    workspace, name = repo.split("/", 1)
    base = f"{BITBUCKET_REST_URL}/repositories/{workspace}/{name}"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"bitbucket_{repo}")
    jsonl_path = state_path = None
    done_ids: set = set()

    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = checkpoint_dir / f"{slug}_prs.jsonl"
        state_path = checkpoint_dir / f"{slug}_fetch_state.json"
        if jsonl_path.exists():
            for row in _load_jsonl(jsonl_path):
                pid = row.get("number")
                if pid is not None:
                    done_ids.add(int(pid))
            if done_ids:
                logger.info("Resuming Bitbucket fetch for %s (%d PRs on disk)...", repo, len(done_ids))

    logger.info("Fetching merged PRs for Bitbucket %s...", repo)
    pr_list = bitbucket_rest_paginated(token, f"{base}/pullrequests?state=MERGED&pagelen=50", username)

    fetched = 0
    for pr in pr_list:
        pid = pr.get("id")
        if pid is None or int(pid) in done_ids:
            continue
        # description isn't in the list payload; fetch the PR detail
        detail = bitbucket_rest_get(token, f"{base}/pullrequests/{pid}", username) or pr
        diffstat = bitbucket_rest_paginated(token, f"{base}/pullrequests/{pid}/diffstat?pagelen=100", username)
        comments = bitbucket_rest_paginated(token, f"{base}/pullrequests/{pid}/comments?pagelen=100", username)
        participants = detail.get("participants") or []
        normalized = normalize_bitbucket_pr(detail, diffstat, comments, participants)

        if jsonl_path is not None:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(normalized, ensure_ascii=False) + "\n")
        done_ids.add(int(pid))
        fetched += 1
        if fetched % 25 == 0 or fetched == 1:
            logger.info("  enriched %d new PRs (%d total on disk)", fetched, len(done_ids))
        if state_path is not None:
            state_path.write_text(
                json.dumps({"repo": repo, "count": len(done_ids), "last_id": pid}, indent=2),
                encoding="utf-8",
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    logger.info("Total merged PRs fetched for %s: %d", repo, len(done_ids))
    if jsonl_path is not None and jsonl_path.exists():
        all_prs = _load_jsonl(jsonl_path)
        jsonl_path.unlink(missing_ok=True)
        if state_path:
            state_path.unlink(missing_ok=True)
        return all_prs
    return []


def resolve_gitlab_targets(
    token: str,
    group_args: Optional[List[str]],
    project_args: Optional[List[str]],
    include_archived: bool,
) -> List[str]:
    projects: List[str] = []
    seen = set()

    def add(path: str) -> None:
        key = path.lower()
        if key not in seen:
            seen.add(key)
            projects.append(path)

    for group in _split_csv_args(group_args):
        for path in list_gitlab_group_projects(token, group, include_archived):
            add(path)

    for project in _split_csv_args(project_args):
        add(project)

    return projects


# ─────────────────────────────────────────────────────────────────────────────
# Target resolution (single repo / multiple / org / user)
# ─────────────────────────────────────────────────────────────────────────────

def github_rest_get(
    token: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    max_retries: int = 5,
) -> Any:
    session = requests.Session()
    session.headers.update(github_headers(token))
    url = f"{GITHUB_REST_URL}{path}"
    response = request_with_retry(session, "GET", url, params=params, max_retries=max_retries)
    if response is None:
        raise RuntimeError(f"GitHub REST request failed (not found, no access, or exhausted retries): {url}")
    return response.json()


def github_rest_paginated(token: str, path: str, params: Dict[str, Any]) -> List[Any]:
    session = requests.Session()
    session.headers.update(github_headers(token))
    url = f"{GITHUB_REST_URL}{path}"
    return github_platform_paginate(session, url, params=params)


def get_owner_type(token: str, owner: str) -> str:
    """Return 'Organization' or 'User' for an owner login."""
    data = github_rest_get(token, f"/users/{owner}")
    return data.get("type", "User")


def list_repos_for_owner(
    token: str,
    owner: str,
    owner_type: Optional[str],
    include_archived: bool,
    include_forks: bool,
) -> List[str]:
    if owner_type is None:
        owner_type = get_owner_type(token, owner)

    if owner_type == "Organization":
        path = f"/orgs/{owner}/repos"
        params: Dict[str, Any] = {"type": "all", "sort": "updated"}
    else:
        path = f"/users/{owner}/repos"
        params = {"type": "owner", "sort": "updated"}

    logger.info("Listing repos for %s (%s)...", owner, owner_type)
    raw = github_rest_paginated(token, path, params)

    repos: List[str] = []
    skipped_archived = skipped_forks = 0
    for r in raw:
        if r.get("archived") and not include_archived:
            skipped_archived += 1
            continue
        if r.get("fork") and not include_forks:
            skipped_forks += 1
            continue
        full = r.get("full_name")
        if full:
            repos.append(full)

    logger.info(
        "  %s: %d repos (skipped %d archived, %d forks)",
        owner, len(repos), skipped_archived, skipped_forks,
    )
    return repos


def _split_csv_args(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for item in values or []:
        out.extend(piece.strip() for piece in item.split(",") if piece.strip())
    return out


def resolve_targets(
    token: str,
    repo_args: Optional[List[str]],
    org_args: Optional[List[str]],
    user_args: Optional[List[str]],
    include_archived: bool,
    include_forks: bool,
) -> List[str]:
    """Turn --repo/--org/--user into a de-duplicated list of owner/name repos."""
    repos: List[str] = []
    seen = set()

    def add(full_name: str) -> None:
        key = full_name.lower()
        if key not in seen:
            seen.add(key)
            repos.append(full_name)

    for entry in _split_csv_args(repo_args):
        if "/" in entry:
            add(entry)
        else:
            # Bare owner -> expand to all its repos (org or user, auto-detected).
            for full in list_repos_for_owner(token, entry, None, include_archived, include_forks):
                add(full)

    for org in _split_csv_args(org_args):
        for full in list_repos_for_owner(token, org, "Organization", include_archived, include_forks):
            add(full)

    for user in _split_csv_args(user_args):
        for full in list_repos_for_owner(token, user, "User", include_archived, include_forks):
            add(full)

    return repos


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic signal extraction + rules
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(text: Optional[str]) -> str:
    return " ".join((text or "").strip().lower().split())


def is_bot_actor(actor: Optional[Dict[str, Any]]) -> bool:
    if not actor:
        return False
    login = clean_text(actor.get("login"))
    typename = actor.get("__typename") or actor.get("type")
    if typename == "Bot":
        return True
    if login.endswith("[bot]"):
        return True
    if login in BOT_LOGINS:
        return True
    if "bot" in login:
        return True
    return False


def body_linked_issue_refs(body: str) -> List[str]:
    refs = []
    for match in LINKED_ISSUE_REGEX.finditer(body or ""):
        refs.append(match.group(3))
    return sorted(set(refs))


def touches_tests(paths: List[str]) -> bool:
    lower_paths = [p.lower() for p in paths]
    return any(any(hint in path for hint in TEST_PATH_HINTS) for path in lower_paths)


def mostly_generated_or_lockfiles(paths: List[str]) -> bool:
    if not paths:
        return False
    lower_paths = [p.lower() for p in paths]
    generated_count = sum(
        1 for path in lower_paths if any(hint in path for hint in GENERATED_PATH_HINTS)
    )
    return generated_count / len(paths) >= 0.60


def has_feature_or_bug_label(labels: List[str]) -> bool:
    normalized = {clean_text(label) for label in labels}
    useful = {
        "feature",
        "enhancement",
        "bug",
        "bugfix",
        "fix",
        "backend",
        "frontend",
        "api",
        "tests",
        "refactor",
    }
    return bool(normalized & useful)


def is_trivial_comment(text: str) -> bool:
    t = clean_text(text)
    if not t:
        return True
    if t in TRIVIAL_COMMENT_EXACT:
        return True
    if len(t) <= 25:
        for phrase in TRIVIAL_COMMENT_EXACT:
            if t.startswith(phrase):
                return True
    trivial_phrases = (
        "lgtm",
        "looks good",
        "approved",
        "ship it",
        "thanks",
        "done",
        "fixed",
        "please rebase",
        "rebase please",
    )
    if len(t) < 60 and any(phrase in t for phrase in trivial_phrases):
        return True
    return False


def is_potentially_substantive_comment(text: str) -> bool:
    t = clean_text(text)
    if is_trivial_comment(t):
        return False
    if len(t) >= 120:
        return True
    return any(term in t for term in SUBSTANTIVE_TERMS)


def flatten_discussion(pr: Dict[str, Any]) -> List[Dict[str, Any]]:
    comments = []
    for c in pr.get("comments", {}).get("nodes", []) or []:
        comments.append(
            {"kind": "issue_comment", "body": c.get("bodyText") or "", "author": c.get("author")}
        )
    for review in pr.get("reviews", {}).get("nodes", []) or []:
        review_body = review.get("bodyText") or ""
        if review_body.strip():
            comments.append(
                {"kind": "review_body", "body": review_body, "author": review.get("author")}
            )
        for c in review.get("comments", {}).get("nodes", []) or []:
            comments.append(
                {
                    "kind": "review_comment",
                    "body": c.get("bodyText") or "",
                    "author": c.get("author"),
                    "path": c.get("path"),
                }
            )
    for thread in pr.get("reviewThreads", {}).get("nodes", []) or []:
        for c in thread.get("comments", {}).get("nodes", []) or []:
            comments.append(
                {
                    "kind": "review_thread_comment",
                    "body": c.get("bodyText") or "",
                    "author": c.get("author"),
                    "path": c.get("path"),
                    "thread_resolved": thread.get("isResolved"),
                }
            )
    return comments


def compute_size_axis(changed_files: int) -> str:
    if changed_files <= 2:
        return "tiny"
    if changed_files <= 10:
        return "small_mid"
    return "large"


def compute_richness_axis(
    linked_issue_count: int,
    non_bot_comments: List[Dict[str, Any]],
    non_bot_reviewers_count: int,
    has_changes_requested: bool,
) -> Tuple[str, int]:
    substantive_comments = [
        c for c in non_bot_comments if is_potentially_substantive_comment(c.get("body", ""))
    ]
    substantive_count = len(substantive_comments)
    has_linked_issue = linked_issue_count > 0

    if has_linked_issue and (
        substantive_count >= 1
        or has_changes_requested
        or (non_bot_reviewers_count >= 2 and len(non_bot_comments) >= 2)
    ):
        return "substantive", substantive_count
    if has_linked_issue or len(non_bot_comments) > 0:
        return "light", substantive_count
    return "none", substantive_count


def extract_signals(pr: Dict[str, Any]) -> Dict[str, Any]:
    author = pr.get("author")
    author_is_bot = is_bot_actor(author)

    labels = [x["name"] for x in pr.get("labels", {}).get("nodes", []) or []]
    changed_files = int(pr.get("changedFiles") or 0)
    additions = int(pr.get("additions") or 0)
    deletions = int(pr.get("deletions") or 0)

    file_nodes = pr.get("files", {}).get("nodes", []) or []
    paths = [x["path"] for x in file_nodes if x.get("path")]
    file_list_complete = not pr.get("files", {}).get("pageInfo", {}).get("hasNextPage", False)

    closing_issues = pr.get("closingIssuesReferences", {}).get("nodes", []) or []
    closing_issue_refs = [f"#{x['number']}" for x in closing_issues if x.get("number")]
    body_issue_refs = body_linked_issue_refs(pr.get("bodyText") or "")
    linked_issue_refs = sorted(set(closing_issue_refs + body_issue_refs))
    linked_issue_count = len(linked_issue_refs)

    all_comments = flatten_discussion(pr)
    non_bot_comments = [c for c in all_comments if not is_bot_actor(c.get("author"))]

    review_nodes = pr.get("reviews", {}).get("nodes", []) or []
    non_bot_reviewers = {
        r.get("author", {}).get("login")
        for r in review_nodes
        if r.get("author") and not is_bot_actor(r.get("author"))
    }
    non_bot_reviewers.discard(None)

    has_changes_requested = any(
        r.get("state") == "CHANGES_REQUESTED" and not is_bot_actor(r.get("author"))
        for r in review_nodes
    )

    size_axis = compute_size_axis(changed_files)
    richness_axis, substantive_comment_count = compute_richness_axis(
        linked_issue_count=linked_issue_count,
        non_bot_comments=non_bot_comments,
        non_bot_reviewers_count=len(non_bot_reviewers),
        has_changes_requested=has_changes_requested,
    )

    discussion_incomplete = (
        pr.get("comments", {}).get("pageInfo", {}).get("hasNextPage", False)
        or pr.get("reviews", {}).get("pageInfo", {}).get("hasNextPage", False)
        or pr.get("reviewThreads", {}).get("pageInfo", {}).get("hasNextPage", False)
    )

    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "title": pr.get("title") or "",
        "author": author.get("login") if author else "",
        "author_is_bot": author_is_bot,
        "merged_at": pr.get("mergedAt"),
        "changed_files": changed_files,
        "additions": additions,
        "deletions": deletions,
        "commits": pr.get("commits", {}).get("totalCount", 0),
        "labels": labels,
        "paths": paths,
        "file_list_complete": file_list_complete,
        "touches_tests": touches_tests(paths),
        "generated_or_lockfile_heavy": mostly_generated_or_lockfiles(paths),
        "feature_or_bug_label": has_feature_or_bug_label(labels),
        "linked_issue_count": linked_issue_count,
        "linked_issue_refs": linked_issue_refs,
        "non_bot_comments": non_bot_comments,
        "non_bot_comment_count": len(non_bot_comments),
        "substantive_comment_count": substantive_comment_count,
        "non_bot_reviewers_count": len(non_bot_reviewers),
        "has_changes_requested": has_changes_requested,
        "discussion_incomplete": discussion_incomplete,
        "size_axis": size_axis,
        "richness_axis": richness_axis,
    }


def rules_classify(sig: Dict[str, Any]) -> Tuple[str, str]:
    size_axis = sig["size_axis"]
    richness_axis = sig["richness_axis"]
    has_tests = sig["touches_tests"]
    generated_or_lockfile_heavy = sig["generated_or_lockfile_heavy"]
    feature_or_bug_label = sig["feature_or_bug_label"]
    linked_issue_count = sig["linked_issue_count"]

    if sig["author_is_bot"]:
        return "automated", "Bot-authored PR."
    if richness_axis == "substantive" and linked_issue_count > 0 and not generated_or_lockfile_heavy:
        return "rich_task", "Linked issue plus substantive non-bot review discussion."
    if (
        size_axis == "tiny"
        and richness_axis == "none"
        and not has_tests
        and not generated_or_lockfile_heavy
    ):
        return "simple_fix", "Tiny human PR with no meaningful non-bot discussion."
    if generated_or_lockfile_heavy and richness_axis != "substantive":
        return "other", "Mechanical/generated or lockfile-heavy PR."
    if (
        size_axis == "small_mid" or has_tests or feature_or_bug_label
    ) and richness_axis in {"none", "light"}:
        return "standard_feature_work", "Normal human implementation PR without rich review signals."
    return "other", "Does not cleanly fit simple, standard, or rich definitions."


# ─────────────────────────────────────────────────────────────────────────────
# LLM pass
# ─────────────────────────────────────────────────────────────────────────────

def comment_samples(sig: Dict[str, Any], max_samples: int = 8, max_len: int = 280) -> List[str]:
    samples: List[str] = []
    for c in sig["non_bot_comments"]:
        body = (c.get("body") or "").strip()
        if not body or is_trivial_comment(body):
            continue
        snippet = " ".join(body.split())[:max_len]
        samples.append(f"[{c.get('kind')}] {snippet}")
        if len(samples) >= max_samples:
            break
    return samples


def build_llm_input(sig: Dict[str, Any], body_text: str) -> Dict[str, Any]:
    return {
        "number": sig["number"],
        "title": sig["title"],
        "body_excerpt": " ".join((body_text or "").split())[:800],
        "changed_files": sig["changed_files"],
        "additions": sig["additions"],
        "deletions": sig["deletions"],
        "file_paths_sample": sig["paths"][:30],
        "touches_tests": sig["touches_tests"],
        "generated_or_lockfile_heavy": sig["generated_or_lockfile_heavy"],
        "labels": sig["labels"],
        "linked_issue_count": sig["linked_issue_count"],
        "non_bot_comment_count": sig["non_bot_comment_count"],
        "substantive_comment_count": sig["substantive_comment_count"],
        "non_bot_reviewers_count": sig["non_bot_reviewers_count"],
        "has_changes_requested": sig["has_changes_requested"],
        "discussion_samples": comment_samples(sig),
    }


def _llm_preflight(client: Any, model: str) -> None:
    """One test call before classifying thousands of PRs.

    A configuration error (bad key, unknown model, wrong Azure deployment or
    api-version) aborts the phase immediately with a clear message, instead of
    surfacing as a per-PR failure repeated across the whole repo. Transient
    errors (rate limit, timeout, 5xx) are not fatal here -- the per-PR retry
    loop handles those -- so we only abort on 4xx config-class statuses.
    """
    try:
        client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        logger.info("LLM preflight OK (model=%s).", model)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        if status in (400, 401, 403, 404):
            raise SystemExit(
                f"LLM preflight failed [{status} {type(exc).__name__}]: {exc}\n"
                "The LLM is misconfigured. Check the model/deployment name and "
                "API key; for Azure, verify AZURE_OPENAI_ENDPOINT, "
                "AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT and "
                "OPENAI_API_VERSION. Aborting before classifying PRs so you don't "
                "get thousands of failed calls and an all-'error' report."
            )
        logger.warning(
            "LLM preflight hit a non-fatal error (%s: %s); proceeding, per-PR "
            "retries will handle transient issues.", type(exc).__name__, exc,
        )


def _parse_classification_content(content: Optional[str]) -> Dict[str, Any]:
    """Turn one LLM response's raw JSON content into the category/confidence/
    reason shape used downstream. Shared by the batch and sync paths -- both
    of llm.batch's run_batch_or_sync branches hand back a raw content string,
    so this is the one place that interprets it."""
    try:
        parsed = json.loads(content or "{}")
    except json.JSONDecodeError:
        return {"llm_category": "error", "llm_confidence": "low", "llm_reason": "invalid JSON from LLM"}
    category = parsed.get("category")
    if category not in CATEGORIES:
        category = "other"
    return {
        "llm_category": category,
        "llm_confidence": parsed.get("confidence", "low"),
        "llm_reason": (parsed.get("reason") or "")[:300],
    }


def _sync_classify_item(
    client: Any, model: str, item: BatchItem, max_retries: int = 3
) -> BatchItemResult:
    """The sync-fallback path run_batch_or_sync uses below its batch
    threshold -- one live chat.completions.create call per PR, same retry
    behaviour the old per-PR loop had."""
    last_err: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=item.temperature,
                response_format=item.response_format,
                messages=item.messages,
            )
            content = resp.choices[0].message.content or "{}"
            return BatchItemResult(item.custom_id, True, content, None, item.metadata)
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            logger.warning(
                "PR #%s: LLM call failed (attempt %d/%d): %s",
                item.metadata.get("number"), attempt, max_retries, exc,
            )
            time.sleep(1.5 * attempt)
    logger.error("PR #%s: LLM classification failed permanently: %s",
                 item.metadata.get("number"), last_err)
    return BatchItemResult(item.custom_id, False, None, last_err, item.metadata)


def classify_with_llm(
    client: Any,
    model: str,
    llm_input: Dict[str, Any],
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Single-PR classification, kept for direct/interactive use. process_prs
    below no longer calls this in a loop -- it goes through
    llm.batch.run_batch_or_sync so large PR counts use the Batch API instead
    of one live request per PR."""
    item = BatchItem(
        custom_id="single",
        messages=[
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(llm_input, ensure_ascii=False)},
        ],
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        metadata={"number": llm_input.get("number")},
    )
    result = _sync_classify_item(client, model, item, max_retries=max_retries)
    if result.ok:
        return _parse_classification_content(result.content)
    return {
        "llm_category": "error",
        "llm_confidence": "low",
        "llm_reason": f"LLM call failed: {result.error}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def process_prs(
    prs: List[Dict[str, Any]],
    model: str,
    max_workers: int,
    repo: str,
    client: Any = None,
    batch_work_dir: Optional[Path] = None,
    llm_mode: str = "auto",
    llm_batch_threshold: int = DEFAULT_BATCH_THRESHOLD,
) -> List[Dict[str, Any]]:
    if client is None:
        client = safe_openai()

    logger.info("Extracting deterministic signals for %d PRs...", len(prs))
    signals = [extract_signals(pr) for pr in prs]
    body_texts = [pr.get("bodyText") or "" for pr in prs]
    rules = [rules_classify(sig) for sig in signals]

    bot_count = sum(1 for s in signals if s["author_is_bot"])
    logger.info("Signals ready. %d bot-authored PRs will skip the LLM.", bot_count)

    # Fail fast on a misconfigured LLM. Without this, a wrong key/model/Azure
    # deployment makes EVERY non-bot PR fail its 3 retries and get logged as an
    # error -- hours of noise on a large repo, ending in an all-"error" CSV that
    # looks like success. One test call turns that into a clear abort in seconds.
    if bot_count < len(prs):
        _llm_preflight(client, model)

    results: Dict[int, Dict[str, Any]] = {}
    items: List[BatchItem] = []
    for idx, sig in enumerate(signals):
        if sig["author_is_bot"]:
            results[idx] = {
                "llm_category": "automated",
                "llm_confidence": "high",
                "llm_reason": "Bot-authored PR (no LLM call).",
            }
            continue
        llm_input = build_llm_input(sig, body_texts[idx])
        items.append(
            BatchItem(
                custom_id=str(idx),
                messages=[
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(llm_input, ensure_ascii=False)},
                ],
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                metadata={"idx": idx, "number": sig["number"]},
            )
        )

    if items:
        logger.info(
            "Classifying %d PRs with model=%s (llm_mode=%s; batches once count >= %d, "
            "otherwise %d sync workers)...",
            len(items), model, llm_mode, llm_batch_threshold, max_workers,
        )
        work_dir = batch_work_dir or (Path("outputs") / "batch_state" / safe_repo_slug(repo))
        batch_results = run_batch_or_sync(
            client,
            items,
            work_dir,
            tag=safe_repo_slug(repo),
            sync_fn=lambda item: _sync_classify_item(client, model, item),
            mode=llm_mode,
            threshold=llm_batch_threshold,
            max_workers=max_workers,
        )

        completed = 0
        for r in batch_results:
            idx = r.metadata["idx"]
            if r.ok:
                results[idx] = _parse_classification_content(r.content)
            else:
                results[idx] = {
                    "llm_category": "error",
                    "llm_confidence": "low",
                    "llm_reason": f"LLM call failed: {r.error}",
                }
            completed += 1
            sig = signals[idx]
            logger.debug(
                "PR #%s | rules=%s | llm=%s | conf=%s | %s",
                sig["number"], rules[idx][0], results[idx]["llm_category"],
                results[idx].get("llm_confidence"), sig["title"][:80],
            )
            if completed % 25 == 0 or completed == len(items):
                logger.info("  classified %d/%d", completed, len(items))

    rows: List[Dict[str, Any]] = []
    for idx, sig in enumerate(signals):
        rules_category, rules_reason = rules[idx]
        llm_result = results.get(idx, {})
        llm_category = llm_result.get("llm_category", "error")
        agree = rules_category == llm_category
        if not agree and llm_category not in ("error",):
            logger.debug("DISAGREEMENT PR #%s: rules=%s llm=%s",
                         sig["number"], rules_category, llm_category)
        rows.append(
            {
                "repository": repo,
                "number": sig["number"],
                "url": sig["url"],
                "title": sig["title"],
                "author": sig["author"],
                "author_is_bot": sig["author_is_bot"],
                "merged_at": sig["merged_at"],
                "changed_files": sig["changed_files"],
                "additions": sig["additions"],
                "deletions": sig["deletions"],
                "commits": sig["commits"],
                "rules_category": rules_category,
                "rules_reason": rules_reason,
                "llm_category": llm_category,
                "llm_confidence": llm_result.get("llm_confidence", ""),
                "llm_reason": llm_result.get("llm_reason", ""),
                "agree": agree,
                "touches_tests": sig["touches_tests"],
                "generated_or_lockfile_heavy": sig["generated_or_lockfile_heavy"],
                "linked_issue_count": sig["linked_issue_count"],
                "linked_issue_refs": ",".join(sig["linked_issue_refs"]),
                "non_bot_comment_count": sig["non_bot_comment_count"],
                "substantive_comment_count": sig["substantive_comment_count"],
                "non_bot_reviewers_count": sig["non_bot_reviewers_count"],
                "has_changes_requested": sig["has_changes_requested"],
                "size_axis": sig["size_axis"],
                "richness_axis": sig["richness_axis"],
                "labels": ",".join(sig["labels"]),
                "file_paths_sample": ",".join(sig["paths"][:30]),
                "file_list_complete": sig["file_list_complete"],
                "discussion_incomplete": sig["discussion_incomplete"],
            }
        )
    return rows


def build_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)

    def pct(n: int, d: int) -> float:
        return round((n / d) * 100, 2) if d else 0.0

    rules_counts = Counter(r["rules_category"] for r in rows)
    llm_counts = Counter(r["llm_category"] for r in rows)
    comparable = [r for r in rows if r["llm_category"] != "error"]
    agree_count = sum(1 for r in comparable if r["agree"])
    disagreements = Counter(
        f"{r['rules_category']} -> {r['llm_category']}"
        for r in comparable
        if not r["agree"]
    )

    return {
        "total_merged_prs_analyzed": total,
        "rules": {
            "counts": dict(rules_counts),
            "percentages": {k: pct(v, total) for k, v in rules_counts.items()},
        },
        "llm": {
            "counts": dict(llm_counts),
            "percentages": {k: pct(v, total) for k, v in llm_counts.items()},
        },
        "agreement": {
            "comparable_prs": len(comparable),
            "agree_count": agree_count,
            "agreement_rate_pct": pct(agree_count, len(comparable)),
            "top_disagreements": disagreements.most_common(15),
            "error_count": sum(1 for r in rows if r["llm_category"] == "error"),
        },
        "notes": [
            "rules_category is the deterministic rulebook; llm_category is the LLM judging the same signals.",
            "Bot-authored PRs are labeled 'automated' by both, before discussion is counted.",
            "agree=True means both methods produced the same label for that PR.",
            "rich_task requires a linked issue; detection uses closingIssuesReferences + body regex. "
            "Issues linked only via the GitHub UI sidebar may be undercounted.",
            "Percentages are over all analyzed merged PRs (including automated).",
        ],
    }


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        logger.warning("No rows to write to CSV.")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote per-PR CSV: %s", path)


def write_json(path: Path, payload: Dict[str, Any], label: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.info("Wrote %s: %s", label, path)


def safe_repo_slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", repo)


PROFILE_CATEGORIES = [
    "simple_fix",
    "standard_feature_work",
    "rich_task",
    "other",
    "automated",
]


def _pct_columns(prefix: str, percentages: Dict[str, Any]) -> Dict[str, Any]:
    return {
        f"{prefix}_{cat}_pct": percentages.get(cat, 0.0 if percentages else "")
        for cat in PROFILE_CATEGORIES
    }


def build_org_repo_row(
    repository: str,
    summary: Dict[str, Any],
    platform: str = "",
) -> Dict[str, Any]:
    total = summary.get("total_merged_prs_analyzed", 0)
    rules_pct = summary.get("rules", {}).get("percentages", {})
    llm_pct = summary.get("llm", {}).get("percentages", {})
    agreement = summary.get("agreement", {})
    row: Dict[str, Any] = {
        "repository": repository,
        "platform": platform,
        "total_prs": total,
        "agreement_rate_pct": agreement.get("agreement_rate_pct", ""),
        "llm_error_count": agreement.get("error_count", 0),
    }
    row.update(_pct_columns("rules", rules_pct if total else {}))
    row.update(_pct_columns("llm", llm_pct if total else {}))
    return row


def build_org_total_row(rows: List[Dict[str, Any]], label: str = "org total") -> Dict[str, Any]:
    data = [r for r in rows if int(r.get("total_prs") or 0) > 0]
    total_prs = sum(int(r["total_prs"]) for r in data)
    total_row: Dict[str, Any] = {
        "repository": label,
        "platform": "",
        "total_prs": total_prs,
        "agreement_rate_pct": "",
        "llm_error_count": sum(int(r.get("llm_error_count") or 0) for r in data),
    }
    if not total_prs:
        for prefix in ("rules", "llm"):
            for cat in PROFILE_CATEGORIES:
                total_row[f"{prefix}_{cat}_pct"] = ""
        return total_row

    for prefix in ("rules", "llm"):
        counts = {cat: 0.0 for cat in PROFILE_CATEGORIES}
        for r in data:
            n = int(r["total_prs"])
            for cat in PROFILE_CATEGORIES:
                pct = r.get(f"{prefix}_{cat}_pct")
                if pct not in ("", None):
                    counts[cat] += n * float(pct) / 100.0
        for cat in PROFILE_CATEGORIES:
            total_row[f"{prefix}_{cat}_pct"] = round(counts[cat] / total_prs * 100, 2)

    comparable = 0
    agree = 0
    for r in data:
        n = int(r["total_prs"])
        rate = r.get("agreement_rate_pct")
        if rate not in ("", None):
            comparable += n
            agree += n * float(rate) / 100.0
    total_row["agreement_rate_pct"] = round(agree / comparable * 100, 2) if comparable else ""
    return total_row


def write_org_summary(
    run_dir: Path,
    per_repo_summaries: Dict[str, Any],
    platform_by_repo: Dict[str, str],
    metadata: Dict[str, Any],
    combined_summary: Dict[str, Any],
    failed_repos: Dict[str, str],
) -> Tuple[Path, Path]:
    repo_rows = [
        build_org_repo_row(repo, summary, platform_by_repo.get(repo, ""))
        for repo, summary in per_repo_summaries.items()
    ]
    repo_rows.sort(key=lambda r: (-int(r.get("total_prs") or 0), r["repository"]))
    org_total = build_org_total_row(repo_rows, label="org total")

    csv_rows = repo_rows + [org_total]
    org_csv = run_dir / "org_summary.csv"
    write_csv(org_csv, csv_rows)

    org_json = run_dir / "org_summary.json"
    payload = {
        "metadata": metadata,
        "org_total": org_total,
        "combined_summary": combined_summary,
        "repositories": repo_rows,
        "failures": failed_repos,
    }
    write_json(org_json, payload, label="org-level repo summary")
    return org_csv, org_json


def create_run_zip(
    run_dir: Path,
    run_id: str,
    org_csv: Path,
    org_json: Path,
    log_path: Path,
    failures_path: Optional[Path] = None,
) -> Path:
    zip_path = run_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in (org_csv, org_json, log_path):
            if path.exists():
                zf.write(path, arcname=path.name)
        if failures_path and failures_path.exists():
            zf.write(failures_path, arcname=failures_path.name)
    logger.info("Wrote run archive: %s", zip_path)
    return zip_path


def log_summary_block(title: str, summary: Dict[str, Any]) -> None:
    logger.info("-" * 70)
    logger.info("SUMMARY: %s", title)
    logger.info("Total merged PRs analyzed : %d", summary["total_merged_prs_analyzed"])
    logger.info("Rules distribution        : %s", summary["rules"]["percentages"])
    logger.info("LLM distribution          : %s", summary["llm"]["percentages"])
    logger.info("Agreement rate            : %.2f%% (%d/%d)",
                summary["agreement"]["agreement_rate_pct"],
                summary["agreement"]["agree_count"],
                summary["agreement"]["comparable_prs"])
    logger.info("Top disagreements         : %s", summary["agreement"]["top_disagreements"][:5])
    logger.info("-" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitHub PR task-profile report (deterministic rules + LLM). "
                    "Scan one repo, many repos, a whole org, or a user's repos.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo", action="append", default=None,
        help="owner/name for a single repo, OR a bare owner to scan all its repos. "
             "Repeatable and comma-separated (e.g. --repo a/b,c/d).",
    )
    parser.add_argument(
        "--org", action="append", default=None,
        help="Organization login; scans all its repos. Repeatable / comma-separated.",
    )
    parser.add_argument(
        "--user", action="append", default=None,
        help="User login; scans all their repos. Repeatable / comma-separated.",
    )
    parser.add_argument(
        "--gitlab-group", action="append", default=None,
        help="GitLab group; scans all its projects (include_subgroups). Repeatable / comma-separated.",
    )
    parser.add_argument(
        "--gitlab-project", action="append", default=None,
        help="GitLab project path (group/project). Repeatable / comma-separated.",
    )
    parser.add_argument(
        "--bitbucket-repo", action="append", default=None,
        help="Bitbucket repo path (workspace/repo). Repeatable / comma-separated.",
    )
    parser.add_argument("--include-archived", action="store_true",
                        help="Include archived repos when expanding an org/user.")
    parser.add_argument("--no-forks", dest="include_forks", action="store_false",
                        help="Exclude forked repos (default: forks excluded).")
    parser.add_argument("--include-forks", dest="include_forks", action="store_true",
                        help="Include forked repos when expanding an org/user.")
    parser.set_defaults(include_forks=False)
    parser.add_argument("--output-dir", default="outputs", help="Base directory for report files.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model for the LLM pass.")
    parser.add_argument("--max-workers", type=int, default=6, help="Parallel LLM calls (sync fallback only).")
    parser.add_argument(
        "--llm-mode",
        choices=("auto", "batch", "sync"),
        default="auto",
        help="How PR classification calls the LLM: 'batch' uses the OpenAI Batch API "
        "(one submission for the whole repo, ~50%% cheaper, no live-request-per-PR "
        "cost), 'sync' is one live chat.completions.create call per PR, 'auto' "
        "(default) batches once a repo has --llm-batch-threshold or more PRs to "
        "classify and uses sync below that.",
    )
    parser.add_argument(
        "--llm-batch-threshold",
        type=int,
        default=50,
        help="PR count at/above which --llm-mode=auto switches to the Batch API.",
    )
    parser.add_argument("--page-size", type=int, default=50,
                        help="Merged PRs per GraphQL page (lower if you see 502 errors).")
    parser.add_argument("--sleep", type=float, default=0.2, help="Sleep seconds between GraphQL pages.")
    parser.add_argument("--verbose", action="store_true", help="Verbose (DEBUG) console output.")
    args = parser.parse_args()

    has_github = bool(args.repo or args.org or args.user)
    has_gitlab = bool(args.gitlab_group or args.gitlab_project)
    has_bitbucket = bool(args.bitbucket_repo)
    if not (has_github or has_gitlab or has_bitbucket):
        parser.error(
            "Provide at least one of --repo, --org, --user, --gitlab-group, "
            "--gitlab-project, or --bitbucket-repo."
        )

    started_at = datetime.now(timezone.utc)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S")

    # Provisional run_id; refined once we know how many repos resolved.
    run_id = f"scan_{timestamp}"
    output_dir = Path(args.output_dir)
    run_dir = output_dir / run_id
    repos_dir = run_dir / "repos"
    run_dir.mkdir(parents=True, exist_ok=True)
    repos_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{run_id}.log"

    setup_logging(log_path, verbose=args.verbose)

    logger.info("=" * 70)
    logger.info("PR Task-Profile Report v%s", VERSION)
    logger.info("run_id        : %s", run_id)
    logger.info("targets       : repo=%s org=%s user=%s", args.repo, args.org, args.user)
    logger.info("gitlab        : group=%s project=%s", args.gitlab_group, args.gitlab_project)
    logger.info("llm model     : %s", args.model)
    logger.info("page size     : %s", args.page_size)
    logger.info("max workers   : %s", args.max_workers)
    logger.info("include forks : %s | include archived: %s", args.include_forks, args.include_archived)
    logger.info("output dir    : %s", run_dir.resolve())
    logger.info("=" * 70)

    github_token = os.getenv("GITHUB_TOKEN")
    gitlab_token = os.getenv("GITLAB_TOKEN")
    bitbucket_token = os.getenv("BITBUCKET_TOKEN")
    bitbucket_username = os.getenv("BITBUCKET_USERNAME", "")
    if has_github and not github_token:
        logger.error("GITHUB_TOKEN is not set (env or .env). Aborting.")
        sys.exit(1)
    if has_gitlab and not gitlab_token:
        logger.error("GITLAB_TOKEN is not set (env or .env). Aborting.")
        sys.exit(1)
    if has_bitbucket and not bitbucket_token:
        logger.warning("BITBUCKET_TOKEN not set — using anonymous Bitbucket access (public repos only, low rate limit).")
    if not llm_available():
        logger.error(
            "No LLM configured. Set OPENAI_API_KEY, or Azure "
            "(AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY). The LLM pass is "
            "required. Aborting."
        )
        sys.exit(1)

    scan_targets: List[Tuple[str, str]] = []  # (platform, repo_or_project)

    if has_github:
        try:
            github_repos = resolve_targets(
                token=github_token,
                repo_args=args.repo,
                org_args=args.org,
                user_args=args.user,
                include_archived=args.include_archived,
                include_forks=args.include_forks,
            )
            scan_targets.extend(("github", r) for r in github_repos)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to resolve GitHub targets: %s", exc)
            sys.exit(1)

    if has_gitlab:
        try:
            gitlab_projects = resolve_gitlab_targets(
                token=gitlab_token,
                group_args=args.gitlab_group,
                project_args=args.gitlab_project,
                include_archived=args.include_archived,
            )
            scan_targets.extend(("gitlab", p) for p in gitlab_projects)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to resolve GitLab targets: %s", exc)
            sys.exit(1)

    if has_bitbucket:
        bb_repos: List[str] = []
        seen_bb: set = set()
        for raw in args.bitbucket_repo:
            for part in raw.split(","):
                r = part.strip().strip("/")
                if r and r not in seen_bb:
                    seen_bb.add(r)
                    bb_repos.append(r)
        scan_targets.extend(("bitbucket", r) for r in bb_repos)

    if not scan_targets:
        logger.warning("No repositories resolved from the given targets. Nothing to do.")
        sys.exit(0)

    logger.info("Resolved %d repositor%s to scan:", len(scan_targets), "y" if len(scan_targets) == 1 else "ies")
    for platform, name in scan_targets:
        logger.info("  - [%s] %s", platform, name)

    # One OpenAI client shared across all repos.
    client = safe_openai()

    all_rows: List[Dict[str, Any]] = []
    per_repo_summaries: Dict[str, Any] = {}
    platform_by_repo: Dict[str, str] = {}
    failed_repos: Dict[str, str] = {}

    for i, (platform, repo) in enumerate(scan_targets, start=1):
        logger.info("=" * 70)
        logger.info("[%d/%d] Scanning [%s] %s", i, len(scan_targets), platform, repo)
        logger.info("=" * 70)
        try:
            if platform == "github":
                prs = fetch_merged_prs(
                    token=github_token,
                    repo=repo,
                    sleep_seconds=args.sleep,
                    checkpoint_dir=Path(args.output_dir) / "checkpoints",
                    page_size=args.page_size,
                )
            elif platform == "bitbucket":
                prs = fetch_merged_bitbucket_prs(
                    token=bitbucket_token,
                    repo=repo,
                    sleep_seconds=args.sleep,
                    username=bitbucket_username,
                    checkpoint_dir=Path(args.output_dir) / "checkpoints",
                )
            else:
                prs = fetch_merged_gitlab_mrs(
                    token=gitlab_token,
                    project=repo,
                    sleep_seconds=args.sleep,
                    checkpoint_dir=Path(args.output_dir) / "checkpoints",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to fetch PRs for %s: %s", repo, exc)
            failed_repos[repo] = f"fetch failed: {exc}"
            continue

        platform_by_repo[repo] = platform

        if not prs:
            logger.warning("No merged PRs found for %s. Skipping.", repo)
            per_repo_summaries[repo] = {"total_merged_prs_analyzed": 0}
            continue

        try:
            rows = process_prs(
                prs, model=args.model, max_workers=args.max_workers,
                repo=repo, client=client,
                batch_work_dir=Path(args.output_dir) / "batch_state" / safe_repo_slug(repo),
                llm_mode=args.llm_mode,
                llm_batch_threshold=args.llm_batch_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Classification failed for %s: %s", repo, exc)
            failed_repos[repo] = f"classification failed: {exc}"
            continue

        repo_summary = build_summary(rows)
        per_repo_summaries[repo] = repo_summary
        all_rows.extend(rows)

        slug = safe_repo_slug(repo)
        write_csv(repos_dir / f"{slug}.csv", rows)
        write_json(
            repos_dir / f"{slug}.json",
            {
                "metadata": {
                    "report_version": VERSION,
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "run_id": run_id,
                    "repository": repo,
                    "platform": platform,
                    "llm_model": args.model,
                    "tool": "org_analyser.pr_task_profile",
                },
                "summary": repo_summary,
                "results": rows,
            },
            label=f"per-repo report for {repo}",
        )
        log_summary_block(repo, repo_summary)

    if not all_rows and failed_repos:
        logger.error("No PRs were classified across any repo. See failures: %s", failed_repos)
        sys.exit(1)
    if not all_rows:
        logger.warning("No merged PRs found in any scanned repo — nothing to classify.")

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    combined_summary = build_summary(all_rows)

    combined_report = {
        "metadata": {
            "report_version": VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "llm_model": args.model,
            "started_at_utc": started_at.isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "tool": "org_analyser.pr_task_profile",
            "targets": {
                "repo": args.repo,
                "org": args.org,
                "user": args.user,
                "gitlab_group": args.gitlab_group,
                "gitlab_project": args.gitlab_project,
            },
            "repositories_scanned": list(per_repo_summaries.keys()),
            "repositories_failed": failed_repos,
            "repo_count": len(per_repo_summaries),
        },
        "combined_summary": combined_summary,
        "per_repository_summary": {
            repo: {
                "total_merged_prs_analyzed": s.get("total_merged_prs_analyzed", 0),
                "rules_percentages": s.get("rules", {}).get("percentages", {}),
                "llm_percentages": s.get("llm", {}).get("percentages", {}),
                "agreement_rate_pct": s.get("agreement", {}).get("agreement_rate_pct"),
            }
            for repo, s in per_repo_summaries.items()
        },
        "results": all_rows,
    }

    combined_json = run_dir / "combined_report.json"
    combined_csv = run_dir / "combined_per_pr.csv"
    write_json(combined_json, combined_report, label="combined report")
    write_csv(combined_csv, all_rows)

    org_csv, org_json = write_org_summary(
        run_dir=run_dir,
        per_repo_summaries=per_repo_summaries,
        platform_by_repo=platform_by_repo,
        metadata=combined_report["metadata"],
        combined_summary=combined_summary,
        failed_repos=failed_repos,
    )

    zip_path: Optional[Path] = None
    failures_path: Optional[Path] = None
    if failed_repos:
        logger.warning("Repos that failed: %s", failed_repos)
        failures_path = run_dir / "failures.json"
        write_json(failures_path, {"failures": failed_repos}, label="failures report")

    zip_path = create_run_zip(
        run_dir=run_dir,
        run_id=run_id,
        org_csv=org_csv,
        org_json=org_json,
        log_path=log_path,
        failures_path=failures_path,
    )

    log_summary_block(f"COMBINED across {len(per_repo_summaries)} repos", combined_summary)
    logger.info("Elapsed: %.1fs", elapsed)
    logger.info("Deliverables:")
    logger.info("  Org summary CSV : %s", org_csv.resolve())
    logger.info("  Org summary JSON: %s", org_json.resolve())
    logger.info("  Combined JSON   : %s", combined_json.resolve())
    logger.info("  Combined CSV    : %s", combined_csv.resolve())
    logger.info("  Per-repo dir    : %s", repos_dir.resolve())
    logger.info("  LOG             : %s", log_path.resolve())
    logger.info("  ZIP archive     : %s", zip_path.resolve())
    logger.info("Done.")


if __name__ == "__main__":
    main()
