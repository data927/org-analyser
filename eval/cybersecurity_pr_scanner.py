#!/usr/bin/env python3
"""
Scan GitHub pull requests for cybersecurity relevance using two layers:

  Layer 1 — High-recall heuristics (labels, keywords, changed-file paths, bots).
  Layer 2 — Optional LLM pass (OpenAI) only when layer 1 marks a PR as a candidate.

Every PR is included in the output with title, body, labels, URLs, and layer results.

Examples:
  python cybersecurity_pr_scanner.py --repo owner/name --json-out results.json
  python cybersecurity_pr_scanner.py --org my-org --max-repos 5 --json-out org.json
  python cybersecurity_pr_scanner.py --repo owner/name --skip-layer2 --json-out l1_only.json

Auth: GITHUB_TOKEN / GH_TOKEN / --token (CLI wins). OPENAI_API_KEY for layer 2.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from platforms.github import github_headers
from platforms.github import paginate as github_platform_paginate

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# --- Layer 1: broad cybersecurity signals ---------------------------------

STRONG_TEXT_PATTERNS = [
    r"\bCVE-\d{4}-\d+\b",
    r"\bGHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b",
    r"\bcvss\b",
    r"\bCWE-\d+\b",
    r"\bvulnerabilit",
    r"\badvisories?\b",
    r"\bsecurity\s+patch\b",
    r"\bpenetration\b",
    r"\bpen[- ]test\b",
    r"\bxss\b",
    r"\bcsrf\b",
    r"\bssrf\b",
    r"\bsql\s*injection\b",
    r"\brce\b",
    r"\bpath\s*traversal\b",
    r"\bopen\s*redirect\b",
    r"\bauth(n|z)?\b",
    r"\bauthentication\b",
    r"\bauthorization\b",
    r"\boauth\b",
    r"\boidc\b",
    r"\bsaml\b",
    r"\bjwt\b",
    r"\bmfa\b",
    r"\b2fa\b",
    r"\bssh\b",
    r"\btls\b",
    r"\bssl\b",
    r"\bcertificate\b",
    r"\bcryptograph",
    r"\bencrypt",
    r"\bdecrypt",
    r"\bhashing\b",
    r"\bbcrypt\b",
    r"\bargon2\b",
    r"\bsandbox\b",
    r"\bsaniti[sz]e\b",
    r"\bhardening\b",
    r"\bfirewall\b",
    r"\biam\b",
    r"\brbac\b",
    r"\bsecrets?\b",
    r"\bcredential\b",
    r"\bkey\s*rotation\b",
    r"\bprivilege\b",
    r"\bescalation\b",
    r"\bbypass\b",
    r"\bbounty\b",
    r"\bmalware\b",
    r"\bexploit\b",
    r"\bsiems?\b",
    r"\baudit\s*log\b",
    r"\bcompliance\b",
    r"\bgdpr\b",
    r"\bhipaa\b",
    r"\bsoc\s*2\b",
    r"\bsbom\b",
    r"\bsupply\s*chain\b",
    r"\bdos\b|\bddos\b",
    r"\brate\s*limit\b",
    r"\bcors\b",
    r"\bcsp\b",
    r"\bcontent[- ]security[- ]policy\b",
]

WEAK_TEXT_PATTERNS = [
    r"\bsecurity\b",
    r"\bsafe(ty)?\b",
    r"\bfix\s+auth\b",
    r"\blockdown\b",
    r"\brestrict\b",
    r"\bpermission\b",
]

LABEL_HINTS = [
    "security",
    "vulnerability",
    "vulnerabilities",
    "dependabot",
    "dependencies",
    "renovate",
    "snyk",
]

PATH_HINTS = [
    r"(^|/)security/",
    r"(^|/)auth/",
    r"(^|/)authentication/",
    r"(^|/)authorization/",
    r"(^|/)iam/",
    r"(^|/)crypto/",
    r"(^|/)cryptography/",
    r"(^|/)oauth",
    r"(^|/)ssl/",
    r"(^|/)tls/",
    r"(^|/)secrets?",
    r"(^|/)vault/",
    r"(^|/)kms/",
    r"(^|/)\.github/workflows/",
    r"(^|/)firewall",
    r"(^|/)waf/",
    r"(^|/)policies/",
    r"(^|/)rbac",
    r"\.pem$",
    r"id_rsa",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"\.env",
    r"Dockerfile",
    r"(^|/)helm/",
    r"(^|/)terraform/",
    r"\.tf$",
    r"\.tfvars$",
]

BOT_LOGINS = {
    "dependabot",
    "dependabot[bot]",
    "renovate",
    "renovate[bot]",
    "snyk-bot",
    "imgbot",
}


@dataclass
class Layer1Result:
    passed: bool
    score: int
    signals: List[str] = field(default_factory=list)


@dataclass
class Layer2Result:
    is_security_related: bool
    confidence: str
    categories: List[str]
    rationale: str
    raw: Optional[Dict[str, Any]] = None


def _compile(patterns: List[str]) -> List[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_STRONG_RE = _compile(STRONG_TEXT_PATTERNS)
_WEAK_RE = _compile(WEAK_TEXT_PATTERNS)
_PATH_RE = [re.compile(p, re.IGNORECASE) for p in PATH_HINTS]


class _FlushStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        for h in root.handlers:
            h.setLevel(level)
        return
    h = _FlushStreamHandler(sys.stderr)
    h.setLevel(level)
    h.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [cyber-pr-scan] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(h)


class GitHubClient:
    """Thin wrapper over `platforms.github` -- auth, retry/backoff, and
    `Link`-header pagination all live there now; this just adapts the
    relative-path/params calling convention this file's callers already use.
    """

    def __init__(self, token: Optional[str]) -> None:
        self.session = requests.Session()
        self.session.headers.update(github_headers(token or ""))
        self.session.headers["User-Agent"] = "org-analyser-cybersecurity-pr-scanner"

    def paginate(self, path: str, params: Optional[dict] = None) -> List[dict]:
        url = f"{GITHUB_API}{path}"
        out = github_platform_paginate(self.session, url, params=params)
        logger.info("GitHub paginate done %s — %d row(s)", path.split("?", 1)[0], len(out))
        return out


def _token(cli_token: Optional[str]) -> Optional[str]:
    return (
        cli_token
        or os.getenv("GITHUB_TOKEN")
        or os.getenv("GH_TOKEN")
        or None
    )


def layer1_evaluate(
    title: str,
    body: str,
    labels: List[str],
    file_paths: List[str],
    author_login: str,
) -> Layer1Result:
    signals: List[str] = []
    score = 0
    text = f"{title or ''}\n{body or ''}"

    for rx in _STRONG_RE:
        if rx.search(text):
            signals.append(f"text:{rx.pattern[:48]}")
            score += 4
            break
    else:
        weak_hits = sum(1 for rx in _WEAK_RE if rx.search(text))
        if weak_hits:
            signals.append(f"weak_keywords:{weak_hits}")
            score += min(weak_hits * 2, 4)

    low_labels = [x.lower() for x in labels]
    for hint in LABEL_HINTS:
        if any(hint in lab for lab in low_labels):
            signals.append(f"label:{hint}")
            score += 4
            break

    for fp in file_paths:
        for rx in _PATH_RE:
            if rx.search(fp.replace("\\", "/")):
                signals.append(f"path:{fp[:80]}")
                score += 2
                break

    al = (author_login or "").lower()
    if al in BOT_LOGINS or al.endswith("[bot]") and any(
        b in al for b in ("dependabot", "renovate", "snyk")
    ):
        signals.append(f"bot:{author_login}")
        score += 3

    score = min(score, 30)
    return Layer1Result(passed=False, score=score, signals=signals)


def _layer1_finalize(l1: Layer1Result, threshold: int) -> Layer1Result:
    l1.passed = l1.score >= threshold
    return l1


def fetch_pr_files(client: GitHubClient, owner: str, repo: str, number: int) -> List[str]:
    path = f"/repos/{owner}/{repo}/pulls/{number}/files"
    try:
        logger.debug("Fetching changed files for %s/%s PR #%s", owner, repo, number)
        files = client.paginate(path, {"per_page": 100})
        paths = [f.get("filename", "") for f in files if f.get("filename")]
        logger.debug("PR #%s → %d changed file path(s)", number, len(paths))
        return paths
    except requests.HTTPError as e:
        logger.warning("Could not list files for %s/%s PR #%s: %s", owner, repo, number, e)
        return []


def list_pulls(
    client: GitHubClient,
    owner: str,
    repo: str,
    max_prs: Optional[int],
) -> List[dict]:
    logger.info("Listing pull requests for %s/%s (state=all, sort=updated desc)…", owner, repo)
    pulls = client.paginate(
        f"/repos/{owner}/{repo}/pulls",
        {"state": "all", "per_page": 100, "sort": "updated", "direction": "desc"},
    )
    before = len(pulls)
    if max_prs is not None:
        pulls = pulls[:max_prs]
    logger.info("Loaded %d PR(s) for %s/%s%s", len(pulls), owner, repo, f" (capped from {before})" if max_prs is not None and before > len(pulls) else "")
    return pulls


def list_org_repo_full_names(client: GitHubClient, org: str, max_repos: Optional[int]) -> List[str]:
    logger.info("Listing repositories for org %r…", org)
    repos = client.paginate(f"/orgs/{org}/repos", {"per_page": 100, "type": "all"})
    names = []
    for r in repos:
        fn = r.get("full_name")
        if fn:
            names.append(fn)
    names.sort()
    if max_repos is not None:
        names = names[:max_repos]
    logger.info("Org %s: %d repository name(s) to scan%s", org, len(names), f" (limit {max_repos})" if max_repos else "")
    return names


def scan_repo(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    max_prs: Optional[int],
    layer1_threshold: int,
    run_layer2: bool,
    layer2_model: str,
    fetch_files: bool,
    batch_work_dir: Optional[Path] = None,
    llm_mode: str = "auto",
    llm_batch_threshold: int = 50,
) -> Dict[str, Any]:
    full_name = f"{owner}/{repo}"
    logger.info(
        "━━ Scan start %s ━━ layer1_threshold=%s layer2=%s fetch_files=%s",
        full_name,
        layer1_threshold,
        run_layer2,
        fetch_files,
    )
    pulls = list_pulls(client, owner, repo, max_prs)
    total = len(pulls)
    results: List[Dict[str, Any]] = []
    l1_passed = 0
    prog_every = max(1, total // 20) if total > 40 else max(5, total // 10 or 1)

    # Pass 1: Layer1 (local heuristics) + file listing for every PR. Collects
    # the full set of Layer1-passing PRs before any Layer2 call is made --
    # batch processing needs the whole request set up front, so nothing here
    # calls the LLM yet.
    layer2_contexts: List[Dict[str, Any]] = []
    for idx, pr in enumerate(pulls, start=1):
        num = pr["number"]
        if idx == 1 or idx == total or idx % prog_every == 0:
            logger.info(
                'Analyzing PRs %s/%s — #%d ("%s")',
                idx,
                total,
                num,
                (pr.get("title") or "")[:60] + ("..." if len((pr.get("title") or "")) > 60 else ""),
            )
        labels = [lb.get("name", "") for lb in pr.get("labels", []) if lb.get("name")]
        user = (pr.get("user") or {}) or {}
        author = user.get("login") or ""
        title = pr.get("title") or ""
        body = pr.get("body") or ""

        paths: List[str] = []
        if fetch_files:
            paths = fetch_pr_files(client, owner, repo, num)

        l1 = layer1_evaluate(title, body, labels, paths, author)
        _layer1_finalize(l1, layer1_threshold)

        row: Dict[str, Any] = {
            "number": num,
            "html_url": pr.get("html_url"),
            "state": pr.get("state"),
            "draft": pr.get("draft"),
            "merged_at": pr.get("merged_at"),
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
            "title": title,
            "body": body,
            "author": author,
            "labels": labels,
            "base_ref": (pr.get("base") or {}).get("ref"),
            "head_ref": (pr.get("head") or {}).get("ref"),
            "changed_files_reported": pr.get("changed_files"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "files_sample": paths[:200],
            "files_truncated": len(paths) > 200,
            "layer1": asdict(l1),
            "layer2": None,
        }

        if l1.passed:
            l1_passed += 1
            logger.info(
                "Layer1 PASS PR #%d score=%d · %s",
                num,
                l1.score,
                "; ".join(l1.signals[:5]) + (" …" if len(l1.signals) > 5 else ""),
            )
            if run_layer2:
                layer2_contexts.append(
                    {
                        "number": num,
                        "repo_full": full_name,
                        "title": title,
                        "body": body,
                        "labels": labels,
                        "file_paths": paths[:80],
                        "layer1_signals": l1.signals,
                    }
                )
            else:
                row["layer2"] = {
                    "skipped": True,
                    "reason": "Layer 2 not run (--skip-layer2)",
                }
                logger.info("Layer2 skipped (--skip-layer2) for PR #%d", num)

        results.append(row)

    # Pass 2: one batched (or, below threshold, thread-pooled sync) Layer2
    # call for every Layer1-passing PR in this repo instead of one live
    # request per PR.
    if layer2_contexts:
        logger.info(
            "Layer2: classifying %d candidate PR(s) for %s (llm_mode=%s, model=%s)",
            len(layer2_contexts), full_name, llm_mode, layer2_model,
        )
        work_dir = batch_work_dir or (Path("outputs") / "batch_state" / full_name.replace("/", "_"))
        layer2_by_number = run_layer2_llm_batch(
            layer2_contexts, layer2_model, work_dir, tag=full_name.replace("/", "_"),
            mode=llm_mode, threshold=llm_batch_threshold,
        )
    else:
        layer2_by_number = {}

    l2_calls = len(layer2_contexts)
    candidate_numbers = {ctx["number"] for ctx in layer2_contexts}
    for row in results:
        num = row["number"]
        if num not in candidate_numbers:
            # Either failed Layer1 (row["layer2"] stays None) or Layer2 was
            # disabled (row["layer2"] already holds the skipped-marker dict).
            continue
        l2 = layer2_by_number.get(num)
        row["layer2"] = asdict(l2) if l2 else {"error": "layer2_failed"}
        if l2:
            logger.info(
                "Layer2 done PR #%d → security_related=%s conf=%s tags=%s",
                num, l2.is_security_related, l2.confidence, l2.categories,
            )

    logger.info(
        "━━ Scan done %s ━━ PRs=%d layer1_passed=%d layer2_calls=%d",
        full_name,
        total,
        l1_passed,
        l2_calls,
    )

    return {
        "owner": owner,
        "repo": repo,
        "full_name": f"{owner}/{repo}",
        "pull_requests": results,
    }


LAYER2_INSTRUCTIONS = """You classify GitHub pull requests for cybersecurity relevance.
Cybersecurity includes: vulns/CVEs/advisories, authn/z, crypto/TLS/secrets, IAM/RBAC,
application security (XSS, CSRF, injection, etc.), infra hardening, supply chain,
privacy/compliance-related controls, security logging/monitoring, and dependency updates
that address security.

