#!/usr/bin/env python3
"""
Discover all GitHub/GitLab/Bitbucket organizations (groups/workspaces) & repos
accessible via a token, then run repo_evaluator.py on each one.

Modes
─────
  --dry-run   List every org/group and repo the token can see (no evaluation).
  --run       Actually execute repo_evaluator.py for each repo.

Quick start
───────────
  # GitHub (default):
  python run_all_repos.py --dry-run
  python run_all_repos.py --dry-run --token ghp_xxx

  # GitLab:
  python run_all_repos.py --platform gitlab --dry-run
  python run_all_repos.py --platform gitlab --token glpat-xxx --dry-run

  # Bitbucket:
  python run_all_repos.py --platform bitbucket --dry-run
  python run_all_repos.py --platform bitbucket --token <app-password> --dry-run

  # Or export env vars directly:
  export GITHUB_TOKEN=ghp_xxx
  python run_all_repos.py --dry-run

  export GITLAB_TOKEN=glpat-xxx
  python run_all_repos.py --platform gitlab --dry-run

  export BITBUCKET_TOKEN=<app-password>
  python run_all_repos.py --platform bitbucket --dry-run

Configuration priority (highest → lowest)
──────────────────────────────────────────
  1. CLI argument          --token ghp_xxx
  2. Environment variable  GITHUB_TOKEN=ghp_xxx  (or exported in shell)
  3. .env file             GITHUB_TOKEN=ghp_xxx  (loaded via python-dotenv)
  4. Built-in default

Supported environment variables
───────────────────────────────
  GITHUB_TOKEN          GitHub Personal Access Token  (or GH_TOKEN)
  GITLAB_TOKEN          GitLab Personal Access Token  (or GL_TOKEN)
  GITLAB_URL            GitLab instance URL            (default: https://gitlab.com)
  BITBUCKET_TOKEN       Bitbucket App Password / API token  (or BB_TOKEN)
  BITBUCKET_USERNAME    Bitbucket username for Basic auth   (or BITBUCKET_EMAIL)
  OPENAI_API_KEY        OpenAI key — passed through to repo_evaluator.py
  EVAL_PLATFORM         "github" | "gitlab" | "bitbucket"  (--platform)
  EVAL_ORGS             Comma-separated org/group list (--org)
  EVAL_EXCLUDE_ORGS     Comma-separated orgs to skip   (--exclude-org)
  EVAL_EXCLUDE_REPOS    Comma-separated repos to skip  (--exclude-repo)
  EVAL_INCLUDE_USER     "true" to include personal repos (--include-user-repos)
  EVAL_INCLUDE_ARCHIVED "true" to include archived repos (--include-archived)
  EVAL_INCLUDE_FORKS    "true" to include forked repos   (--include-forks)
  EVAL_VISIBILITY       "all" | "public" | "private"     (--visibility)
  EVAL_WORKERS          Number of parallel workers       (--workers)
  EVAL_OUTPUT_DIR       Output directory                 (--output-dir)
  EVAL_EVALUATOR_SCRIPT Path to repo_evaluator.py        (--evaluator-script)
  EVAL_EVALUATOR_ARGS   Extra flags for repo_evaluator   (--evaluator-args)

Dependencies
────────────
  pip install requests python-dotenv
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

# ── Load .env file (same pattern as the rest of the project) ─────────────────
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

# ──────────────────────────────────────────────────────────────────────────────
# Env helpers
# ──────────────────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    """Read an env var (already populated from .env by dotenv)."""
    return os.getenv(key, default)


# ──────────────────────────────────────────────────────────────────────────────
# GitHub API helpers
# ──────────────────────────────────────────────────────────────────────────────
GITHUB_API_BASE = "https://api.github.com"


class GitHubAPI:
    """Thin wrapper around the GitHub REST API with pagination & rate-limit handling."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "run-all-repos",
        })

    # ── low-level ────────────────────────────────────────────────────────────

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        resp = self.session.request(method, url, timeout=60, **kw)
        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset_at = resp.headers.get("X-RateLimit-Reset")
            if remaining == "0" and reset_at:
                wait = max(int(reset_at) - int(time.time()) + 2, 2)
                print(f"  ⏳ Rate-limited — sleeping {wait}s …", file=sys.stderr)
                time.sleep(wait)
                resp = self.session.request(method, url, timeout=60, **kw)
        return resp

    def _paginate(self, path: str, params: Optional[dict] = None) -> List[dict]:
        """Fetch all pages from a list endpoint."""
        results: List[dict] = []
        page = 1
        while True:
            merged = {"per_page": 100, "page": page}
            if params:
                merged.update(params)
            resp = self._request("GET", f"{GITHUB_API_BASE}{path}", params=merged)
            if resp.status_code >= 400:
                print(f"  ⚠ API error {resp.status_code} for {path}: "
                      f"{resp.text[:200]}", file=sys.stderr)
                break
            data = resp.json()
            if not isinstance(data, list):
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    # ── public helpers ───────────────────────────────────────────────────────

    def authenticated_user(self) -> dict:
        resp = self._request("GET", f"{GITHUB_API_BASE}/user")
        resp.raise_for_status()
        return resp.json()

    def list_orgs(self) -> List[dict]:
        """Return orgs the authenticated user belongs to."""
        return self._paginate("/user/orgs")

    def list_org_repos(self, org: str) -> List[dict]:
        """Return all repos for an org (all types the token can see)."""
        return self._paginate(f"/orgs/{org}/repos", params={"type": "all"})

    def list_user_repos(self) -> List[dict]:
        """Return repos the user owns or collaborates on."""
        return self._paginate("/user/repos",
                              params={"affiliation": "owner,collaborator"})


# ──────────────────────────────────────────────────────────────────────────────
# GitLab API helpers
# ──────────────────────────────────────────────────────────────────────────────


