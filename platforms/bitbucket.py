"""Bitbucket client: per-repo PR/issue fetching (REST 2.0) plus the shared
auth-resolution, repo-listing, and raw pagination primitives used by every
Bitbucket caller.

`resolve_bitbucket_auth` is the single source of truth for the 3-branch
REST-auth decision (username set -> Basic; Atlassian API token (ATATT...)
with no username -> raise; else -> Bearer) that was duplicated, with real
behavioral drift, across 5 places in this repo. `resolve_bitbucket_git_auth`
is a *separate* decision for git-clone-over-HTTPS: Bitbucket's git and REST
endpoints accept different usernames for the same ATATT token, so the two
must not be conflated (see its docstring).

`BitbucketClient` is moved largely unchanged from `eval/platform_clients.py`
-- it keeps that module's own `retry_api_call` policy for its REST calls --
except `_configure_auth`/`get_repo_url` now both route through
`resolve_bitbucket_auth` instead of each re-deciding the same 3 branches
(the latter was a real audit bug: `get_repo_url` ignored `_configure_auth`'s
branching entirely and always used the `x-token-auth` sentinel, which is
wrong for an Atlassian API token).
"""

from __future__ import annotations

import base64
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

import requests

from .base import PlatformClient, _is_bot_username, request_with_retry, retry_api_call
from .errors import PlatformAuthError, PlatformError

logger = logging.getLogger(__name__)


def resolve_bitbucket_auth(token: str, username: str = "") -> tuple[str, str]:
    """The 3-branch Bitbucket Cloud REST-auth decision, single-sourced.

    Returns (scheme, basic_user):
      ("basic", user)  -- caller builds HTTP Basic base64(user:token), or
                           sets `session.auth = (user, token)`.
      ("bearer", "")   -- caller sets `Authorization: Bearer {token}`.

    Raises PlatformAuthError if `token` is an Atlassian API token (ATATT...)
    and no username was supplied -- that combination returns a misleading
    "Token is invalid" from the REST API rather than a usable 401, so it is
    rejected up front instead of being sent.
    """
    user = username.strip()
    if user:
        return "basic", user
    if token.startswith("ATATT"):
        raise PlatformAuthError(
            "Atlassian API token (ATATT…) needs your Atlassian account email. "
            "Set bitbucket_username to that email in the tokens file."
        )
    return "bearer", ""


def resolve_bitbucket_git_auth(token: str, username: str = "") -> str:
    """The git-clone-over-HTTPS username to pair with `token`.

    Genuinely different from `resolve_bitbucket_auth`: git-over-HTTPS
    accepts the static "x-bitbucket-api-token-auth" username for an
    Atlassian API token (ATATT...) -- unlike the REST API, which rejects an
    email/username for that same token type with a misleading "Token is
    invalid". So the ATATT branch here returns a sentinel instead of raising,
    and it takes priority over any configured username (git only ever wants
    the sentinel for this token type).
    """
    if token.startswith("ATATT"):
        return "x-bitbucket-api-token-auth"
    user = username.strip()
    if user:
        return user
    return "x-token-auth"


