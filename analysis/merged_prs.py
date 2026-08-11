#!/usr/bin/env python3
"""
Merged pull/merge-request counting across GitHub, GitLab, and Bitbucket.

Two subcommands:
  count   - count merged PRs/MRs for specific repos/orgs/groups/workspaces (ad hoc)
  export  - discover every org/group a token can see and export one CSV per
            org/group plus a summary CSV (batch)

Examples:
  export GITHUB_TOKEN=ghp_...
  export GITLAB_TOKEN=glpat-...

  merged-prs count --github-org my-org --gitlab-group my-group
  merged-prs count --github-repo owner/repo --gitlab-project group/project
  merged-prs count --github-org my-org --since 2025-01-01 --json
  merged-prs count --bitbucket-workspace my-workspace
  merged-prs export --tokens-file tokens --output-dir merged-pr-counts
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from platforms import bitbucket as bitbucket_platform
from platforms import github as github_platform
from platforms import gitlab as gitlab_platform
from platforms.base import ci_headers, request_with_retry
from platforms.errors import PlatformError

CSV_FIELDS = ["platform", "org", "repo", "merged_count", "error"]
SUMMARY_FIELDS = ["platform", "org", "repos_total", "merged_total", "token_name", "error"]

ProgressCb = Optional[Callable[[str], None]]


def _emit(progress_cb: ProgressCb, msg: str) -> None:
    """Default to stdout for standalone/CLI use; callers embedding this module
    in-process (e.g. the rich progress UI) pass a callback instead so raw
    prints never fight the terminal's live rendering."""
    if progress_cb is not None:
        progress_cb(msg)
    else:
        print(msg, flush=True)


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value!r}. Use YYYY-MM-DD or ISO8601.")


def in_range(dt_str: str | None, since: datetime | None, until: datetime | None) -> bool:
    if not dt_str:
        return False
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if since and dt < since:
        return False
    if until and dt > until:
        return False
    return True


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def list_github_repos(token: str, org: str, host: str) -> list[str]:
    return github_platform.list_repos(token, org, host)


def list_github_orgs(token: str, host: str = "github.com") -> list[str]:
    session = requests.Session()
    session.headers.update(github_platform.github_headers(token))
    api = github_platform.github_api(host)
    orgs: set[str] = set()

    for org in github_platform.paginate(session, f"{api}/user/orgs", params={"per_page": 100}):
        orgs.add(org["login"])

    repos = github_platform.paginate(
        session,
        f"{api}/user/repos",
        params={"affiliation": "owner,collaborator,organization_member", "per_page": 100},
    )
    for repo in repos:
        owner = repo.get("owner") or {}
        if owner.get("type") == "Organization":
            orgs.add(owner["login"])

    return sorted(orgs)