Return a single JSON object with keys:
  "is_security_related" (boolean),
  "confidence" ("high"|"medium"|"low"),
  "categories" (array of short strings, e.g. "supply_chain", "auth", "secrets"),
  "rationale" (1-3 short sentences, plain text).
Be conservative: if the change is only cosmetic or unrelated, is_security_related must be false."""


def _layer2_messages(
    *, repo_full: str, title: str, body: str, labels: List[str], file_paths: List[str],
    layer1_signals: List[str],
) -> List[Dict[str, str]]:
    user_payload = {
        "repository": repo_full,
        "title": title,
        "body": body[:12000],
        "labels": labels,
        "changed_files": file_paths,
        "layer1_signals": layer1_signals,
    }
    return [
        {"role": "system", "content": LAYER2_INSTRUCTIONS},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def _layer2_result_from_content(content: Optional[str], error: Optional[str]) -> Layer2Result:
    if error:
        return Layer2Result(
            is_security_related=False, confidence="none", categories=[],
            rationale=f"OpenAI error: {error}", raw=None,
        )
    try:
        data = json.loads((content or "").strip())
    except json.JSONDecodeError as e:
        return Layer2Result(
            is_security_related=False, confidence="none", categories=[],
            rationale=f"LLM JSON parse error: {e}", raw=None,
        )
    return Layer2Result(
        is_security_related=bool(data.get("is_security_related")),
        confidence=str(data.get("confidence") or "low"),
        categories=list(data.get("categories") or []),
        rationale=str(data.get("rationale") or ""),
        raw=data,
    )


def run_layer2_llm(
    *,
    repo_full: str,
    title: str,
    body: str,
    labels: List[str],
    file_paths: List[str],
    layer1_signals: List[str],
    model: str,
) -> Optional[Layer2Result]:
    """Single-PR entry point, kept for direct/interactive use. scan_repo below
    batches all of a repo's Layer1-passing PRs through run_layer2_llm_batch
    instead of calling this in a loop."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    try:
        from llm.llm_safety import llm_available, safe_openai
    except ImportError:
        logger.warning("Layer2 skipped: openai package not installed")
        return Layer2Result(
            is_security_related=False, confidence="none", categories=[],
            rationale="openai package not installed", raw=None,
        )
    if not llm_available():
        logger.warning("Layer2 skipped: no LLM configured (OpenAI or Azure)")
        return Layer2Result(
            is_security_related=False, confidence="none", categories=[],
            rationale="no LLM configured", raw=None,
        )

    from llm.batch import BatchItem  # noqa: WPS433

    client = safe_openai(api_key=key)
    item = BatchItem(
        custom_id="single",
        messages=_layer2_messages(
            repo_full=repo_full, title=title, body=body, labels=labels,
            file_paths=file_paths, layer1_signals=layer1_signals,
        ),
        model=model,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    result = _sync_layer2_item(client, item)
    return _layer2_result_from_content(result.content, result.error)


def _sync_layer2_item(client: Any, item: "BatchItem") -> "BatchItemResult":
    from llm.batch import BatchItemResult  # noqa: WPS433

    try:
        resp = client.chat.completions.create(
            model=item.model,
            temperature=item.temperature,
            response_format=item.response_format,
            messages=item.messages,
        )
        content = (resp.choices[0].message.content or "").strip()
        return BatchItemResult(item.custom_id, True, content, None, item.metadata)
    except Exception as e:
        logger.exception("Layer2 OpenAI request failed for custom_id=%s", item.custom_id)
        return BatchItemResult(item.custom_id, False, None, str(e), item.metadata)


def run_layer2_llm_batch(
    pr_contexts: List[Dict[str, Any]],
    model: str,
    work_dir: Path,
    tag: str,
    mode: str = "auto",
    threshold: int = 50,
    max_workers: int = 8,
) -> Dict[int, Layer2Result]:
    """Batch Layer2 across every Layer1-passing PR in one repo scan, instead
    of one live chat.completions.create call per PR. Each entry in
    `pr_contexts` needs: number, repo_full, title, body, labels, file_paths,
    layer1_signals. Returns {pr_number: Layer2Result}."""
    from llm.batch import BatchItem, run_batch_or_sync  # noqa: WPS433
    from llm.llm_safety import llm_available, safe_openai  # noqa: WPS433

    if not pr_contexts:
        return {}
    if not llm_available():
        logger.warning("Layer2 skipped: no LLM configured (OpenAI or Azure)")
        return {
            ctx["number"]: Layer2Result(
                is_security_related=False, confidence="none", categories=[],
                rationale="no LLM configured", raw=None,
            )
            for ctx in pr_contexts
        }

    client = safe_openai()
    items = [
        BatchItem(
            custom_id=str(ctx["number"]),
            messages=_layer2_messages(
                repo_full=ctx["repo_full"], title=ctx["title"], body=ctx["body"],
                labels=ctx["labels"], file_paths=ctx["file_paths"],
                layer1_signals=ctx["layer1_signals"],
            ),
            model=model,
            temperature=0.1,
            response_format={"type": "json_object"},
            metadata={"number": ctx["number"]},
        )
        for ctx in pr_contexts
    ]
    results = run_batch_or_sync(
        client, items, work_dir, tag=tag,
        sync_fn=lambda item: _sync_layer2_item(client, item),
        mode=mode, threshold=threshold, max_workers=max_workers,
    )
    return {
        r.metadata["number"]: _layer2_result_from_content(r.content, None if r.ok else (r.error or "unknown error"))
        for r in results
    }


def parse_owner_repo(full: str) -> Tuple[str, str]:
    full = full.strip()
    if full.count("/") != 1:
        raise ValueError(f"Expected owner/repo, got: {full!r}")
    owner, repo = full.split("/", 1)
    if not owner or not repo:
        raise ValueError(f"Invalid owner/repo: {full!r}")
    return owner, repo


def main() -> int:
    p = argparse.ArgumentParser(description="GitHub PR cybersecurity scanner (layer1 + optional LLM layer2)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="Repository as owner/name")
    src.add_argument("--org", help="Scan every repo in this GitHub org (use --max-repos to limit)")
    p.add_argument("--token", default=None, help="GitHub token (or GITHUB_TOKEN / GH_TOKEN)")
    p.add_argument("--max-prs", type=int, default=None, help="Limit PRs per repo (newest first by update time)")
    p.add_argument("--max-repos", type=int, default=None, help="With --org, max repositories to scan")
    p.add_argument(
        "--layer1-threshold",
        type=int,
        default=6,
        help="Minimum layer1 score to treat as candidate (default: 6). Lower = more layer2 calls.",
    )
    p.add_argument("--skip-layer2", action="store_true", help="Heuristics only; no OpenAI")
    p.add_argument(
        "--no-fetch-files",
        action="store_true",
        help="Do not list per-PR files (faster, weaker layer1 path signals)",
    )
    p.add_argument("--layer2-model", default="gpt-4o-mini", help="OpenAI model for layer2")
    p.add_argument(
        "--llm-mode",
        choices=("auto", "batch", "sync"),
        default="auto",
        help="Layer2 LLM call strategy: 'batch' submits every candidate PR in a repo as "
        "one OpenAI Batch API job (~50%% cheaper, no live-request-per-PR cost), 'sync' "
        "is one live call per PR, 'auto' (default) batches once a repo has "
        "--llm-batch-threshold or more candidates.",
    )
    p.add_argument(
        "--llm-batch-threshold",
        type=int,
        default=50,
        help="Candidate-PR count at/above which --llm-mode=auto switches to the Batch API.",
    )
    p.add_argument(
        "--batch-work-dir",
        default=None,
        help="Directory for Batch API request/state files (default: alongside --json-out).",
    )
    p.add_argument(
        "--json-out",
        required=True,
        help="Write full results to this JSON file",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose logging (DEBUG): include per-page GitHub pagination details",
    )
    args = p.parse_args()
    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    token = _token(args.token)
    if not token:
        logger.warning("No GitHub token — rate limits will be strict")
    else:
        logger.info("GitHub authentication: bearer token configured (length %d)", len(token))

    gh = GitHubClient(token)
    run_l2 = not args.skip_layer2
    fetch_files = not args.no_fetch_files

    logger.info(
        "Starting run · json_out=%s max_prs=%s max_repos=%s layer1_threshold=%s fetch_files=%s layer2=%s model=%s",
        args.json_out,
        args.max_prs,
        args.max_repos,
        args.layer1_threshold,
        fetch_files,
        run_l2,
        args.layer2_model,
    )

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "layer1_threshold": args.layer1_threshold,
        "layer2_enabled": run_l2,
        "repositories": [],
    }

    batch_work_dir = Path(args.batch_work_dir) if args.batch_work_dir else Path(args.json_out).with_suffix("").parent / "batch_state"

    if args.repo:
        logger.info("Mode: single repo %s", args.repo)
        owner, name = parse_owner_repo(args.repo)
        report["repositories"].append(
            scan_repo(
                gh,
                owner,
                name,
                max_prs=args.max_prs,
                layer1_threshold=args.layer1_threshold,
                run_layer2=run_l2,
                layer2_model=args.layer2_model,
                fetch_files=fetch_files,
                batch_work_dir=batch_work_dir,
                llm_mode=args.llm_mode,
                llm_batch_threshold=args.llm_batch_threshold,
            )
        )
    else:
        logger.info("Mode: organization %r", args.org.strip())
        names = list_org_repo_full_names(gh, args.org.strip(), args.max_repos)
        logger.info("Will scan %d repository name(s)", len(names))
        report["org"] = args.org
        report["repo_full_names"] = names
        for fn in names:
            o, r = parse_owner_repo(fn)
            try:
                logger.info("━━ Repo %s ━━", fn)
                report["repositories"].append(
                    scan_repo(
                        gh,
                        o,
                        r,
                        max_prs=args.max_prs,
                        layer1_threshold=args.layer1_threshold,
                        run_layer2=run_l2,
                        layer2_model=args.layer2_model,
                        fetch_files=fetch_files,
                        batch_work_dir=batch_work_dir / fn.replace("/", "_"),
                        llm_mode=args.llm_mode,
                        llm_batch_threshold=args.llm_batch_threshold,
                    )
                )
            except requests.HTTPError as e:
                logger.error("HTTP error for %s: %s", fn, e)
                report["repositories"].append(
                    {
                        "full_name": fn,
                        "error": str(e),
                        "pull_requests": [],
                    }
                )

    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "Wrote JSON report (%d repo entr(y/ies)) → %s",
        len(report.get("repositories") or []),
        out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
