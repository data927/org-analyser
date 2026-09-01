"""GitHub provider — REST for listing/repo metadata, GraphQL for PR review stats.

Repo listing uses `platforms.github.list_repo_items`, which follows the
`Link` response header (`rel="next"`) and includes private personal repos
via `GET /user/repos` when the token owner matches the requested login.
"""

from __future__ import annotations

import logging

import requests

from platforms.github import github_api, list_repo_items

from .base import GitProvider, ProviderError, RemoteRepo

logger = logging.getLogger(__name__)

_PR_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:MERGED, first:100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { reviewDecision reviews { totalCount } }
    }
  }
}
"""


class GitHubProvider(GitProvider):
    platform = "github"

    def __init__(self, token: str | None = None, host: str | None = None) -> None:
        super().__init__(token, host)
        # Supports GitHub Enterprise via a custom host.
        base = host or "github.com"
        self.api = github_api(base)
        self.graphql = (
            "https://api.github.com/graphql" if base == "github.com"
            else f"https://{base}/api/graphql"
        )
        self.web_host = base

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def list_repos(self, org: str) -> list[RemoteRepo]:
        try:
            items = list_repo_items(self.token or "", org, self.web_host)
        except Exception as exc:
            raise ProviderError(str(exc)) from exc
        repos = [self._to_repo(r) for r in items]
        if not repos:
            raise ProviderError(f"no repositories found for '{org}' on {self.web_host}")
        return repos

    def get_repo(self, owner: str, name: str) -> RemoteRepo:
        data, _ = self._get_json(f"{self.api}/repos/{owner}/{name}")
        return self._to_repo(data)

    def _to_repo(self, r: dict) -> RemoteRepo:
        full = r["full_name"]
        owner, _, name = full.partition("/")
        return RemoteRepo(
            platform="github",
            owner=owner,
            name=name,
            clone_url=r.get("clone_url") or f"https://{self.web_host}/{full}.git",
            default_branch=r.get("default_branch"),
            is_fork=r.get("fork"),
            is_private=r.get("private"),
        )

    def pr_stats(self, repo: RemoteRepo) -> tuple[int, int]:
        total = reviewed = 0
        cursor = None
        while True:
            variables = {"owner": repo.owner, "name": repo.name, "cursor": cursor}
            data = self._post_json(self.graphql, {"query": _PR_QUERY, "variables": variables})
            if data.get("errors"):
                raise ProviderError(str(data["errors"])[:300])
            prs = data["data"]["repository"]["pullRequests"]
            for node in prs["nodes"]:
                total += 1
                decision = node.get("reviewDecision")
                has_reviews = (node.get("reviews") or {}).get("totalCount", 0) > 0
                if decision in ("APPROVED", "CHANGES_REQUESTED") or has_reviews:
                    reviewed += 1
            if not prs["pageInfo"]["hasNextPage"]:
                break
            cursor = prs["pageInfo"]["endCursor"]
        return total, reviewed

    def _resolve_fork(self, repo: RemoteRepo) -> bool:
        data, _ = self._get_json(f"{self.api}/repos/{repo.owner}/{repo.name}")
        return bool(data.get("fork"))

    def auth_clone_url(self, repo: RemoteRepo) -> str:
        if not self.token:
            return repo.clone_url
        return repo.clone_url.replace(
            "https://", f"https://x-access-token:{self.token}@", 1
        )
