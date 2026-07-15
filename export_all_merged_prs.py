#!/usr/bin/env python3
"""
Export merged PR/MR counts for every org/group accessible via tokens in a tokens file.

Writes one CSV per org under the output folder, plus a summary CSV.

Usage:
    python export_all_merged_prs.py --tokens-file tokens --output-dir merged-pr-counts
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from count_merged_prs import (
    count_bitbucket_merged,
    count_github_merged,
    count_gitlab_merged,
    github_api,
    gitlab_api,
    list_bitbucket_repos,
    list_github_repos,
    list_gitlab_projects,
    paginate_github,
    paginate_gitlab,
)


CSV_FIELDS = ["platform", "org", "repo", "merged_count", "error"]
SUMMARY_FIELDS = ["platform", "org", "repos_total", "merged_total", "token_name", "error"]


def parse_tokens_file(path: Path) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        tokens[key.strip()] = value.strip()
    return tokens


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)


def list_github_orgs(token: str, host: str = "github.com") -> list[str]:
    api = github_api(token, host)
    orgs: set[str] = set()

    for org in paginate_github(f"{api}/user/orgs?per_page=100", token):
        orgs.add(org["login"])

    repos = paginate_github(
        f"{api}/user/repos?affiliation=owner,collaborator,organization_member&per_page=100",
        token,
    )
    for repo in repos:
        owner = repo.get("owner") or {}
        if owner.get("type") == "Organization":
            orgs.add(owner["login"])

    return sorted(orgs)


def list_gitlab_top_level_groups(token: str, host: str = "gitlab.com") -> list[str]:
    api = gitlab_api(host)
    groups = paginate_gitlab(api, "/groups", token, {"min_access_level": "10"})
    paths = sorted({g["full_path"] for g in groups if g.get("full_path")})
    top_level: list[str] = []
    for path in paths:
        if any(path.startswith(parent + "/") for parent in top_level):
            continue
        top_level.append(path)
    return top_level


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
            {
                "platform": "github",
                "org": org,
                "repo": "",
                "merged_count": 0,
                "error": org_error,
            }
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

    print(f"  GitHub {org}: {len(repos)} repos", flush=True)
    for idx, repo in enumerate(repos, start=1):
        error = ""
        count = 0
        try:
            count = count_github_merged(token, repo, host, None, None)
        except Exception as exc:
            error = str(exc)
        rows.append(
            {
                "platform": "github",
                "org": org,
                "repo": repo,
                "merged_count": count,
                "error": error,
            }
        )
        if idx % 10 == 0 or idx == len(repos):
            print(f"    [{idx}/{len(repos)}] latest={repo} count={count}", flush=True)

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
        {
            "platform": "gitlab",
            "org": namespace,
            "repo": project,
            "merged_count": count,
            "error": error,
        }
    ]
    merged_total = write_org_csv(out_path, "gitlab", namespace, rows)
    print(f"  GitLab project {project}: merged_count={count}", flush=True)
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

    print(f"  GitHub repos: {len(normalized)} repo(s)", flush=True)
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
            {
                "platform": "github",
                "org": owner,
                "repo": repo,
                "merged_count": count,
                "error": error,
            }
        )
        print(f"    [{idx}/{len(normalized)}] {repo} count={count}", flush=True)

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
) -> dict[str, Any]:
    """Shared merged-PR-count export for Bitbucket repos (workspace/repo)."""
    filename = safe_filename(f"{workspace_label}.csv")
    out_path = output_dir / filename
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    org_error = ""

    print(f"  Bitbucket repos: {len(repos)} repo(s)", flush=True)
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
            {
                "platform": "bitbucket",
                "org": workspace,
                "repo": repo,
                "merged_count": count,
                "error": error,
            }
        )
        print(f"    [{idx}/{len(repos)}] {repo} count={count}", flush=True)

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
) -> dict[str, Any]:
    """Export merged PR counts for every repo in a Bitbucket workspace."""
    repos = list_bitbucket_repos(token, workspace, username)
    return _export_bitbucket(token, repos, workspace, token_name, output_dir, username)


def export_bitbucket_repos(
    token: str,
    repos: list[str],
    token_name: str,
    output_dir: Path,
    username: str = "",
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
    return _export_bitbucket(token, normalized, label, token_name, output_dir, username)


def export_gitlab_projects(
    token: str,
    projects: list[str],
    token_name: str,
    output_dir: Path,
    host: str = "gitlab.com",
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
        return export_gitlab_project(token, normalized[0], token_name, output_dir, host)

    filename = safe_filename(f"gitlab_projects_{len(normalized)}.csv")
    out_path = output_dir / filename
    rows: list[dict[str, Any]] = []
    org_error = ""

    print(f"  GitLab projects batch: {len(normalized)} projects", flush=True)
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
            {
                "platform": "gitlab",
                "org": namespace,
                "repo": project,
                "merged_count": count,
                "error": error,
            }
        )
        if idx % 10 == 0 or idx == len(normalized):
            print(f"    [{idx}/{len(normalized)}] latest={project} count={count}", flush=True)

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
            {
                "platform": "gitlab",
                "org": group,
                "repo": "",
                "merged_count": 0,
                "error": org_error,
            }
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

    print(f"  GitLab {group}: {len(projects)} projects", flush=True)
    for idx, project in enumerate(projects, start=1):
        error = ""
        count = 0
        try:
            count = count_gitlab_merged(token, project, host, None, None)
        except Exception as exc:
            error = str(exc)
        rows.append(
            {
                "platform": "gitlab",
                "org": group,
                "repo": project,
                "merged_count": count,
                "error": error,
            }
        )
        if idx % 10 == 0 or idx == len(projects):
            print(f"    [{idx}/{len(projects)}] latest={project} count={count}", flush=True)

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Export merged PR/MR counts for all orgs")
    parser.add_argument("--tokens-file", default="tokens", help="Path to tokens file")
    parser.add_argument(
        "--output-dir",
        default="merged-pr-counts",
        help="Folder to write per-org CSV files",
    )
    parser.add_argument("--github-host", default="github.com")
    parser.add_argument("--gitlab-host", default="gitlab.com")
    args = parser.parse_args()

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
            export_github_org(
                github_token,
                org,
                github_token_name,
                output_dir,
                args.github_host,
            )
        )

    print("Discovering GitLab groups...", flush=True)
    gitlab_groups = list_gitlab_top_level_groups(gitlab_token, args.gitlab_host)
    print(f"Found {len(gitlab_groups)} GitLab top-level groups: {', '.join(gitlab_groups)}", flush=True)

    for group in gitlab_groups:
        print(f"Exporting GitLab group: {group}", flush=True)
        summary_rows.append(
            export_gitlab_group(
                gitlab_token,
                group,
                gitlab_token_name,
                output_dir,
                args.gitlab_host,
            )
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
    (output_dir / "manifest.json").write_text(
        __import__("json").dumps(manifest, indent=2),
        encoding="utf-8",
    )

    grand_total = sum(int(r["merged_total"]) for r in summary_rows)
    print(f"\nDone. Wrote {len(summary_rows)} org CSVs to {output_dir}", flush=True)
    print(f"Grand total merged PRs/MRs: {grand_total}", flush=True)
    print(f"Summary: {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