class BitbucketClient(PlatformClient):
    def __init__(
        self,
        owner: str,
        repo_name: str,
        token: Optional[str] = None,
        username: Optional[str] = None,
    ):
        super().__init__(owner, repo_name, token)
        self.base_url = "https://api.bitbucket.org/2.0"
        self.username = (
            username
            or os.getenv("BITBUCKET_USERNAME")
            or os.getenv("BITBUCKET_EMAIL")
        )
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._configure_auth()

    def _configure_auth(self) -> None:
        if not self.token:
            return
        scheme, user = resolve_bitbucket_auth(self.token, self.username or "")
        if scheme == "basic":
            self.session.auth = (user, self.token)
        else:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def fetch_prs(self, cursor: Optional[str] = None, page_size: int = 50, start_date: Optional[datetime] = None) -> dict:
        if cursor and cursor.startswith("http"):
            request_url = cursor
            params = None
        else:
            request_url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/pullrequests"
            params = {"state": "MERGED", "pagelen": page_size, "sort": "-created_on"}
            if cursor:
                params["page"] = cursor
            if start_date:
                params["q"] = f"created_on>={start_date.isoformat()}"

        def _make_request():
            response = self.session.get(request_url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()

        data = retry_api_call(_make_request)
        pr_nodes = []
        for pr in data.get("values", []):
            files_url = pr.get("links", {}).get("diffstat", {}).get("href", "")
            files = []
            if files_url:
                try:
                    def _get_files():
                        files_response = self.session.get(files_url, timeout=30)
                        files_response.raise_for_status()
                        return files_response.json()

                    files_data = retry_api_call(_get_files)
                    for file_info in files_data.get("values", []):
                        files.append(
                            {
                                "path": file_info.get("new", {}).get("path", file_info.get("old", {}).get("path", "")),
                                "changeType": "ADDED"
                                if file_info.get("status") == "added"
                                else "DELETED"
                                if file_info.get("status") == "deleted"
                                else "MODIFIED",
                                "additions": file_info.get("lines_added", 0),
                                "deletions": file_info.get("lines_removed", 0),
                            }
                        )
                except Exception:
                    pass

            linked_issues = []
            issue_numbers = self.extract_issue_number_from_text(pr.get("description", "") or "")
            for issue_num in issue_numbers:
                issue_data = self.fetch_issue(issue_num)
                if issue_data:
                    linked_issues.append(issue_data)

            author_info = pr.get("author", {}) or {}
            author_login = author_info.get("display_name") or author_info.get("username") or ""

            pr_nodes.append(
                {
                    "number": pr.get("id"),
                    "title": pr.get("title", ""),
                    "body": pr.get("description", "") or "",
                    "baseRefOid": pr.get("destination", {}).get("commit", {}).get("hash", ""),
                    "headRefOid": pr.get("source", {}).get("commit", {}).get("hash", ""),
                    "mergedAt": pr.get("closed_on", pr.get("updated_on", "")),
                    "createdAt": pr.get("created_on", ""),
                    "url": pr.get("links", {}).get("html", {}).get("href", ""),
                    "author": {"login": author_login, "isBot": _is_bot_username(author_login), "__typename": "User"},
                    "baseRepository": {"nameWithOwner": f"{self.owner}/{self.repo_name}"},
                    "headRepository": {"nameWithOwner": f"{self.owner}/{self.repo_name}"},
                    "files": {"nodes": files},
                    "closingIssuesReferences": {"nodes": linked_issues},
                    "labels": {"nodes": []},
                }
            )

        page_info = {"hasNextPage": data.get("next") is not None, "endCursor": data.get("next")}
        primary_language_name = None
        try:
            languages = self.fetch_repo_languages()
            if languages:
                primary_language_name = list(languages.keys())[0]
        except Exception:
            pass

        return {
            "data": {
                "repository": {
                    "primaryLanguage": {"name": primary_language_name},
                    "owner": {"login": self.owner},
                    "name": self.repo_name,
                    "pullRequests": {"pageInfo": page_info, "nodes": pr_nodes},
                }
            }
        }

    def fetch_issue(self, issue_number: int) -> Optional[dict]:
        try:
            url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/issues/{issue_number}"

            def _make_request():
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.json()

            issue_details = retry_api_call(_make_request)
            return {
                "number": issue_details.get("id"),
                "title": issue_details.get("title", ""),
                "body": issue_details.get("content", {}).get("raw", "")
                if isinstance(issue_details.get("content"), dict)
                else str(issue_details.get("content", "")),
                "state": issue_details.get("state", "").upper(),
                "__typename": "Issue",
            }
        except Exception:
            return None

    def get_repo_url(self, include_token: bool = False) -> str:
        if not (include_token and self.token):
            return f"https://bitbucket.org/{self.repo_full_name}.git"
        # git-clone-over-HTTPS, not the REST API -- resolve_bitbucket_git_auth,
        # not resolve_bitbucket_auth (the latter raises for an ATATT token with
        # no username, which is a REST-only restriction; git accepts ATATT
        # fine via the sentinel username).
        user = resolve_bitbucket_git_auth(self.token, self.username or "")
        return f"https://{user}:{self.token}@bitbucket.org/{self.repo_full_name}.git"

    def extract_issue_number_from_text(self, text: str) -> List[int]:
        if not text:
            return []
        issue_numbers = []
        issue_numbers.extend([int(m) for m in re.findall(r"#(\d+)", text)])
        issue_numbers.extend(
            [
                int(m)
                for m in re.findall(
                    r"https://bitbucket\.org/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/issues/(\d+)",
                    text,
                )
            ]
        )
        return list(set(issue_numbers))

    def fetch_repo_languages(self) -> Optional[Dict[str, int]]:
        try:
            url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}"

            def _make_request():
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.json()

            repo_data = retry_api_call(_make_request)
            language = repo_data.get("language")
            return {language: 1} if language else None
        except Exception as e:
            logger.debug(f"Failed to fetch repository language from Bitbucket API: {e}")
            return None

    def fetch_issue_count(self) -> dict:
        try:
            base = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/issues"

            def _count(state_query: str) -> int:
                def _make_request():
                    response = self.session.get(
                        base,
                        params={"q": state_query, "pagelen": 1},
                        timeout=30,
                    )
                    response.raise_for_status()
                    return response.json()

                data = retry_api_call(_make_request)
                return int(data.get("size", 0))

            open_count = _count('state="new" OR state="open"')
            closed_count = _count('state="resolved" OR state="closed"')
            return {"open": open_count, "closed": closed_count, "total": open_count + closed_count}
        except Exception:
            return {"open": 0, "closed": 0, "total": 0}

    def fetch_patch(self, base_commit: str, head_commit: str) -> Optional[str]:
        try:
            url = f"{self.base_url}/repositories/{self.owner}/{self.repo_name}/diff/{base_commit}..{head_commit}"

            def _make_request():
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.text

            return retry_api_call(_make_request)
        except Exception:
            return None