class GitLabAPI:
    """Thin wrapper around the GitLab REST API v4 with pagination.

    Supports all GitLab token types:
      • Personal Access Token (glpat-xxx)  → PRIVATE-TOKEN header
      • Group / Project Access Token       → PRIVATE-TOKEN header
      • OAuth2 token                       → Authorization: Bearer header
    """

    def __init__(self, token: str, base_url: str = "https://gitlab.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_base = f"{self.base_url}/api/v4"
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "run-all-repos",
        })
        # Will be set by authenticate()
        self._auth_resolved = False

    def _set_private_token(self) -> None:
        self.session.headers.pop("Authorization", None)
        self.session.headers["PRIVATE-TOKEN"] = self.token

    def _set_bearer(self) -> None:
        self.session.headers.pop("PRIVATE-TOKEN", None)
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    # ── low-level ────────────────────────────────────────────────────────────

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        resp = self.session.request(method, url, timeout=60, **kw)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            print(f"  ⏳ Rate-limited — sleeping {retry_after}s …", file=sys.stderr)
            time.sleep(retry_after)
            resp = self.session.request(method, url, timeout=60, **kw)
        return resp

    def _paginate(self, url: str, params: Optional[dict] = None) -> List[dict]:
        """Fetch all pages from a list endpoint using page pagination."""
        results: List[dict] = []
        page = 1
        while True:
            merged = {"per_page": 100, "page": page}
            if params:
                merged.update(params)
            resp = self._request("GET", url, params=merged)
            if resp.status_code >= 400:
                print(f"  ⚠ GitLab API error {resp.status_code} for {url}: "
                      f"{resp.text[:200]}", file=sys.stderr)
                break
            data = resp.json()
            if not isinstance(data, list):
                break
            results.extend(data)
            # Check X-Next-Page header (GitLab specific)
            next_page = resp.headers.get("X-Next-Page", "")
            if not next_page or len(data) < 100:
                break
            page = int(next_page)
        return results

    # ── public helpers ───────────────────────────────────────────────────────

    def _try_auth(self, label: str) -> Optional[dict]:
        """Try authenticating with current headers. Returns user dict or None."""
        # 1. Try /user (works for personal tokens + OAuth)
        resp = self._request("GET", f"{self.api_base}/user")
        if resp.status_code == 200:
            return resp.json()

        # Check for granular scope error — save for diagnostics
        self._last_error_body = ""
        if resp.status_code == 403:
            try:
                body = resp.json()
                self._last_error_body = body.get("error", "")
            except Exception:
                pass

        # 2. /user failed — try /groups (works for group/project tokens)
        resp2 = self._request("GET", f"{self.api_base}/groups",
                              params={"per_page": 1, "min_access_level": 10})
        if resp2.status_code == 200:
            data = resp2.json()
            if isinstance(data, list):
                print(f"  ℹ️  Token authenticated via /groups ({label}). "
                      f"/user not accessible — likely a Group/Project token.",
                      file=sys.stderr)
                return {"username": "(group/project token)", "name": "(group/project token)"}

        # 3. Try /projects as a last resort
        resp3 = self._request("GET", f"{self.api_base}/projects",
                              params={"per_page": 1, "membership": "true"})
        if resp3.status_code == 200:
            data = resp3.json()
            if isinstance(data, list):
                print(f"  ℹ️  Token authenticated via /projects ({label}). "
                      f"/user not accessible — likely a Group/Project token.",
                      file=sys.stderr)
                return {"username": "(project token)", "name": "(project token)"}

        return None

    def authenticate(self) -> dict:
        """Verify the token works and return user info.

        Tries PRIVATE-TOKEN header first, then falls back to Bearer auth.
        Validates via /user → /groups → /projects (each progressively
        more permissive for restricted token types).
        """
        # Attempt 1: PRIVATE-TOKEN header
        self._set_private_token()
        result = self._try_auth("PRIVATE-TOKEN")
        if result:
            self._auth_resolved = True
            return result

        # Attempt 2: Bearer header (OAuth2 tokens)
        print("  ℹ️  PRIVATE-TOKEN auth failed — trying Bearer …",
              file=sys.stderr)
        self._set_bearer()
        result = self._try_auth("Bearer")
        if result:
            self._auth_resolved = True
            return result

        # Nothing worked — build a helpful error message
        if self._last_error_body == "insufficient_granular_scope":
            raise RuntimeError(
                "GitLab token has insufficient granular scopes.\n"
                "\n"
                "   Your token is a fine-grained personal access token but is\n"
                "   missing the required permissions. Please recreate the token at:\n"
                "     → https://gitlab.com/-/user_settings/personal_access_tokens\n"
                "\n"
                "   Required scopes (select these when creating the token):\n"
                "     ✅ read_user            (or User: Read)\n"
                "     ✅ read_api             (or API: Read)\n"
                "     ✅ read_repository      (or Repository: Read)\n"
                "\n"
                "   Or use a classic token with 'read_api' scope instead."
            )
        else:
            raise RuntimeError(
                "Could not authenticate with GitLab.\n"
                "   Both PRIVATE-TOKEN and Bearer auth failed on /user, /groups, /projects.\n"
                "   Check that your token is valid and has at least 'read_api' scope.\n"
                "   Create a token at: https://gitlab.com/-/user_settings/personal_access_tokens"
            )

    def list_groups(self) -> List[dict]:
        """Return all groups the authenticated user/token has access to."""
        return self._paginate(
            f"{self.api_base}/groups",
            params={"min_access_level": 10}  # 10 = Guest (i.e. any membership)
        )

    def list_group_projects(self, group_id: int) -> List[dict]:
        """Return all projects under a group (including subgroups)."""
        return self._paginate(
            f"{self.api_base}/groups/{group_id}/projects",
            params={"include_subgroups": "true", "with_shared": "false"}
        )


    def list_owned_projects(self) -> List[dict]:
        """Return projects the user owns."""
        return self._paginate(
            f"{self.api_base}/projects",
            params={"owned": "true"}
        )


# ──────────────────────────────────────────────────────────────────────────────
# Bitbucket API helpers
# ──────────────────────────────────────────────────────────────────────────────
BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"


class BitbucketAPI:
    """Thin wrapper around the Bitbucket Cloud REST API v2 with pagination.

    Supports Bitbucket auth modes:
      • App password / API token  → HTTP Basic (username:token)
        - App password: use your Bitbucket username
        - API token: use your Atlassian account email
      • OAuth access token      → Authorization: Bearer
    """

    def __init__(self, token: str, username: Optional[str] = None) -> None:
        self.token = token
        self.username = username
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "run-all-repos",
        })
        self._auth_mode = ""

    def _apply_bearer(self) -> None:
        self.session.auth = None
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        self._auth_mode = "bearer"

    def _apply_basic(self, user: str) -> None:
        self.session.headers.pop("Authorization", None)
        self.session.auth = (user, self.token)
        self._auth_mode = f"basic:{user}"

    def authenticate(self) -> dict:
        """Verify credentials and return the /user payload."""
        attempts: List[tuple] = []
        if self.username:
            attempts.append(("basic", self.username))
        else:
            attempts.extend([
                ("bearer", None),
                ("basic", "x-bitbucket-api-token-auth"),
            ])

        last_error: Optional[Exception] = None
        for mode, user in attempts:
            if mode == "bearer":
                self._apply_bearer()
            else:
                self._apply_basic(user or "")
            try:
                resp = self._request("GET", f"{BITBUCKET_API_BASE}/user")
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_error = e

        raise RuntimeError(
            "Could not authenticate with Bitbucket.\n"
            "   Tried Bearer and Basic auth.\n"
            "   For app passwords, pass --bitbucket-username <bitbucket_username>.\n"
            "   For API tokens, pass --bitbucket-username <atlassian_email>.\n"
            f"   Last error: {last_error}"
        )

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        resp = self.session.request(method, url, timeout=60, **kw)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            print(f"  ⏳ Rate-limited — sleeping {retry_after}s …", file=sys.stderr)
            time.sleep(retry_after)
            resp = self.session.request(method, url, timeout=60, **kw)
        return resp

    def _paginate(self, url: str, params: Optional[dict] = None) -> List[dict]:
        """Fetch all pages from a list endpoint using Bitbucket's next-link pagination."""
        results: List[dict] = []
        while url:
            resp = self._request("GET", url, params=params)
            if resp.status_code >= 400:
                print(f"  ⚠ Bitbucket API error {resp.status_code} for {url}: "
                      f"{resp.text[:200]}", file=sys.stderr)
                break
            data = resp.json()
            results.extend(data.get("values", []))
            url = data.get("next")
            params = None  # next URL already includes query params
        return results

    def authenticated_user(self) -> dict:
        return self.authenticate()

    def list_workspaces(self) -> List[dict]:
        """Return workspaces the authenticated user is a member of."""
        return self._paginate(
            f"{BITBUCKET_API_BASE}/workspaces",
            params={"role": "member"},
        )

    def list_workspace_repos(self, workspace: str) -> List[dict]:
        """Return all repos in a workspace that the token can see."""
        return self._paginate(
            f"{BITBUCKET_API_BASE}/repositories/{workspace}",
            params={"role": "member"},
        )

    def list_user_repos(self) -> List[dict]:
        """Return repos the user owns or is a member of."""
        return self._paginate(
            f"{BITBUCKET_API_BASE}/repositories",
            params={"role": "member"},
        )


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class RepoInfo:
    full_name: str          # e.g. "my-org/backend-api"
    owner: str
    name: str
    private: bool
    archived: bool
    fork: bool
    default_branch: str
    language: Optional[str]
    org: str                # org/group name (or "user" for personal repos)
    platform: str = "github"  # "github", "gitlab", or "bitbucket"


