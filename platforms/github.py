"""GitHub client: per-repo PR/issue fetching (GraphQL + REST) plus the shared
repo-listing and raw pagination primitives used by every GitHub caller.

`GitHubClient` is moved largely unchanged from `eval/platform_clients.py` --
it keeps that module's own `retry_api_call` policy for its GraphQL/REST calls.
`list_repos`/`paginate` are new consolidated replacements for the ~3 duplicate
GitHub repo-listing implementations found across this repo, built on the
newer `request_with_retry` policy and following the `Link` response header
(`rel="next"`) for pagination -- the correct GitHub pagination contract that
only one of those duplicates (`eval/cybersecurity_pr_scanner.py`) actually
implemented.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

import requests

from eval.repo_evaluator_helpers import HEADERS

from .base import PlatformClient, request_with_retry, retry_api_call
from .errors import PlatformError

logger = logging.getLogger(__name__)


class GitHubClient(PlatformClient):
    def __init__(self, owner: str, repo_name: str, token: Optional[str] = None):
        super().__init__(owner, repo_name, token)
        self.base_url = "https://api.github.com"
        self.headers = HEADERS.copy()
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def fetch_prs(self, cursor: Optional[str] = None, page_size: int = 50, start_date: Optional[datetime] = None) -> dict:
        query = """
            query($owner: String!, $name: String!, $cursor: String, $page_size: Int!) {
            repository(owner: $owner, name: $name) {
              primaryLanguage { name }
              owner { login }
              name
              pullRequests(
                first: $page_size,
                after: $cursor,
                states: MERGED,
                orderBy: {field: CREATED_AT, direction: DESC}
              ) {
                pageInfo {
                  endCursor
                  hasNextPage
                }
                nodes {
                  number
                  title
                  body
                  baseRefOid
                  headRefOid
                  baseRefName
                  headRefName
                  mergedAt
                  createdAt
                  url
                  author {
                    login
                    __typename
                  }
                  files(first: 100) {
                    nodes {
                      path
                      changeType
                      additions
                      deletions
                    }
                  }
                  closingIssuesReferences(first: 10) {
                    nodes {
                      url
                      number
                      title
                      body
                      state
                      __typename
                    }
                  }
                  labels(first: 10) {
                    nodes {
                      name
                    }
                  }
                }
              }
            }
          }
        """
        variables = {
            "owner": self.owner,
            "name": self.repo_name,
            "cursor": cursor,
            "page_size": page_size,
        }

        def _make_request():
            response = requests.post(
                f"{self.base_url}/graphql",
                json={"query": query, "variables": variables},
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()

        return retry_api_call(_make_request)

    def fetch_issue(self, issue_number: int) -> Optional[dict]:
        try:
            def _make_request():
                response = requests.get(
                    f"{self.base_url}/repos/{self.repo_full_name}/issues/{issue_number}",
                    headers=self.headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response.json()

            issue_details = retry_api_call(_make_request)
            if "pull_request" in issue_details:
                return None

            return {
                "number": issue_details.get("number"),
                "title": issue_details.get("title", ""),
                "body": issue_details.get("body", ""),
                "state": issue_details.get("state", "").upper(),
                "__typename": "Issue",
            }
        except Exception:
            return None

    def fetch_issue_count(self) -> dict:
        query = """
            query($owner: String!, $name: String!) {
                repository(owner: $owner, name: $name) {
                    open: issues(states: OPEN) { totalCount }
                    closed: issues(states: CLOSED) { totalCount }
                }
            }
        """
        variables = {"owner": self.owner, "name": self.repo_name}

        def _make_request():
            response = requests.post(
                f"{self.base_url}/graphql",
                json={"query": query, "variables": variables},
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()

        result = retry_api_call(_make_request)
        repo = result.get("data", {}).get("repository", {})
        open_count = repo.get("open", {}).get("totalCount", 0)
        closed_count = repo.get("closed", {}).get("totalCount", 0)
        return {"open": open_count, "closed": closed_count, "total": open_count + closed_count}

    def get_repo_url(self, include_token: bool = False) -> str:
        if include_token and self.token:
            return f"https://{self.token}@github.com/{self.repo_full_name}.git"
        return f"https://github.com/{self.repo_full_name}.git"

    def extract_issue_number_from_text(self, text: str) -> List[int]:
        if not text:
            return []
        issue_numbers = []
        issue_numbers.extend([int(m) for m in re.findall(r"#(\d+)", text)])
        issue_numbers.extend(
            [
                int(m)
                for m in re.findall(
                    r"https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/issues/(\d+)",
                    text,
                )
            ]
        )
        return list(set(issue_numbers))

    def fetch_repo_languages(self) -> Optional[Dict[str, int]]:
        try:
            url = f"{self.base_url}/repos/{self.repo_full_name}/languages"

            def _make_request():
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response

            response = retry_api_call(_make_request)
            languages = response.json()
            return languages if languages else None
        except Exception as e:
            logger.debug(f"Failed to fetch repository languages from GitHub API: {e}")
            return None

    def fetch_patch(self, base_commit: str, head_commit: str) -> Optional[str]:
        diff_headers = self.headers.copy()
        diff_headers["Accept"] = "application/vnd.github.v3.diff"
        try:
            def _make_request():
                response = requests.get(
                    f"{self.base_url}/repos/{self.repo_full_name}/compare/{base_commit}...{head_commit}",
                    headers=diff_headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response.text

            return retry_api_call(_make_request)
        except Exception:
            return None


def github_api(host: str = "github.com") -> str:
    return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"


def github_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "org-analyser-platforms",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def paginate(session: requests.Session, url: str, params: Optional[dict] = None) -> List[dict]:
    """Raw REST pagination via the `Link` response header (`rel="next"`).

    Escape hatch for callers that need raw API dicts rather than a normalized
    repo-name list (e.g. CSV-column extraction over `/repos/{full}` fields).
    """
    results: List[dict] = []
    query = dict(params or {})
    next_url: Optional[str] = url
    while next_url:
        response = request_with_retry(session, "GET", next_url, params=query)
        query = {}  # subsequent Link URLs already carry their own query string
        if response is None:
            break
        batch = response.json()
        if not isinstance(batch, list):
            break
        results.extend(batch)
        next_url = None
        link = response.headers.get("Link", "")
        for part in link.split(","):
            if 'rel="next"' in part:
                match = re.search(r"<([^>]+)>", part)
                if match:
                    next_url = match.group(1)
                break
    return results


def list_repo_items(token: str, org: str, host: str = "github.com") -> list[dict]:
    """List non-archived repo payloads for a GitHub org or user.

    ``GET /users/{login}/repos`` never returns the authenticated user's private
    repos; GitHub requires ``GET /user/repos`` for those. When ``org`` matches
    the token owner, use that endpoint so private personal repos are included.
    """
    session = requests.Session()
    session.headers.update(github_headers(token))
    api = github_api(host)
    me = None
    response = request_with_retry(session, "GET", f"{api}/user")
    if response is not None and response.status_code == 200:
        try:
            me = (response.json() or {}).get("login")
        except ValueError:
            me = None
    if me and me.lower() == org.lower():
        items = paginate(
            session,
            f"{api}/user/repos",
            params={"per_page": 100, "affiliation": "owner", "visibility": "all"},
        )
        owned = [r for r in items if not r.get("archived")]
        if owned:
            return owned
    for kind in (f"orgs/{org}/repos", f"users/{org}/repos"):
        items = paginate(session, f"{api}/{kind}", params={"per_page": 100, "type": "all"})
        if items:
            return [r for r in items if not r.get("archived")]
    raise PlatformError(f"no repositories found for {org!r} on {host}")


def list_repos(token: str, org: str, host: str = "github.com") -> list[str]:
    """List non-archived repo full_names for a GitHub org or user."""
    return [r["full_name"] for r in list_repo_items(token, org, host)]