def bitbucket_headers(token: str, username: str = "") -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "org-analyser-platforms"}
    if not token:
        return headers  # anonymous: fine for public repos (lower rate limit)
    scheme, user = resolve_bitbucket_auth(token, username)
    if scheme == "basic":
        creds = base64.b64encode(f"{user}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def paginate(session: requests.Session, url: str) -> List[dict]:
    """Follow Bitbucket's `next` cursor URL field in the response body
    (not a header -- Bitbucket's own pagination contract, unlike GitHub's
    `Link` header or GitLab's `X-Next-Page`).

    `request_with_retry` returns None for both a 404 (not found) and a
    network error that exhausted its retries -- those must not be treated
    alike. A None on the *first* page is a legitimate "not found" (empty
    result). A None on a later page means pagination broke mid-flight, and
    silently returning the partial list collected so far would masquerade
    as a complete, correct result. Callers already catch this per-repo
    (cli.py's merged-PR fallback), so raise instead of truncating."""
    items: List[dict] = []
    next_url: Optional[str] = url
    first_page = True
    while next_url:
        response = request_with_retry(session, "GET", next_url)
        if response is None:
            if first_page:
                break
            raise PlatformError(
                f"Bitbucket pagination failed partway through {url} "
                f"({len(items)} item(s) fetched before the failure)"
            )
        data = response.json()
        if not isinstance(data, dict):
            break
        items.extend(data.get("values") or [])
        next_url = data.get("next")
        first_page = False
    return items


def list_repos(token: str, workspace: str, username: str = "") -> list[str]:
    """List repo full_names for a Bitbucket Cloud workspace."""
    session = requests.Session()
    session.headers.update(bitbucket_headers(token, username))
    items = paginate(session, f"https://api.bitbucket.org/2.0/repositories/{workspace}?pagelen=100")
    names = [r["full_name"] for r in items if r.get("full_name")]
    if not names:
        raise PlatformError(f"no repositories found for {workspace!r}")
    return names