@dataclass
class RunResult:
    repo: str
    exit_code: int
    duration_seconds: float
    error: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# Discovery — GitHub
# ──────────────────────────────────────────────────────────────────────────────

def discover_github_repos(
    api: GitHubAPI,
    *,
    only_orgs: Optional[List[str]] = None,
    exclude_orgs: Optional[List[str]] = None,
    exclude_repos: Optional[List[str]] = None,
    include_user_repos: bool = False,
    include_archived: bool = False,
    include_forks: bool = False,
    visibility: str = "all",
) -> Dict[str, List[RepoInfo]]:
    """
    Returns { org_name: [RepoInfo, …] } for every accessible GitHub org
    (+ "user" for personal/collaborator repos).

    Personal repos are included when:
      • --include-user-repos is set, OR
      • no organizations are found (auto-fallback).
    """
    exclude_orgs_set = set(exclude_orgs or [])
    exclude_repos_set = set(exclude_repos or [])

    # 1. Discover orgs
    orgs_raw = api.list_orgs()
    org_logins = sorted({o["login"] for o in orgs_raw})

    if only_orgs:
        wanted = set(only_orgs)
        org_logins = [o for o in org_logins if o in wanted]

    org_logins = [o for o in org_logins if o not in exclude_orgs_set]

    # 2. Fetch repos per org
    result: Dict[str, List[RepoInfo]] = {}

    for org in org_logins:
        raw_repos = api.list_org_repos(org)
        repos: List[RepoInfo] = []
        for r in raw_repos:
            ri = _github_to_repo_info(r, org)
            if _should_include(ri, visibility, include_archived, include_forks,
                               exclude_repos_set):
                repos.append(ri)
        repos.sort(key=lambda x: x.full_name.lower())
        result[org] = repos

    # 3. Personal / collaborator repos
    should_fetch_user = include_user_repos
    auto_fallback = False
    if not org_logins and not include_user_repos:
        should_fetch_user = True
        auto_fallback = True

    if should_fetch_user:
        if auto_fallback:
            print("  ℹ️  No GitHub organizations found — automatically including "
                  "personal/collaborator repos.")
        raw_user = api.list_user_repos()
        user_repos: List[RepoInfo] = []
        for r in raw_user:
            ri = _github_to_repo_info(r, "user")
            if _should_include(ri, visibility, include_archived, include_forks,
                               exclude_repos_set):
                user_repos.append(ri)
        user_repos.sort(key=lambda x: x.full_name.lower())
        if user_repos:
            result["user"] = user_repos

    return result