_GITHUB_MERGED_COUNT_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED) {
      totalCount
    }
  }
}
"""


def github_graphql(token: str, host: str = "github.com") -> str:
    return "https://api.github.com/graphql" if host == "github.com" else f"https://{host}/api/graphql"


def count_github_merged(
    token: str,
    repo: str,
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> int:
    owner, name = repo.split("/", 1)
    session = requests.Session()
    session.headers.update(github_platform.github_headers(token))

    if since is None and until is None:
        response = request_with_retry(
            session,
            "POST",
            github_graphql(token, host),
            json={
                "query": _GITHUB_MERGED_COUNT_QUERY,
                "variables": {"owner": owner, "name": name},
            },
        )
        if response is None:
            raise RuntimeError(f"Repository not found: {repo}")
        data = response.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:500])
        repository = (data.get("data") or {}).get("repository")
        if not repository:
            raise RuntimeError(f"Repository not found: {repo}")
        return int(repository["pullRequests"]["totalCount"])

    api = github_platform.github_api(host)
    pulls = github_platform.paginate(
        session,
        f"{api}/repos/{owner}/{name}/pulls",
        params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100},
    )
    return sum(
        1 for pr in pulls if pr.get("merged_at") and in_range(pr["merged_at"], since, until)
    )


def github_merged_counts(
    token: str,
    repos: list[str],
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, int]:
    return {
        repo: count_github_merged(token, repo, host, since, until)
        for repo in repos
    }


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

def list_gitlab_projects(token: str, group: str, host: str) -> list[str]:
    return gitlab_platform.list_projects(token, group, host)


def list_gitlab_top_level_groups(token: str, host: str = "gitlab.com") -> list[str]:
    return gitlab_platform.list_top_level_groups(token, host)


def count_gitlab_merged(
    token: str,
    project: str,
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> int:
    api = gitlab_platform.gitlab_api(host)
    encoded = urllib.parse.quote(project, safe="")
    params: dict[str, str] = {"state": "merged"}
    if since:
        params["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    if until:
        params["updated_before"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    session = requests.Session()
    session.headers.update(gitlab_platform.gitlab_headers(token))
    url = f"{api}/projects/{encoded}/merge_requests"

    # Probe with per_page=1 first -- GitLab returns the true total in
    # X-Total, cheaper than paginating through every MR just to count them.
    probe = request_with_retry(session, "GET", url, params={**params, "per_page": 1})
    if probe is not None:
        total = ci_headers(probe.headers).get("X-Total")
        if total is not None:
            return int(total)

    mrs = gitlab_platform.paginate(session, url, params=params)
    return len(mrs)


def gitlab_merged_counts(
    token: str,
    projects: list[str],
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, int]:
    return {
        project: count_gitlab_merged(token, project, host, since, until)
        for project in projects
    }


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------

def list_bitbucket_repos(token: str, workspace: str, username: str = "") -> list[str]:
    return bitbucket_platform.list_repos(token, workspace, username)


def count_bitbucket_merged(
    token: str,
    repo: str,
    username: str,
    since: datetime | None,
    until: datetime | None,
) -> int:
    workspace, name = repo.split("/", 1)
    session = requests.Session()
    session.headers.update(bitbucket_platform.bitbucket_headers(token, username))
    base = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{name}/pullrequests?state=MERGED"

    if since is None and until is None:
        # Bitbucket returns the total in `size` on the first page.
        response = request_with_retry(session, "GET", f"{base}&pagelen=1")
        if response is not None:
            data = response.json()
            if isinstance(data, dict) and "size" in data:
                return int(data["size"])
        # Fallback: full pagination if `size` is absent.
        return len(bitbucket_platform.paginate(session, f"{base}&pagelen=50"))

    prs = bitbucket_platform.paginate(session, f"{base}&pagelen=50")
    return sum(
        1 for pr in prs if in_range(pr.get("updated_on"), since, until)
    )


def bitbucket_merged_counts(
    token: str,
    repos: list[str],
    username: str,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, int]:
    return {
        repo: count_bitbucket_merged(token, repo, username, since, until)
        for repo in repos
    }


# ---------------------------------------------------------------------------
# `count` subcommand — ad hoc counting for explicit repos/orgs/groups
# ---------------------------------------------------------------------------

def _run_count(args: argparse.Namespace) -> int:
    since = parse_date(args.since)
    until = parse_date(args.until)

    github_repos = list(args.github_repo)
    gitlab_projects = list(args.gitlab_project)
    bitbucket_repos = list(args.bitbucket_repo)

    if args.github_token:
        for org in args.github_org:
            github_repos.extend(list_github_repos(args.github_token, org, args.github_host))
    elif args.github_repo or args.github_org:
        print("Error: GITHUB_TOKEN (or --github-token) required for GitHub.", file=sys.stderr)
        return 1

    if args.gitlab_token:
        for group in args.gitlab_group:
            gitlab_projects.extend(list_gitlab_projects(args.gitlab_token, group, args.gitlab_host))
    elif args.gitlab_project or args.gitlab_group:
        print("Error: GITLAB_TOKEN (or --gitlab-token) required for GitLab.", file=sys.stderr)
        return 1

    if args.bitbucket_workspace:
        for workspace in args.bitbucket_workspace:
            bitbucket_repos.extend(
                list_bitbucket_repos(args.bitbucket_token, workspace, args.bitbucket_username)
            )
    elif args.bitbucket_repo and not args.bitbucket_token and not args.bitbucket_username:
        print(
            "Warning: no BITBUCKET_TOKEN/--bitbucket-token set; querying Bitbucket anonymously.",
            file=sys.stderr,
        )

    if not github_repos and not gitlab_projects and not bitbucket_repos:
        print(
            "Provide at least one of --github-repo, --github-org, --gitlab-project, "
            "--gitlab-group, --bitbucket-repo, --bitbucket-workspace",
            file=sys.stderr,
        )
        return 1

    github_repos = sorted(set(github_repos))
    gitlab_projects = sorted(set(gitlab_projects))
    bitbucket_repos = sorted(set(bitbucket_repos))

    github_counts = (
        github_merged_counts(args.github_token, github_repos, args.github_host, since, until)
        if github_repos
        else {}
    )
    gitlab_counts = (
        gitlab_merged_counts(args.gitlab_token, gitlab_projects, args.gitlab_host, since, until)
        if gitlab_projects
        else {}
    )
    bitbucket_counts = (
        bitbucket_merged_counts(args.bitbucket_token, bitbucket_repos, args.bitbucket_username, since, until)
        if bitbucket_repos
        else {}
    )

    github_total = sum(github_counts.values())
    gitlab_total = sum(gitlab_counts.values())
    bitbucket_total = sum(bitbucket_counts.values())
    grand_total = github_total + gitlab_total + bitbucket_total

    result = {
        "github": {"repos": github_counts, "total": github_total},
        "gitlab": {"projects": gitlab_counts, "total": gitlab_total},
        "bitbucket": {"repos": bitbucket_counts, "total": bitbucket_total},
        "grand_total": grand_total,
        "since": args.since,
        "until": args.until,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("\nMerged PR/MR counts\n" + "=" * 40)
    if github_counts:
        print("\nGitHub:")
        for repo, count in github_counts.items():
            print(f"  {repo}: {count}")
        print(f"  GitHub subtotal: {github_total}")

    if gitlab_counts:
        print("\nGitLab:")
        for project, count in gitlab_counts.items():
            print(f"  {project}: {count}")
        print(f"  GitLab subtotal: {gitlab_total}")

    if bitbucket_counts:
        print("\nBitbucket:")
        for repo, count in bitbucket_counts.items():
            print(f"  {repo}: {count}")
        print(f"  Bitbucket subtotal: {bitbucket_total}")

    print(f"\nGrand total: {grand_total}")
    return 0


# ---------------------------------------------------------------------------
# `export` subcommand — batch export for every org/group a token can see
# ---------------------------------------------------------------------------

def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)


def parse_tokens_file(path: Path) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        tokens[key.strip()] = value.strip()
    return tokens


def list_github_users(token: str, host: str = "github.com") -> list[str]:
    session = requests.Session()
    session.headers.update(github_platform.github_headers(token))
    api = github_platform.github_api(host)
    users: set[str] = set()
    repos = github_platform.paginate(
        session,
        f"{api}/user/repos",
        params={"affiliation": "owner,collaborator,organization_member", "per_page": 100},
    )
    for repo in repos:
        owner = repo.get("owner") or {}
        if owner.get("type") == "User":
            users.add(owner["login"])
    return sorted(users)


def write_org_csv(
    path: Path,
    platform: str,
    org: str,
    rows: list[dict[str, Any]],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return sum(int(r["merged_count"]) for r in rows if r.get("merged_count"))


def export_github_org(
    token: str,
    org: str,
    token_name: str,
    output_dir: Path,
    host: str = "github.com",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    filename = safe_filename(f"github_{org}.csv")
    out_path = output_dir / filename
    rows: list[dict[str, Any]] = []
    org_error = ""

    try:
        repos = list_github_repos(token, org, host)
    except Exception as exc:
        org_error = str(exc)
        rows.append(
            {"platform": "github", "org": org, "repo": "", "merged_count": 0, "error": org_error}
        )
        write_org_csv(out_path, "github", org, rows)
        return {
            "platform": "github",
            "org": org,
            "repos_total": 0,
            "merged_total": 0,
            "token_name": token_name,
            "error": org_error,
            "csv_path": str(out_path),
        }

    _emit(progress_cb, f"  GitHub {org}: {len(repos)} repos")
    for idx, repo in enumerate(repos, start=1):
        error = ""
        count = 0
        try:
            count = count_github_merged(token, repo, host, None, None)
        except Exception as exc:
            error = str(exc)
        rows.append(
            {"platform": "github", "org": org, "repo": repo, "merged_count": count, "error": error}
        )
        if idx % 10 == 0 or idx == len(repos):
            _emit(progress_cb, f"    [{idx}/{len(repos)}] latest={repo} count={count}")

    merged_total = write_org_csv(out_path, "github", org, rows)
    return {
        "platform": "github",
        "org": org,
        "repos_total": len(repos),
        "merged_total": merged_total,
        "token_name": token_name,
        "error": org_error,
        "csv_path": str(out_path),
    }


def export_gitlab_project(
    token: str,
    project: str,
    token_name: str,
    output_dir: Path,
    host: str = "gitlab.com",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """Export merged MR count for a single GitLab project (group/subgroup/project)."""
    project = project.strip().strip("/")
    namespace = "/".join(project.split("/")[:-1]) or project
    filename = safe_filename(f"gitlab_{project.replace('/', '_')}.csv")
    out_path = output_dir / filename
    error = ""
    count = 0
    try:
        count = count_gitlab_merged(token, project, host, None, None)
    except Exception as exc:
        error = str(exc)

    rows = [
        {"platform": "gitlab", "org": namespace, "repo": project, "merged_count": count, "error": error}
    ]
    merged_total = write_org_csv(out_path, "gitlab", namespace, rows)
    _emit(progress_cb, f"  GitLab project {project}: merged_count={count}")
    return {
        "platform": "gitlab",
        "org": namespace,
        "project": project,
        "repos_total": 1,
        "merged_total": merged_total,
        "token_name": token_name,
        "error": error,
        "csv_path": str(out_path),
    }


def export_github_repos(
    token: str,
    repos: list[str],
    token_name: str,
    output_dir: Path,
    host: str = "github.com",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """Export merged PR counts for one or more specific GitHub repos (owner/repo)."""
    normalized: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        r = repo.strip().strip("/")
        if not r or r in seen:
            continue
        if "/" not in r:
            raise ValueError(f"GitHub repo must be owner/repo (got {r!r})")
        seen.add(r)
        normalized.append(r)
    if not normalized:
        raise ValueError("At least one GitHub repo path is required")

    label = normalized[0].replace("/", "_") if len(normalized) == 1 else f"github_repos_{len(normalized)}"
    filename = safe_filename(f"{label}.csv")
    out_path = output_dir / filename
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    org_error = ""

    _emit(progress_cb, f"  GitHub repos: {len(normalized)} repo(s)")
    for idx, repo in enumerate(normalized, start=1):
        owner = repo.split("/")[0]
        error = ""
        count = 0
        try:
            count = count_github_merged(token, repo, host, None, None)
        except Exception as exc:
            error = str(exc)
            org_error = org_error or error
        rows.append(
            {"platform": "github", "org": owner, "repo": repo, "merged_count": count, "error": error}
        )
        _emit(progress_cb, f"    [{idx}/{len(normalized)}] {repo} count={count}")

    merged_total = write_org_csv(out_path, "github", normalized[0].split("/")[0], rows)
    return {
        "platform": "github",
        "repos": normalized,
        "repos_total": len(normalized),
        "merged_total": merged_total,
        "token_name": token_name,
        "error": org_error,
        "csv_path": str(out_path),
    }


def _export_bitbucket(
    token: str,
    repos: list[str],
    workspace_label: str,
    token_name: str,
    output_dir: Path,
    username: str = "",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """Shared merged-PR-count export for Bitbucket repos (workspace/repo)."""
    filename = safe_filename(f"{workspace_label}.csv")
    out_path = output_dir / filename
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    org_error = ""

    _emit(progress_cb, f"  Bitbucket repos: {len(repos)} repo(s)")
    for idx, repo in enumerate(repos, start=1):
        workspace = repo.split("/")[0]
        error = ""
        count = 0
        try:
            count = count_bitbucket_merged(token, repo, username, None, None)
        except Exception as exc:
            error = str(exc)
            org_error = org_error or error
        rows.append(
            {"platform": "bitbucket", "org": workspace, "repo": repo, "merged_count": count, "error": error}
        )
        _emit(progress_cb, f"    [{idx}/{len(repos)}] {repo} count={count}")

    merged_total = write_org_csv(out_path, "bitbucket", repos[0].split("/")[0], rows)
    return {
        "platform": "bitbucket",
        "repos": repos,
        "repos_total": len(repos),
        "merged_total": merged_total,
        "token_name": token_name,
        "error": org_error,
        "csv_path": str(out_path),
    }


def export_bitbucket_workspace(
    token: str,
    workspace: str,
    token_name: str,
    output_dir: Path,
    username: str = "",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """Export merged PR counts for every repo in a Bitbucket workspace."""
    repos = list_bitbucket_repos(token, workspace, username)
    return _export_bitbucket(token, repos, workspace, token_name, output_dir, username, progress_cb)


def export_bitbucket_repos(
    token: str,
    repos: list[str],
    token_name: str,
    output_dir: Path,
    username: str = "",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """Export merged PR counts for one or more specific Bitbucket repos."""
    normalized: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        r = repo.strip().strip("/")
        if not r or r in seen:
            continue
        if "/" not in r:
            raise ValueError(f"Bitbucket repo must be workspace/repo (got {r!r})")
        seen.add(r)
        normalized.append(r)
    if not normalized:
        raise ValueError("At least one Bitbucket repo path is required")
    label = normalized[0].replace("/", "_") if len(normalized) == 1 else f"bitbucket_repos_{len(normalized)}"
    return _export_bitbucket(token, normalized, label, token_name, output_dir, username, progress_cb)


def export_gitlab_projects(
    token: str,
    projects: list[str],
    token_name: str,
    output_dir: Path,
    host: str = "gitlab.com",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """Export merged MR counts for one or more GitLab projects into a single CSV."""
    normalized = []
    seen: set[str] = set()
    for project in projects:
        path = project.strip().strip("/")
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    if not normalized:
        raise ValueError("At least one GitLab project path is required")
    if len(normalized) == 1:
        return export_gitlab_project(token, normalized[0], token_name, output_dir, host, progress_cb)

    filename = safe_filename(f"gitlab_projects_{len(normalized)}.csv")
    out_path = output_dir / filename
    rows: list[dict[str, Any]] = []
    org_error = ""

    _emit(progress_cb, f"  GitLab projects batch: {len(normalized)} projects")
    for idx, project in enumerate(normalized, start=1):
        namespace = "/".join(project.split("/")[:-1]) or project
        error = ""
        count = 0
        try:
            count = count_gitlab_merged(token, project, host, None, None)
        except Exception as exc:
            error = str(exc)
            if not org_error:
                org_error = error
        rows.append(
            {"platform": "gitlab", "org": namespace, "repo": project, "merged_count": count, "error": error}
        )
        if idx % 10 == 0 or idx == len(normalized):
            _emit(progress_cb, f"    [{idx}/{len(normalized)}] latest={project} count={count}")

    merged_total = write_org_csv(out_path, "gitlab", "gitlab-projects", rows)
    return {
        "platform": "gitlab",
        "org": "gitlab-projects",
        "projects": normalized,
        "repos_total": len(normalized),
        "merged_total": merged_total,
        "token_name": token_name,
        "error": org_error,
        "csv_path": str(out_path),
    }


def export_gitlab_group(
    token: str,
    group: str,
    token_name: str,
    output_dir: Path,
    host: str = "gitlab.com",
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    filename = safe_filename(f"gitlab_{group.replace('/', '_')}.csv")
    out_path = output_dir / filename
    rows: list[dict[str, Any]] = []
    org_error = ""

    try:
        projects = list_gitlab_projects(token, group, host)
    except Exception as exc:
        org_error = str(exc)
        rows.append(
            {"platform": "gitlab", "org": group, "repo": "", "merged_count": 0, "error": org_error}
        )
        write_org_csv(out_path, "gitlab", group, rows)
        return {
            "platform": "gitlab",
            "org": group,
            "repos_total": 0,
            "merged_total": 0,
            "token_name": token_name,
            "error": org_error,
            "csv_path": str(out_path),
        }

    _emit(progress_cb, f"  GitLab {group}: {len(projects)} projects")
    for idx, project in enumerate(projects, start=1):
        error = ""
        count = 0
        try:
            count = count_gitlab_merged(token, project, host, None, None)
        except Exception as exc:
            error = str(exc)
        rows.append(
            {"platform": "gitlab", "org": group, "repo": project, "merged_count": count, "error": error}
        )
        if idx % 10 == 0 or idx == len(projects):
            _emit(progress_cb, f"    [{idx}/{len(projects)}] latest={project} count={count}")

    merged_total = write_org_csv(out_path, "gitlab", group, rows)
    return {
        "platform": "gitlab",
        "org": group,
        "repos_total": len(projects),
        "merged_total": merged_total,
        "token_name": token_name,
        "error": org_error,
        "csv_path": str(out_path),
    }


def _run_export(args: argparse.Namespace) -> int:
    tokens_path = Path(args.tokens_file)
    if not tokens_path.is_file():
        print(f"Tokens file not found: {tokens_path}", file=sys.stderr)
        return 1

    tokens = parse_tokens_file(tokens_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    github_token_name = "github-data-token"
    gitlab_token_name = "gitlab_token"
    github_token = tokens.get(github_token_name)
    gitlab_token = tokens.get(gitlab_token_name)

    if not github_token:
        print(f"Missing {github_token_name} in {tokens_path}", file=sys.stderr)
        return 1
    if not gitlab_token:
        print(f"Missing {gitlab_token_name} in {tokens_path}", file=sys.stderr)
        return 1

    started = datetime.now(timezone.utc).isoformat()
    summary_rows: list[dict[str, Any]] = []

    print("Discovering GitHub orgs...", flush=True)
    github_orgs = list_github_orgs(github_token, args.github_host)
    print(f"Found {len(github_orgs)} GitHub orgs: {', '.join(github_orgs)}", flush=True)

    for org in github_orgs:
        print(f"Exporting GitHub org: {org}", flush=True)
        summary_rows.append(
            export_github_org(github_token, org, github_token_name, output_dir, args.github_host)
        )

    print("Discovering GitLab groups...", flush=True)
    gitlab_groups = list_gitlab_top_level_groups(gitlab_token, args.gitlab_host)
    print(f"Found {len(gitlab_groups)} GitLab top-level groups: {', '.join(gitlab_groups)}", flush=True)

    for group in gitlab_groups:
        print(f"Exporting GitLab group: {group}", flush=True)
        summary_rows.append(
            export_gitlab_group(gitlab_token, group, gitlab_token_name, output_dir, args.gitlab_host)
        )

    summary_path = output_dir / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in summary_rows:
            writer.writerow({k: row.get(k, "") for k in SUMMARY_FIELDS})

    manifest = {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "github_token": github_token_name,
        "gitlab_token": gitlab_token_name,
        "github_orgs": github_orgs,
        "gitlab_groups": gitlab_groups,
        "summary": summary_rows,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    grand_total = sum(int(r["merged_total"]) for r in summary_rows)
    print(f"\nDone. Wrote {len(summary_rows)} org CSVs to {output_dir}", flush=True)
    print(f"Grand total merged PRs/MRs: {grand_total}", flush=True)
    print(f"Summary: {summary_path}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Count/export merged PRs/MRs on GitHub, GitLab, and Bitbucket")
    sub = parser.add_subparsers(dest="command", required=True)

    count_p = sub.add_parser("count", help="Count merged PRs/MRs for specific repos/orgs/groups/workspaces")
    count_p.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
    )
    count_p.add_argument(
        "--gitlab-token",
        default=os.environ.get("GITLAB_TOKEN") or os.environ.get("GLAB_TOKEN"),
    )
    count_p.add_argument(
        "--bitbucket-token",
        default=os.environ.get("BITBUCKET_TOKEN") or os.environ.get("BITBUCKET_APP_PASSWORD"),
    )
    count_p.add_argument(
        "--bitbucket-username",
        default=os.environ.get("BITBUCKET_USERNAME") or os.environ.get("ATLASSIAN_EMAIL") or os.environ.get("BITBUCKET_EMAIL", ""),
    )
    count_p.add_argument("--github-host", default=os.environ.get("GITHUB_HOST", "github.com"))
    count_p.add_argument("--gitlab-host", default=os.environ.get("GITLAB_HOST", "gitlab.com"))
    count_p.add_argument("--github-repo", action="append", default=[], help="owner/repo")
    count_p.add_argument("--github-org", action="append", default=[], help="GitHub org or user")
    count_p.add_argument("--gitlab-project", action="append", default=[], help="group/project")
    count_p.add_argument("--gitlab-group", action="append", default=[], help="GitLab group")
    count_p.add_argument("--bitbucket-repo", action="append", default=[], help="workspace/repo")
    count_p.add_argument("--bitbucket-workspace", action="append", default=[], help="Bitbucket workspace")
    count_p.add_argument("--since", help="Only count merges on/after YYYY-MM-DD")
    count_p.add_argument("--until", help="Only count merges on/before YYYY-MM-DD")
    count_p.add_argument("--json", action="store_true", help="Print JSON output")

    export_p = sub.add_parser("export", help="Batch-export merged PR/MR counts for every accessible org/group")
    export_p.add_argument("--tokens-file", default="tokens", help="Path to tokens file")
    export_p.add_argument("--output-dir", default="merged-pr-counts", help="Folder to write per-org CSV files")
    export_p.add_argument("--github-host", default="github.com")
    export_p.add_argument("--gitlab-host", default="gitlab.com")

    args = parser.parse_args()
    try:
        if args.command == "count":
            return _run_count(args)
        return _run_export(args)
    except PlatformError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