def _github_to_repo_info(r: dict, org: str) -> RepoInfo:
    return RepoInfo(
        full_name=r["full_name"],
        owner=r["owner"]["login"],
        name=r["name"],
        private=bool(r.get("private")),
        archived=bool(r.get("archived")),
        fork=bool(r.get("fork")),
        default_branch=r.get("default_branch") or "main",
        language=r.get("language"),
        org=org,
        platform="github",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Discovery — GitLab
# ──────────────────────────────────────────────────────────────────────────────

def discover_gitlab_repos(
    api: GitLabAPI,
    *,
    only_groups: Optional[List[str]] = None,
    exclude_groups: Optional[List[str]] = None,
    exclude_repos: Optional[List[str]] = None,
    include_user_repos: bool = False,
    include_archived: bool = False,
    include_forks: bool = False,
    visibility: str = "all",
) -> Dict[str, List[RepoInfo]]:
    """
    Returns { group_name: [RepoInfo, …] } for every accessible GitLab group
    (+ "user" for personal projects).

    Personal projects are included when:
      • --include-user-repos is set, OR
      • no groups are found (auto-fallback).
    """
    exclude_groups_set = set(exclude_groups or [])
    exclude_repos_set = set(exclude_repos or [])

    # 1. Discover groups
    groups_raw = api.list_groups()
    # Build a list of (group_full_path, group_id) — full_path is like "my-org" or "my-org/sub-group"
    group_info = sorted(
        [(g["full_path"], g["id"]) for g in groups_raw],
        key=lambda x: x[0].lower()
    )

    if only_groups:
        wanted = set(only_groups)
        group_info = [(p, gid) for p, gid in group_info if p in wanted]

    group_info = [(p, gid) for p, gid in group_info if p not in exclude_groups_set]

    # 2. Fetch projects per group
    result: Dict[str, List[RepoInfo]] = {}
    seen_project_ids: set = set()  # Avoid duplicates from nested subgroups

    for group_path, group_id in group_info:
        raw_projects = api.list_group_projects(group_id)
        repos: List[RepoInfo] = []
        for p in raw_projects:
            if p["id"] in seen_project_ids:
                continue
            seen_project_ids.add(p["id"])
            ri = _gitlab_to_repo_info(p, group_path)
            if _should_include(ri, visibility, include_archived, include_forks,
                               exclude_repos_set):
                repos.append(ri)
        repos.sort(key=lambda x: x.full_name.lower())
        if repos:
            result[group_path] = repos

    # 3. Personal / owned projects
    should_fetch_user = include_user_repos
    auto_fallback = False
    if not group_info and not include_user_repos:
        should_fetch_user = True
        auto_fallback = True

    if should_fetch_user:
        if auto_fallback:
            print("  ℹ️  No GitLab groups found — automatically including "
                  "personal/owned projects.")
        raw_owned = api.list_owned_projects()
        user_repos: List[RepoInfo] = []
        for p in raw_owned:
            if p["id"] in seen_project_ids:
                continue
            seen_project_ids.add(p["id"])
            ri = _gitlab_to_repo_info(p, "user")
            if _should_include(ri, visibility, include_archived, include_forks,
                               exclude_repos_set):
                user_repos.append(ri)
        user_repos.sort(key=lambda x: x.full_name.lower())
        if user_repos:
            result["user"] = user_repos

    return result


def _gitlab_to_repo_info(p: dict, group: str) -> RepoInfo:
    """Convert a GitLab project dict to a RepoInfo."""
    # path_with_namespace = "group/subgroup/project-name"
    full_path = p.get("path_with_namespace", "")
    namespace = p.get("namespace", {})
    owner = namespace.get("full_path", "") or namespace.get("path", "")
    visibility = p.get("visibility", "private")  # "public", "internal", "private"

    return RepoInfo(
        full_name=full_path,
        owner=owner,
        name=p.get("path", "") or p.get("name", ""),
        private=(visibility != "public"),
        archived=bool(p.get("archived")),
        fork=bool(p.get("forked_from_project")),
        default_branch=p.get("default_branch") or "main",
        language=None,  # GitLab doesn't return primary language in list endpoints
        org=group,
        platform="gitlab",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Discovery — Bitbucket
# ──────────────────────────────────────────────────────────────────────────────

def discover_bitbucket_repos(
    api: BitbucketAPI,
    *,
    only_workspaces: Optional[List[str]] = None,
    exclude_workspaces: Optional[List[str]] = None,
    exclude_repos: Optional[List[str]] = None,
    include_user_repos: bool = False,
    include_archived: bool = False,
    include_forks: bool = False,
    visibility: str = "all",
) -> Dict[str, List[RepoInfo]]:
    """
    Returns { workspace_name: [RepoInfo, …] } for every accessible Bitbucket
    workspace (+ "user" for personal repos).

    Personal repos are included when:
      • --include-user-repos is set, OR
      • no workspaces are found (auto-fallback).
    """
    exclude_workspaces_set = set(exclude_workspaces or [])
    exclude_repos_set = set(exclude_repos or [])

    # 1. Discover workspaces
    workspaces_raw = api.list_workspaces()
    workspace_slugs = sorted({w["slug"] for w in workspaces_raw})

    if only_workspaces:
        wanted = set(only_workspaces)
        workspace_slugs = [w for w in workspace_slugs if w in wanted]

    workspace_slugs = [w for w in workspace_slugs if w not in exclude_workspaces_set]

    # 2. Fetch repos per workspace
    result: Dict[str, List[RepoInfo]] = {}
    seen_repo_uuids: set = set()

    for workspace in workspace_slugs:
        raw_repos = api.list_workspace_repos(workspace)
        repos: List[RepoInfo] = []
        for r in raw_repos:
            repo_uuid = r.get("uuid", r.get("full_name", ""))
            if repo_uuid in seen_repo_uuids:
                continue
            seen_repo_uuids.add(repo_uuid)
            ri = _bitbucket_to_repo_info(r, workspace)
            if _should_include(ri, visibility, include_archived, include_forks,
                               exclude_repos_set):
                repos.append(ri)
        repos.sort(key=lambda x: x.full_name.lower())
        if repos:
            result[workspace] = repos

    # 3. Personal repos
    should_fetch_user = include_user_repos
    auto_fallback = False
    if not workspace_slugs and not include_user_repos:
        should_fetch_user = True
        auto_fallback = True

    if should_fetch_user:
        if auto_fallback:
            print("  ℹ️  No Bitbucket workspaces found — automatically including "
                  "personal/member repos.")
        raw_user = api.list_user_repos()
        user_repos: List[RepoInfo] = []
        for r in raw_user:
            repo_uuid = r.get("uuid", r.get("full_name", ""))
            if repo_uuid in seen_repo_uuids:
                continue
            seen_repo_uuids.add(repo_uuid)
            ri = _bitbucket_to_repo_info(r, "user")
            if _should_include(ri, visibility, include_archived, include_forks,
                               exclude_repos_set):
                user_repos.append(ri)
        user_repos.sort(key=lambda x: x.full_name.lower())
        if user_repos:
            result["user"] = user_repos

    return result


def _bitbucket_to_repo_info(r: dict, workspace: str) -> RepoInfo:
    """Convert a Bitbucket repository dict to a RepoInfo."""
    full_name = r.get("full_name", "")
    ws_slug = (r.get("workspace") or {}).get("slug", workspace)
    return RepoInfo(
        full_name=full_name,
        owner=ws_slug,
        name=r.get("slug", "") or r.get("name", ""),
        private=bool(r.get("is_private")),
        archived=bool(r.get("is_archived", False)),
        fork=bool(r.get("parent")),
        default_branch=(r.get("mainbranch") or {}).get("name", "main"),
        language=r.get("language"),
        org=workspace,
        platform="bitbucket",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Shared filter
# ──────────────────────────────────────────────────────────────────────────────

def _should_include(ri: RepoInfo, visibility: str, include_archived: bool,
                    include_forks: bool, exclude: set) -> bool:
    if ri.full_name in exclude:
        return False
    if not include_archived and ri.archived:
        return False
    if not include_forks and ri.fork:
        return False
    if visibility == "public" and ri.private:
        return False
    if visibility == "private" and not ri.private:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Pretty printing (dry-run)
# ──────────────────────────────────────────────────────────────────────────────

def print_inventory(org_repos: Dict[str, List[RepoInfo]], platform: str) -> None:
    total = sum(len(repos) for repos in org_repos.values())
    if platform == "gitlab":
        scope_label = "group(s)"
    elif platform == "bitbucket":
        scope_label = "workspace(s)"
    else:
        scope_label = "org(s)/scope(s)"
    print(f"\n{'=' * 70}")
    print(f"  📋  REPOSITORY INVENTORY [{platform.upper()}] — {total} repo(s) across "
          f"{len(org_repos)} {scope_label}")
    print(f"{'=' * 70}\n")

    for org, repos in sorted(org_repos.items()):
        if org == "user":
            label = "👤 Personal / Owned"
        elif platform == "gitlab":
            label = f"🦊 {org}"
        elif platform == "bitbucket":
            label = f"🪣 {org}"
        else:
            label = f"🏢 {org}"
        print(f"  {label}  ({len(repos)} repos)")
        print(f"  {'─' * 60}")
        for r in repos:
            flags = []
            if r.private:
                flags.append("🔒 private")
            if r.archived:
                flags.append("📦 archived")
            if r.fork:
                flags.append("🍴 fork")
            if r.language:
                flags.append(r.language)
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(f"    • {r.full_name}{flag_str}")
        print()

    print(f"{'=' * 70}")
    print(f"  Total: {total} repositories")
    print(f"{'=' * 70}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Execution
# ──────────────────────────────────────────────────────────────────────────────

def run_evaluator(
    repo_info: RepoInfo,
    token: str,
    evaluator_script: str,
    extra_args: List[str],
    output_dir: str,
    timeout_minutes: int = 120,
) -> RunResult:
    """Run repo_evaluator.py for a single repo.

    Output structure:
        output_dir/
          <org>/
            <repo>/
              <repo>.json
              <repo>.csv    (created by repo_evaluator.py)
    """
    repo_full_name = repo_info.full_name
    platform = repo_info.platform

    # Build org/repo folder structure
    org_folder = repo_info.org if repo_info.org else "unknown"
    repo_folder = repo_info.name
    repo_output_dir = os.path.join(output_dir, org_folder, repo_folder)
    os.makedirs(repo_output_dir, exist_ok=True)

    output_file = os.path.join(repo_output_dir, f"{repo_folder}.json")

    # Prefix so repo_evaluator.py detects the platform
    if platform == "gitlab":
        repo_arg = f"gitlab:{repo_full_name}"
    elif platform == "bitbucket":
        repo_arg = f"bitbucket:{repo_full_name}"
    else:
        repo_arg = repo_full_name

    cmd = [
        sys.executable, evaluator_script,
        repo_arg,
        "--token", token,
        "--platform", platform,
        "--json",
        "--output", output_file,
    ] + extra_args

    print(f"  ▶ Running: {repo_full_name} …")
    started = time.time()
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_minutes * 60)
        duration = round(time.time() - started, 1)
        if completed.returncode == 0:
            print(f"  ✅ {repo_full_name}  ({duration}s)")
        else:
            stderr_snippet = (completed.stderr or "")[:300]
            print(f"  ❌ {repo_full_name}  exit={completed.returncode}  ({duration}s)")
            if stderr_snippet:
                print(f"     {stderr_snippet}")
        return RunResult(
            repo=repo_full_name,
            exit_code=completed.returncode,
            duration_seconds=duration,
            error=completed.stderr[:500] if completed.returncode != 0 else None,
        )
    except subprocess.TimeoutExpired:
        duration = round(time.time() - started, 1)
        print(f"  ⏰ {repo_full_name}  TIMEOUT after {duration}s")
        return RunResult(repo=repo_full_name, exit_code=-1,
                         duration_seconds=duration, error=f"timeout ({timeout_minutes}min)")
    except Exception as e:
        duration = round(time.time() - started, 1)
        print(f"  💥 {repo_full_name}  ERROR: {e}")
        return RunResult(repo_full_name, exit_code=-1,
                         duration_seconds=duration, error=str(e))


def run_all(
    org_repos: Dict[str, List[RepoInfo]],
    token: str,
    evaluator_script: str,
    extra_args: List[str],
    output_dir: str,
    workers: int = 4,
    fail_fast: bool = False,
    timeout_minutes: int = 60,
) -> List[RunResult]:
    """Execute repo_evaluator.py on every discovered repo."""
    os.makedirs(output_dir, exist_ok=True)

    # Flatten
    all_repos = [r for repos in org_repos.values() for r in repos]
    total = len(all_repos)
    print(f"\n🚀 Starting evaluation of {total} repos with {workers} worker(s)")
    print(f"   Output directory: {output_dir}")
    print(f"   Timeout per repo: {timeout_minutes} min\n")

    results: List[RunResult] = []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_map = {
            pool.submit(
                run_evaluator, r, token, evaluator_script, extra_args,
                output_dir, timeout_minutes
            ): r
            for r in all_repos
        }

        for future in as_completed(future_map):
            repo_info = future_map[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append(RunResult(
                    repo=repo_info.full_name, exit_code=-1,
                    duration_seconds=0, error=str(e),
                ))

            done = len(results)
            failed = sum(1 for r in results if r.exit_code != 0)
            print(f"   [{done}/{total}]  failed so far: {failed}")

            if fail_fast and failed > 0:
                print("   ⛔ --fail-fast: stopping early")
                break

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Summary report
# ──────────────────────────────────────────────────────────────────────────────

def print_summary(results: List[RunResult], output_dir: str) -> None:
    succeeded = [r for r in results if r.exit_code == 0]
    failed = [r for r in results if r.exit_code != 0]
    total_time = sum(r.duration_seconds for r in results)

    print(f"\n{'=' * 70}")
    print(f"  📊  RUN SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total repos:   {len(results)}")
    print(f"  ✅ Succeeded:  {len(succeeded)}")
    print(f"  ❌ Failed:     {len(failed)}")
    print(f"  ⏱  Total time: {total_time:.0f}s "
          f"({total_time / 60:.1f} min)")
    print(f"  📁 Output dir: {output_dir}")

    if failed:
        print(f"\n  Failed repos:")
        for r in failed:
            err_short = (r.error or "unknown")[:120]
            print(f"    • {r.repo}  (exit {r.exit_code}) — {err_short}")

    print(f"{'=' * 70}\n")

    # Write a summary JSON
    summary_path = os.path.join(output_dir, "_summary.json")
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "total_seconds": round(total_time, 1),
        "results": [
            {
                "repo": r.repo,
                "exit_code": r.exit_code,
                "duration_seconds": r.duration_seconds,
                "error": r.error,
            }
            for r in results
        ],
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  Summary written to {summary_path}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI  — every option has a matching env var fallback
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Discover all org/group repos and run repo_evaluator.py on each one.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration priority (highest wins):
  1. CLI argument          --token ghp_xxx
  2. Environment variable  GITHUB_TOKEN=ghp_xxx
  3. .env file             GITHUB_TOKEN=ghp_xxx
  4. Built-in default

Supported env vars (all optional — CLI args override):
  GITHUB_TOKEN / GH_TOKEN       GitHub Personal Access Token
  GITLAB_TOKEN / GL_TOKEN       GitLab Personal Access Token
  GITLAB_URL                    GitLab instance URL (default: https://gitlab.com)
  BITBUCKET_TOKEN / BB_TOKEN        Bitbucket App Password / API token
  BITBUCKET_USERNAME / BITBUCKET_EMAIL  Username or email for Basic auth
  OPENAI_API_KEY                     Passed through to repo_evaluator.py
  EVAL_PLATFORM                  github / gitlab / bitbucket  → --platform
  EVAL_ORGS                  Comma-separated org/group names → --org
  EVAL_EXCLUDE_ORGS          Comma-separated names           → --exclude-org
  EVAL_EXCLUDE_REPOS         Comma-separated owner/repo      → --exclude-repo
  EVAL_INCLUDE_USER          true / false                    → --include-user-repos
  EVAL_INCLUDE_ARCHIVED      true / false                    → --include-archived
  EVAL_INCLUDE_FORKS         true / false                    → --include-forks
  EVAL_VISIBILITY            all / public / private          → --visibility
  EVAL_WORKERS               integer                        → --workers
  EVAL_OUTPUT_DIR            path                            → --output-dir
  EVAL_EVALUATOR_SCRIPT      path                            → --evaluator-script
  EVAL_EVALUATOR_ARGS        string                          → --evaluator-args

Examples — GitHub:
  python run_all_repos.py --dry-run
  python run_all_repos.py --dry-run --token ghp_xxx --org my-org
  python run_all_repos.py --run --org acme-corp --workers 8

Examples — GitLab:
  python run_all_repos.py --platform gitlab --dry-run
  python run_all_repos.py --platform gitlab --token glpat-xxx --dry-run
  python run_all_repos.py --platform gitlab --run --org my-group

How to get a GitLab token:
  1. Go to https://gitlab.com/-/user_settings/personal_access_tokens
     (or your self-hosted GitLab → Settings → Access Tokens)
  2. Create a token with scopes: read_api, read_repository
  3. Set GITLAB_TOKEN=glpat-xxx in .env or pass --token glpat-xxx

Examples — Bitbucket:
  python run_all_repos.py --platform bitbucket --dry-run
  python run_all_repos.py --platform bitbucket --token <app-password> --dry-run
  python run_all_repos.py --platform bitbucket --run --org my-workspace

How to get a Bitbucket token:
  1. Go to https://bitbucket.org/account/settings/app-passwords/
  2. Create an App Password with scopes: Account (Read), Repositories (Read),
     Workspace membership (Read)
  3. Set BITBUCKET_TOKEN=<app-password> in .env or pass --token <app-password>
  4. For app passwords / API tokens, also set BITBUCKET_USERNAME or pass
     --bitbucket-username (Bitbucket username for app passwords, Atlassian
     email for API tokens)
        """,
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Only list orgs/groups & repos — do not run the evaluator")
    mode.add_argument("--run", action="store_true",
                      help="Run repo_evaluator.py on every discovered repo")

    # ── Platform ─────────────────────────────────────────────────────────────
    p.add_argument(
        "--platform",
        choices=["github", "gitlab", "bitbucket"], default=None,
        help="Platform to discover repos from "
             "(env: EVAL_PLATFORM, default: github)",
    )

    # ── Token ────────────────────────────────────────────────────────────────
    p.add_argument(
        "--token",
        default=None,
        help="Personal Access Token for the chosen platform "
             "(env: GITHUB_TOKEN / GH_TOKEN for GitHub, "
             "GITLAB_TOKEN / GL_TOKEN for GitLab, "
             "BITBUCKET_TOKEN / BB_TOKEN for Bitbucket)",
    )

    # ── GitLab-specific ──────────────────────────────────────────────────────
    p.add_argument(
        "--gitlab-url", default=None,
        help="GitLab instance URL for self-hosted "
             "(env: GITLAB_URL, default: https://gitlab.com)",
    )

    # ── Bitbucket-specific ───────────────────────────────────────────────────
    p.add_argument(
        "--bitbucket-username", default=None,
        help="Bitbucket username (app password) or Atlassian email (API token) "
             "(env: BITBUCKET_USERNAME / BITBUCKET_EMAIL)",
    )

    # ── Filtering ────────────────────────────────────────────────────────────
    p.add_argument(
        "--org", action="append", default=None,
        help="Only include these org(s)/group(s) — repeatable "
             "(env: EVAL_ORGS=org1,org2)",
    )
    p.add_argument(
        "--exclude-org", action="append", default=None,
        help="Exclude these org(s)/group(s) — repeatable "
             "(env: EVAL_EXCLUDE_ORGS=org1,org2)",
    )
    p.add_argument(
        "--exclude-repo", action="append", default=None,
        help="Exclude repos by full name (owner/repo) — repeatable "
             "(env: EVAL_EXCLUDE_REPOS=owner/repo1,owner/repo2)",
    )
    p.add_argument(
        "--include-user-repos", action="store_true", default=None,
        help="Also include personal/owned repos "
             "(env: EVAL_INCLUDE_USER=true)",
    )
    p.add_argument(
        "--include-archived", action="store_true", default=None,
        help="Include archived repos "
             "(env: EVAL_INCLUDE_ARCHIVED=true)",
    )
    p.add_argument(
        "--include-forks", action="store_true", default=None,
        help="Include forked repos "
             "(env: EVAL_INCLUDE_FORKS=true)",
    )
    p.add_argument(
        "--visibility", choices=["all", "public", "private"], default=None,
        help="Filter by visibility "
             "(env: EVAL_VISIBILITY=all|public|private, default: all)",
    )

    # ── Execution ────────────────────────────────────────────────────────────
    p.add_argument(
        "--workers", type=int, default=None,
        help="Parallel workers "
             "(env: EVAL_WORKERS, default: 4)",
    )
    p.add_argument(
        "--fail-fast", action="store_true",
        help="Stop on first failure",
    )
    p.add_argument(
        "--timeout", type=int, default=None,
        help="Timeout in minutes per repo evaluation "
             "(env: EVAL_TIMEOUT, default: 60)",
    )
    p.add_argument(
        "--output-dir", default=None,
        help="Directory for per-repo JSON output "
             "(env: EVAL_OUTPUT_DIR, default: eval_results)",
    )
    p.add_argument(
        "--evaluator-script", default=None,
        help="Path to repo_evaluator.py "
             "(env: EVAL_EVALUATOR_SCRIPT, default: repo_evaluator.py)",
    )
    p.add_argument(
        "--evaluator-args", default=None,
        help="Extra flags to pass to repo_evaluator.py "
             '(env: EVAL_EVALUATOR_ARGS, e.g. "--skip-f2p --skip-quality-checks")',
    )

    # ── Output ───────────────────────────────────────────────────────────────
    p.add_argument(
        "--save-inventory", default=None,
        help="Save the repo inventory to a JSON file",
    )

    return p.parse_args()


# ──────────────────────────────────────────────────────────────────────────────
# Resolve config: CLI arg  →  env var  →  built-in default
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ResolvedConfig:
    """Final configuration after merging CLI args + env vars + defaults."""
    platform: str
    token: str
    gitlab_url: str
    bitbucket_username: str
    orgs: Optional[List[str]]
    exclude_orgs: List[str]
    exclude_repos: List[str]
    include_user_repos: bool
    include_archived: bool
    include_forks: bool
    visibility: str
    workers: int
    timeout: int
    fail_fast: bool
    output_dir: str
    evaluator_script: str
    evaluator_args: str
    save_inventory: Optional[str]
    dry_run: bool
    run: bool
    # Track where each value came from for the config summary
    sources: Dict[str, str]


def _resolve(cli_val, env_key: str, default, *, is_bool=False, is_int=False,
             is_list=False, env_keys=None):
    """
    Return (resolved_value, source_label).
    source_label is one of: "cli", "env:<KEY>", "default".
    """
    # 1. CLI
    if cli_val is not None:
        # argparse sets store_true to True (not None) only when flag is given
        if is_bool and cli_val is True:
            return True, "cli"
        elif not is_bool:
            return cli_val, "cli"

    # 2. Env var(s)
    keys = env_keys or ([env_key] if env_key else [])
    for key in keys:
        raw = os.getenv(key, "")
        if raw.strip():
            if is_bool:
                return raw.strip().lower() in ("true", "1", "yes"), f"env:{key}"
            elif is_int:
                try:
                    return int(raw), f"env:{key}"
                except ValueError:
                    pass
            elif is_list:
                items = [x.strip() for x in raw.split(",") if x.strip()]
                return items, f"env:{key}"
            else:
                return raw.strip(), f"env:{key}"

    # 3. Default
    return default, "default"


def resolve_config(args: argparse.Namespace) -> ResolvedConfig:
    """Merge CLI args, env vars, and defaults into a single config object."""
    sources: Dict[str, str] = {}

    platform, sources["platform"] = _resolve(
        args.platform, "EVAL_PLATFORM", "github")

    # Token: pick env var keys based on platform
    if platform == "gitlab":
        token_env_keys = ["GITLAB_TOKEN", "GL_TOKEN"]
    elif platform == "bitbucket":
        token_env_keys = ["BITBUCKET_TOKEN", "BB_TOKEN"]
    else:
        token_env_keys = ["GITHUB_TOKEN", "GH_TOKEN"]

    token, sources["token"] = _resolve(
        args.token, "", "", env_keys=token_env_keys)

    gitlab_url, sources["gitlab_url"] = _resolve(
        args.gitlab_url, "GITLAB_URL", "https://gitlab.com")

    bitbucket_username, sources["bitbucket_username"] = _resolve(
        args.bitbucket_username, "", "", env_keys=["BITBUCKET_USERNAME", "BITBUCKET_EMAIL"])

    orgs, sources["orgs"] = _resolve(
        args.org, "EVAL_ORGS", None, is_list=True)

    exclude_orgs, sources["exclude_orgs"] = _resolve(
        args.exclude_org, "EVAL_EXCLUDE_ORGS", [], is_list=True)

    exclude_repos, sources["exclude_repos"] = _resolve(
        args.exclude_repo, "EVAL_EXCLUDE_REPOS", [], is_list=True)

    include_user, sources["include_user_repos"] = _resolve(
        args.include_user_repos, "EVAL_INCLUDE_USER", False, is_bool=True)

    include_archived, sources["include_archived"] = _resolve(
        args.include_archived, "EVAL_INCLUDE_ARCHIVED", False, is_bool=True)

    include_forks, sources["include_forks"] = _resolve(
        args.include_forks, "EVAL_INCLUDE_FORKS", False, is_bool=True)

    visibility, sources["visibility"] = _resolve(
        args.visibility, "EVAL_VISIBILITY", "all")

    workers, sources["workers"] = _resolve(
        args.workers, "EVAL_WORKERS", 4, is_int=True)

    timeout, sources["timeout"] = _resolve(
        args.timeout, "EVAL_TIMEOUT", 60, is_int=True)

    output_dir, sources["output_dir"] = _resolve(
        args.output_dir, "EVAL_OUTPUT_DIR", "eval_results")

    evaluator_script, sources["evaluator_script"] = _resolve(
        args.evaluator_script, "EVAL_EVALUATOR_SCRIPT", "repo_evaluator.py")

    evaluator_args, sources["evaluator_args"] = _resolve(
        args.evaluator_args, "EVAL_EVALUATOR_ARGS", "")

    return ResolvedConfig(
        platform=platform,
        token=token,
        gitlab_url=gitlab_url,
        bitbucket_username=bitbucket_username,
        orgs=orgs if orgs else None,
        exclude_orgs=exclude_orgs or [],
        exclude_repos=exclude_repos or [],
        include_user_repos=include_user,
        include_archived=include_archived,
        include_forks=include_forks,
        visibility=visibility,
        workers=workers,
        timeout=timeout,
        fail_fast=args.fail_fast,
        output_dir=output_dir,
        evaluator_script=evaluator_script,
        evaluator_args=evaluator_args,
        save_inventory=args.save_inventory,
        dry_run=args.dry_run,
        run=args.run,
        sources=sources,
    )


def print_config(cfg: ResolvedConfig) -> None:
    """Show resolved configuration and where each value came from."""
    print(f"\n{'─' * 70}")
    print(f"  ⚙  RESOLVED CONFIGURATION")
    print(f"{'─' * 70}")

    def _mask_token(val: str) -> str:
        if not val or len(val) < 8:
            return val or "(not set)"
        return val[:4] + "…" + val[-4:]

    rows = [
        ("Platform", cfg.platform, cfg.sources.get("platform", "")),
        ("Token", _mask_token(cfg.token), cfg.sources.get("token", "")),
    ]

    if cfg.platform == "gitlab":
        rows.append(("GitLab URL", cfg.gitlab_url, cfg.sources.get("gitlab_url", "")))

    if cfg.platform == "bitbucket":
        rows.append((
            "Bitbucket username/email",
            cfg.bitbucket_username or "(auto-detect)",
            cfg.sources.get("bitbucket_username", ""),
        ))

    if cfg.platform == "gitlab":
        org_label = "Groups filter"
    elif cfg.platform == "bitbucket":
        org_label = "Workspaces filter"
    else:
        org_label = "Orgs filter"
    rows += [
        (org_label, ", ".join(cfg.orgs) if cfg.orgs else "(all)", cfg.sources.get("orgs", "")),
        ("Exclude orgs/groups", ", ".join(cfg.exclude_orgs) if cfg.exclude_orgs else "(none)", cfg.sources.get("exclude_orgs", "")),
        ("Exclude repos", ", ".join(cfg.exclude_repos) if cfg.exclude_repos else "(none)", cfg.sources.get("exclude_repos", "")),
        ("Include user repos", str(cfg.include_user_repos), cfg.sources.get("include_user_repos", "")),
        ("Include archived", str(cfg.include_archived), cfg.sources.get("include_archived", "")),
        ("Include forks", str(cfg.include_forks), cfg.sources.get("include_forks", "")),
        ("Visibility", cfg.visibility, cfg.sources.get("visibility", "")),
        ("Workers", str(cfg.workers), cfg.sources.get("workers", "")),
        ("Timeout per repo", f"{cfg.timeout} min", cfg.sources.get("timeout", "")),
        ("Output dir", cfg.output_dir, cfg.sources.get("output_dir", "")),
        ("Evaluator script", cfg.evaluator_script, cfg.sources.get("evaluator_script", "")),
        ("Evaluator args", cfg.evaluator_args or "(none)", cfg.sources.get("evaluator_args", "")),
        ("OpenAI API key", _mask_token(_env("OPENAI_API_KEY")), "env" if _env("OPENAI_API_KEY") else "not set"),
    ]

    for label, value, source in rows:
        src_tag = f"  ← {source}" if source else ""
        print(f"  {label:.<30s} {value}{src_tag}")

    print(f"{'─' * 70}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    cfg = resolve_config(args)

    # ── Validate token ───────────────────────────────────────────────────────
    if not cfg.token:
        if cfg.platform == "gitlab":
            print(
                "❌ ERROR: No GitLab token provided.\n"
                "   Set it via any of these (highest priority first):\n"
                "     1. CLI arg:    --token glpat-xxx\n"
                "     2. Env var:    export GITLAB_TOKEN=glpat-xxx\n"
                "     3. .env file:  GITLAB_TOKEN=glpat-xxx\n"
                "\n"
                "   How to create a GitLab token:\n"
                "     → https://gitlab.com/-/user_settings/personal_access_tokens\n"
                "     → Required scopes: read_api, read_repository\n",
                file=sys.stderr,
            )
        elif cfg.platform == "bitbucket":
            print(
                "❌ ERROR: No Bitbucket token provided.\n"
                "   Set it via any of these (highest priority first):\n"
                "     1. CLI arg:    --token <app-password>\n"
                "     2. Env var:    export BITBUCKET_TOKEN=<app-password>\n"
                "     3. .env file:  BITBUCKET_TOKEN=<app-password>\n"
                "\n"
                "   How to create a Bitbucket App Password:\n"
                "     → https://bitbucket.org/account/settings/app-passwords/\n"
                "     → Required scopes: Account (Read), Repositories (Read),\n"
                "       Workspace membership (Read)\n",
                file=sys.stderr,
            )
        else:
            print(
                "❌ ERROR: No GitHub token provided.\n"
                "   Set it via any of these (highest priority first):\n"
                "     1. CLI arg:    --token ghp_xxx\n"
                "     2. Env var:    export GITHUB_TOKEN=ghp_xxx\n"
                "     3. .env file:  GITHUB_TOKEN=ghp_xxx\n",
                file=sys.stderr,
            )
        return 1

    # ── Show resolved config ─────────────────────────────────────────────────
    print_config(cfg)

    # ── Platform-specific flow ───────────────────────────────────────────────
    if cfg.platform == "gitlab":
        return _run_gitlab(cfg)
    elif cfg.platform == "bitbucket":
        return _run_bitbucket(cfg)
    else:
        return _run_github(cfg)


def _run_github(cfg: ResolvedConfig) -> int:
    """GitHub discovery + evaluation flow."""
    api = GitHubAPI(cfg.token)
    try:
        user = api.authenticated_user()
    except Exception as e:
        print(f"❌ ERROR: Failed to authenticate with GitHub — {e}", file=sys.stderr)
        return 1

    print(f"🔑 Authenticated as: {user['login']}  (GitHub)")

    print("🔍 Discovering organizations and repositories …\n")
    org_repos = discover_github_repos(
        api,
        only_orgs=cfg.orgs,
        exclude_orgs=cfg.exclude_orgs,
        exclude_repos=cfg.exclude_repos,
        include_user_repos=cfg.include_user_repos,
        include_archived=cfg.include_archived,
        include_forks=cfg.include_forks,
        visibility=cfg.visibility,
    )

    return _finish(cfg, org_repos)


def _run_gitlab(cfg: ResolvedConfig) -> int:
    """GitLab discovery + evaluation flow."""
    api = GitLabAPI(cfg.token, base_url=cfg.gitlab_url)
    try:
        user = api.authenticate()
    except Exception as e:
        print(f"❌ ERROR: Failed to authenticate with GitLab — {e}", file=sys.stderr)
        print("   Possible causes:", file=sys.stderr)
        print("   • Token is invalid or expired", file=sys.stderr)
        print("   • Token doesn't have 'read_api' scope", file=sys.stderr)
        print("   • Wrong GitLab URL (try --gitlab-url for self-hosted)", file=sys.stderr)
        return 1

    username = user.get("username", user.get("name", "unknown"))
    print(f"🔑 Authenticated as: {username}  (GitLab @ {cfg.gitlab_url})")

    print("🔍 Discovering groups and projects …\n")
    org_repos = discover_gitlab_repos(
        api,
        only_groups=cfg.orgs,
        exclude_groups=cfg.exclude_orgs,
        exclude_repos=cfg.exclude_repos,
        include_user_repos=cfg.include_user_repos,
        include_archived=cfg.include_archived,
        include_forks=cfg.include_forks,
        visibility=cfg.visibility,
    )

    return _finish(cfg, org_repos)


def _run_bitbucket(cfg: ResolvedConfig) -> int:
    """Bitbucket discovery + evaluation flow."""
    api = BitbucketAPI(cfg.token, username=cfg.bitbucket_username or None)
    try:
        user = api.authenticated_user()
    except Exception as e:
        print(f"❌ ERROR: Failed to authenticate with Bitbucket — {e}", file=sys.stderr)
        print("   Possible causes:", file=sys.stderr)
        print("   • App Password is invalid or expired", file=sys.stderr)
        print("   • Token doesn't have required read scopes", file=sys.stderr)
        print("   • App passwords need --bitbucket-username <bitbucket_username>", file=sys.stderr)
        print("   • API tokens need --bitbucket-username <atlassian_email>", file=sys.stderr)
        return 1

    username = user.get("username", user.get("display_name", "unknown"))
    print(f"🔑 Authenticated as: {username}  (Bitbucket)")

    print("🔍 Discovering workspaces and repositories …\n")
    org_repos = discover_bitbucket_repos(
        api,
        only_workspaces=cfg.orgs,
        exclude_workspaces=cfg.exclude_orgs,
        exclude_repos=cfg.exclude_repos,
        include_user_repos=cfg.include_user_repos,
        include_archived=cfg.include_archived,
        include_forks=cfg.include_forks,
        visibility=cfg.visibility,
    )

    return _finish(cfg, org_repos)


def _finish(cfg: ResolvedConfig, org_repos: Dict[str, List[RepoInfo]]) -> int:
    """Common post-discovery logic: print, save, run."""
    if not org_repos or all(len(v) == 0 for v in org_repos.values()):
        scope = {"gitlab": "groups", "bitbucket": "workspaces"}.get(
            cfg.platform, "orgs"
        )
        print("ℹ️  No repos found.")
        print("   Possible causes:")
        print(f"   • The token doesn't have access to any {scope}")
        print("   • All repos were filtered out by --exclude-repo / --visibility")
        print("   • Try --include-user-repos to explicitly include personal repos")
        print(f"   • Check your token scopes\n")
        return 0

    # ── Print inventory ──────────────────────────────────────────────────────
    print_inventory(org_repos, cfg.platform)

    # ── Save inventory if requested ──────────────────────────────────────────
    if cfg.save_inventory:
        inv = {
            org: [
                {
                    "full_name": r.full_name,
                    "private": r.private,
                    "archived": r.archived,
                    "fork": r.fork,
                    "language": r.language,
                    "platform": r.platform,
                }
                for r in repos
            ]
            for org, repos in org_repos.items()
        }
        with open(cfg.save_inventory, "w") as f:
            json.dump(inv, f, indent=2)
        print(f"  💾 Inventory saved to {cfg.save_inventory}\n")

    # ── Dry-run stops here ───────────────────────────────────────────────────
    if cfg.dry_run:
        print("ℹ️  Dry-run mode — not running the evaluator.")
        print("   To execute, replace --dry-run with --run\n")
        return 0

    # ── Run evaluator ────────────────────────────────────────────────────────
    extra = cfg.evaluator_args.split() if cfg.evaluator_args.strip() else []

    # Resolve evaluator script path relative to this script
    script_dir = Path(__file__).resolve().parent
    evaluator_path = str(script_dir / cfg.evaluator_script)
    if not os.path.isfile(evaluator_path):
        evaluator_path = cfg.evaluator_script  # fallback to raw path

    results = run_all(
        org_repos,
        token=cfg.token,
        evaluator_script=evaluator_path,
        extra_args=extra,
        output_dir=cfg.output_dir,
        workers=cfg.workers,
        fail_fast=cfg.fail_fast,
        timeout_minutes=cfg.timeout,
    )

    print_summary(results, cfg.output_dir)

    failed_count = sum(1 for r in results if r.exit_code != 0)
    return 2 if failed_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())

