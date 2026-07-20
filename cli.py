#!/usr/bin/env python3
"""
Unified repo pipeline: merged PR counts, PR task-profile report, repo-analyzer
(vendor CSV), codebase profiler, eval-kit (full LLM), and optionally
repo-quality-score (sealed) for one GitHub org, one GitLab group, one
Bitbucket workspace, or a folder of downloaded/local repos per run.

Tokens (github-data-token / gitlab_token / bitbucket_token / openai_key),
workers, hosts, default target, etc. all live in config.yml's `tokens:`
mapping and top-level keys at the repo root — see config.example.yml. CLI
flags always override config.yml. Pass --tokens-file to use a separate
key=value file instead.

Usage:
    org-analyser --github-org your-org --workers 10
    org-analyser --github-repo your-org/example-repo --workers 1
    org-analyser --github-repo your-org/repo-a --github-repo your-org/repo-b --workers 4
    org-analyser --gitlab-group your-group --workers 10
    org-analyser --gitlab-project my-group/repo-a --gitlab-project my-group/repo-b --workers 4
    org-analyser --bitbucket-workspace my-team --workers 10
    org-analyser --bitbucket-repo my-team/frontend --bitbucket-repo my-team/backend --workers 4
    org-analyser --local-repos-dir ./my-repos --workers 4
    org-analyser --github-org your-org --skip-quality-score
    org-analyser   # with target/tokens/etc. set in config.yml
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import certifi
from rich.console import Console

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

CODING = Path(__file__).resolve().parent
PROFILER_ROOT = CODING / "profiler"
EVAL_ROOT = CODING / "eval"
QUALITY_SKILL = CODING / "quality"


def load_config(path: Path) -> dict[str, Any]:
    """Load the optional root config.yml. Missing file -> empty config (all built-in defaults apply)."""
    if not path.is_file():
        return {}
    import yaml  # noqa: WPS433

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config file must be a YAML mapping: {path}")
    return data


CONFIG_PATH = Path(os.environ.get("ORG_ANALYSER_CONFIG", "") or (CODING / "config.yml"))
CONFIG = load_config(CONFIG_PATH)


def active_venv_python_candidates() -> list[str]:
    venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if not venv:
        return []
    root = Path(venv)
    return [
        str(root / "bin" / "python"),
        str(root / "Scripts" / "python.exe"),
    ]


PYTHON_CANDIDATES = [
    os.environ.get("ORG_ANALYSER_PYTHON", ""),
    CONFIG.get("python") or "",
    *active_venv_python_candidates(),
    sys.executable,
    str(CODING / ".venv" / "bin" / "python"),
    str(CODING / ".venv" / "Scripts" / "python.exe"),
    str(CODING / "env311" / "bin" / "python"),
    shutil.which("python3.12") or "",
    shutil.which("python3.11") or "",
    shutil.which("python3.10") or "",
]


def resolve_python() -> str:
    for candidate in PYTHON_CANDIDATES:
        if not candidate or not Path(candidate).exists() and "/" in candidate:
            if not candidate or ("/" not in candidate and not shutil.which(candidate)):
                continue
        exe = candidate
        try:
            proc = subprocess.run(
                [exe, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0:
                return exe
        except (OSError, subprocess.TimeoutExpired):
            continue
    return sys.executable


PYTHON_BIN = resolve_python()


def git_longpath_config() -> list[str]:
    """Git -c flags for Windows MAX_PATH; harmless on other platforms."""
    if os.name == "nt":
        return ["-c", "core.longpaths=true"]
    return []


def resolve_clones_dir(run_dir: Path) -> Path:
    """Clones always live outside run_dir, on every OS.

    This makes clone/source-code inclusion in the final zip structurally
    impossible (create_run_zip refuses to run if clones_dir is ever inside
    run_dir) rather than relying on a by-name skip list. On Windows this also
    doubles as the MAX_PATH workaround -- deep run folders (e.g. under
    Downloads) exceed git's GIT_DIR limit.
    """
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("TEMP")
            or "C:\\Temp"
        )
        return base / "org-analyser-clones" / run_dir.name
    return run_dir.parent / ".org-analyser-clones" / run_dir.name


sys.path.insert(0, str(CODING))

from analysis.merged_prs import (  # noqa: E402
    CSV_FIELDS,
    SUMMARY_FIELDS,
    count_bitbucket_merged,
    count_github_merged,
    count_gitlab_merged,
    export_bitbucket_repos,
    export_bitbucket_workspace,
    export_github_org,
    export_github_repos,
    export_gitlab_group,
    export_gitlab_projects,
    list_bitbucket_repos,
    list_github_repos,
    list_gitlab_projects,
    safe_filename,
)
from llm.credential_redactor import scrub_secrets  # noqa: E402
from llm.tree_redactor import redact_working_tree, write_redaction_report  # noqa: E402
from pipeline.progress import RunProgress, rich_console_handler, should_use_rich  # noqa: E402
from pipeline.state import (  # noqa: E402
    DONE_STATUSES,
    FAILED,
    OK,
    StateStore,
)
from platforms.bitbucket import resolve_bitbucket_git_auth  # noqa: E402

GITHUB_TOKEN_NAME = "github-data-token"
GITLAB_TOKEN_NAME = "gitlab_token"
BITBUCKET_TOKEN_NAME = "bitbucket_token"

# Every per-repo profiler row appends to one shared workbook, but the per-repo
# phases run concurrently — openpyxl load→save isn't atomic and an xlsx is a
# zip, so parallel writers corrupt it (BadZipFile) and fail on Windows file
# locks. Serialise the append (see run_profiler).
_PROFILER_WRITE_LOCK = threading.Lock()
# Optional: for Bitbucket app passwords, which authenticate as username+password.
# With a workspace/repo access token, leave the username blank.
BITBUCKET_USERNAME_NAME = "bitbucket_username"
OPENAI_TOKEN_NAMES = ("openai_key", "OPENAI_API_KEY")
# Azure AI Foundry / Azure OpenAI settings the tokens file may carry; promoted
# into the environment at startup (see preflight).
AZURE_TOKEN_NAMES = (
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT",
    "OPENAI_API_VERSION",
)
# Only needed when --pr-rubrics-provider is gemini; promoted the same way.
GEMINI_TOKEN_NAME = "gemini_key"

# codebase-profiler and repo-analyzer run ast.parse() on cloned repos' own
# Python source (arbitrary third-party code we don't control) to check
# syntax validity / collect metrics. Real-world source often has non-raw
# string literals with backslash sequences the parser flags -- noise about
# the target repo, not about org-analyser. ast.parse's default filename is
# the literal string "<unknown>", which lets this hook single those out
# without also swallowing a genuine warning from org-analyser's own code
# (which would carry a real file path). Installed once at import time --
# a plain module-global counter/lock, safe to hit from every repo-pool
# worker thread, unlike toggling warnings.catch_warnings() per call.
_target_repo_warning_count = 0
_target_repo_warning_lock = threading.Lock()
_default_showwarning = warnings.showwarning


def _quiet_target_repo_showwarning(message, category, filename, lineno, file=None, line=None):
    if category is SyntaxWarning and filename == "<unknown>":
        global _target_repo_warning_count
        with _target_repo_warning_lock:
            _target_repo_warning_count += 1
        return
    _default_showwarning(message, category, filename, lineno, file, line)


warnings.showwarning = _quiet_target_repo_showwarning


@dataclass
class RepoEntry:
    platform: str
    full_name: str
    org: str
    batch_org: str
    local_path: Path | None = None

    @property
    def is_local(self) -> bool:
        return self.platform == "local"

    @property
    def short_name(self) -> str:
        parts = self.full_name.split("/")
        rest = parts[1:]
        if len(rest) == 1:
            return rest[0]
        return "__".join(rest)

    @property
    def repo_slug(self) -> str:
        return self.full_name.split("/")[-1]


@dataclass
class RunContext:
    target: str
    platform: str
    run_dir: Path
    clones_dir: Path
    merged_pr_dir: Path
    profiler_dir: Path
    eval_kit_dir: Path
    quality_dir: Path
    task_profile_dir: Path
    repo_analyzer_dir: Path
    include_quality_score: bool
    logs_dir: Path
    tokens: dict[str, str]
    workers: int
    retries: int
    clone_depth: int | None
    skip_f2p: bool
    skip_pr_task_profile: bool
    pr_rubrics_provider: str
    local_only: bool
    github_host: str
    gitlab_host: str
    github_token_name: str
    local_repos_dir: Path | None
    repos_manifest: dict[str, str]
    gitlab_projects: list[str]
    github_repos: list[str]
    bitbucket_repos: list[str]
    profiler_template: Path
    profiler_out: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    pipeline_log: Path = field(default_factory=Path)
    state: "StateStore | None" = None
    run_id: int = 0
    generation: int = 1

    def repo_log_dir(self, entry: RepoEntry) -> Path:
        return self.logs_dir / entry.platform / entry.batch_org / entry.short_name

    def clone_path(self, entry: RepoEntry) -> Path:
        return self.clones_dir / entry.platform / entry.batch_org / entry.short_name


class PipelineLogger:
    """Full detail always goes to master_log. Console output is INFO-level
    (every line) by default; --quiet raises the console handler to WARNING
    so a CI log shows only the start line, the final summary, errors, and
    where to find failures/resume -- everything else still lands in the
    file, nothing is lost, stdout just stops being a mirror of it.
    """

    def __init__(self, master_log: Path, quiet: bool = False) -> None:
        self.master_log = master_log
        self._quiet = quiet
        master_log.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("org_analyser")
        self.logger = self._logger  # public handle, e.g. for pipeline.progress's handler swap
        self._logger.setLevel(logging.INFO)
        if not self._logger.handlers:
            fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            fh = logging.FileHandler(master_log, encoding="utf-8")
            fh.setFormatter(fmt)
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            sh.setLevel(logging.WARNING if quiet else logging.INFO)
            self._logger.addHandler(fh)
            self._logger.addHandler(sh)

    def info(self, msg: str) -> None:
        self._logger.info(msg)

    def important(self, msg: str) -> None:
        """Like info(), but always reaches stdout even under --quiet --
        reserved for the handful of lines someone watching a CI log
        actually needs: start, final summary, failures/resume pointers."""
        self._logger.info(msg)
        if self._quiet:
            print(msg)

    def error(self, msg: str) -> None:
        self._logger.error(msg)

    def phase_log(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def parse_tokens_file(path: Path) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        tokens[key.strip()] = value.strip()
    return tokens


def resolve_tokens(tokens_file: str | None) -> dict[str, str]:
    """Tokens come from config.yml's `tokens:` mapping by default. Pass
    --tokens-file to use a separate key=value file instead (back-compat)."""
    if tokens_file:
        path = Path(tokens_file)
        if not path.is_file():
            raise SystemExit(f"tokens file not found: {path}")
        return parse_tokens_file(path)
    # Drop null/blank entries so `tokens.get(name, "")` is empty for unset keys
    # -- YAML `null` would otherwise stringify to the truthy literal "None".
    return {
        str(k): str(v).strip()
        for k, v in (CONFIG.get("tokens") or {}).items()
        if v is not None and str(v).strip()
    }


def azure_configured() -> bool:
    """True when Azure AI Foundry / Azure OpenAI is set up in the environment.

    In this mode the LLM clients (llm_safety.safe_openai, quality_evaluator)
    authenticate against Azure and no OpenAI key is required.
    """
    return bool(
        os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
        and os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
    )


def resolve_openai_key(tokens: dict[str, str]) -> str | None:
    # config.yml is the single source of truth: an ambient OPENAI_API_KEY in
    # the shell is deliberately ignored, never consulted here.
    for name in OPENAI_TOKEN_NAMES:
        val = tokens.get(name, "").strip()
        if val:
            return val
    return None


_GITHUB_REMOTE_RE = re.compile(
    r"(?:https?://(?:[^/@]+@)?|git@)github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+(?:\.git)?)",
    re.I,
)
_GITLAB_REMOTE_RE = re.compile(
    r"https?://(?:[^/@]+@)?(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?/?$",
    re.I,
)


def load_repos_manifest(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"repos manifest must be a JSON object: {path}")
    return {str(k): str(v) for k, v in data.items()}


def normalize_github_repos(values: list[str]) -> list[str]:
    repos: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            repo = part.strip().strip("/")
            if not repo or repo in seen:
                continue
            seen.add(repo)
            repos.append(repo)
    if not repos:
        raise ValueError("At least one --github-repo path is required")
    for repo in repos:
        if "/" not in repo:
            raise ValueError(
                f"GitHub repo path must be owner/repo (got {repo!r})"
            )
    return repos


def normalize_gitlab_projects(values: list[str]) -> list[str]:
    projects: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            project = part.strip().strip("/")
            if not project or project in seen:
                continue
            seen.add(project)
            projects.append(project)
    if not projects:
        raise ValueError("At least one --gitlab-project path is required")
    for project in projects:
        if "/" not in project:
            raise ValueError(
                f"GitLab project path must be group/project (got {project!r})"
            )
    return projects


def normalize_bitbucket_repos(values: list[str]) -> list[str]:
    repos: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            repo = part.strip().strip("/")
            if not repo or repo in seen:
                continue
            seen.add(repo)
            repos.append(repo)
    if not repos:
        raise ValueError("At least one --bitbucket-repo path is required")
    for repo in repos:
        if "/" not in repo:
            raise ValueError(
                f"Bitbucket repo path must be workspace/repo (got {repo!r})"
            )
    return repos


def parse_git_remote(repo_path: Path) -> tuple[str, str] | None:
    """Return (platform, full_name) from origin remote, if parseable."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    if not url:
        return None

    m = _GITHUB_REMOTE_RE.search(url)
    if m:
        owner = m.group("owner")
        repo = m.group("repo").removesuffix(".git")
        return "github", f"{owner}/{repo}"

    m = _GITLAB_REMOTE_RE.search(url)
    if m and "gitlab" in m.group("host").lower():
        full = m.group("path").removesuffix(".git")
        return "gitlab", full
    return None


def resolve_remote_ref(entry: RepoEntry, ctx: RunContext) -> tuple[str, str] | None:
    """Map a local folder entry to (platform, full_name) for optional API-backed phases."""
    # local-only: never resolve a remote, even if the checkout has an `origin`.
    # This keeps the PR-based phases skipped and eval-kit in local mode, instead
    # of trying (and failing) to hit GitHub/GitLab without a token.
    if ctx.local_only:
        return None
    manifest_ref = ctx.repos_manifest.get(entry.short_name, "").strip()
    if manifest_ref:
        if manifest_ref.startswith("gitlab:"):
            return "gitlab", manifest_ref.removeprefix("gitlab:").strip()
        if manifest_ref.startswith("github:"):
            return "github", manifest_ref.removeprefix("github:").strip()
        if "/" in manifest_ref:
            return "github", manifest_ref
        return None
    if entry.local_path:
        return parse_git_remote(entry.local_path)
    return None


def discover_local_repos(ctx: RunContext, log: PipelineLogger) -> list[RepoEntry]:
    root = ctx.local_repos_dir
    if not root or not root.is_dir():
        raise RuntimeError(f"Local repos directory not found: {root}")

    entries: list[RepoEntry] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        full_name = ctx.repos_manifest.get(child.name) or f"local/{child.name}"
        entries.append(
            RepoEntry(
                platform="local",
                full_name=full_name,
                org=ctx.target,
                batch_org=ctx.target,
                local_path=child.resolve(),
            )
        )
    if not entries:
        raise RuntimeError(f"No repo subdirectories found under {root}")
    log.info(f"Discovered {len(entries)} local repos under {root}")
    return entries


def check_tool(name: str) -> bool:
    return shutil.which(name) is not None


def preflight(ctx: RunContext, log: PipelineLogger) -> None:
    errors: list[str] = []

    try:
        proc = subprocess.run(
            [PYTHON_BIN, "-c", "import sys; assert sys.version_info >= (3, 10)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            errors.append(f"Python 3.10+ required (using {PYTHON_BIN})")
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"Cannot run Python interpreter {PYTHON_BIN}: {exc}")

    # config.yml (its `tokens:` mapping, or --tokens-file) is the single source
    # of truth for every credential. The configured value always wins over any
    # ambient env var, and when config.yml omits a credential its env var is
    # cleared so a stray shell value can never leak in. Written to os.environ
    # here so validation and every child process (which inherit it) agree.
    for name in AZURE_TOKEN_NAMES:
        val = ctx.tokens.get(name, "").strip()
        if val:
            os.environ[name] = val
        else:
            os.environ.pop(name, None)

    openai_key = resolve_openai_key(ctx.tokens)
    if openai_key:
        os.environ["OPENAI_API_KEY"] = openai_key
    else:
        os.environ.pop("OPENAI_API_KEY", None)
        if not azure_configured() and not ctx.local_only:
            errors.append(
                "Missing LLM credentials: set openai_key in config.yml's tokens, "
                "or configure Azure with AZURE_OPENAI_ENDPOINT + "
                "AZURE_OPENAI_API_KEY there"
            )

    # Only needed for the gemini PR-rubrics path; not a hard requirement
    # otherwise, so this is a warning, not an error.
    gemini_key = ctx.tokens.get(GEMINI_TOKEN_NAME, "").strip()
    if gemini_key:
        os.environ["GEMINI_API_KEY"] = gemini_key
    else:
        os.environ.pop("GEMINI_API_KEY", None)
    if ctx.pr_rubrics_provider == "gemini" and not os.environ.get("GEMINI_API_KEY", "").strip():
        log.error(
            f"--pr-rubrics-provider=gemini but no GEMINI_API_KEY is set "
            f"(or {GEMINI_TOKEN_NAME}= in the tokens file)"
        )

    if ctx.platform in ("github", "github-repo"):
        if not ctx.tokens.get(ctx.github_token_name):
            errors.append(f"Missing {ctx.github_token_name} in tokens file")
        if ctx.platform == "github-repo":
            for repo in ctx.github_repos:
                if "/" not in repo:
                    errors.append(
                        f"GitHub repo path must be owner/repo (got {repo!r})"
                    )
    elif ctx.platform in ("bitbucket", "bitbucket-repo"):
        # A token is optional for public repos (anonymous access, lower rate
        # limit). Listing a whole workspace does need one, though.
        if ctx.platform == "bitbucket" and not ctx.tokens.get(BITBUCKET_TOKEN_NAME):
            errors.append(
                f"Missing {BITBUCKET_TOKEN_NAME} in tokens file "
                "(required to list a workspace; a single public --bitbucket-repo can omit it)"
            )
        if ctx.platform == "bitbucket-repo":
            for repo in ctx.bitbucket_repos:
                if "/" not in repo:
                    errors.append(
                        f"Bitbucket repo path must be workspace/repo (got {repo!r})"
                    )
    elif ctx.platform in ("gitlab", "gitlab-project"):
        if not ctx.tokens.get(GITLAB_TOKEN_NAME):
            errors.append(f"Missing {GITLAB_TOKEN_NAME} in tokens file")
        if ctx.platform == "gitlab-project":
            for project in ctx.gitlab_projects:
                if "/" not in project:
                    errors.append(
                        f"GitLab project path must be group/project (got {project!r})"
                    )
    elif ctx.platform == "local":
        if not ctx.local_repos_dir or not ctx.local_repos_dir.is_dir():
            errors.append(f"Local repos directory not found: {ctx.local_repos_dir}")
        needs_github = any(
            ref.startswith("github:") or ("/" in ref and not ref.startswith("gitlab:"))
            for ref in ctx.repos_manifest.values()
        )
        needs_gitlab = any(ref.startswith("gitlab:") for ref in ctx.repos_manifest.values())
        if needs_github and not ctx.tokens.get(ctx.github_token_name):
            errors.append(
                f"repos manifest includes GitHub repos but {ctx.github_token_name} is missing"
            )
        if needs_gitlab and not ctx.tokens.get(GITLAB_TOKEN_NAME):
            errors.append("repos manifest includes GitLab repos but gitlab_token is missing")

    if not check_tool("git"):
        errors.append("git is not on PATH")

    for tool in ("scc", "node"):
        if not check_tool(tool):
            errors.append(f"{tool} is not on PATH (required by codebase_profiler)")

    if not ctx.profiler_template.is_file():
        errors.append(f"Profiler template not found: {ctx.profiler_template}")

    if not (EVAL_ROOT / "repo_evaluator.py").is_file():
        errors.append(f"eval-kit not found: {EVAL_ROOT / 'repo_evaluator.py'}")

    if ctx.include_quality_score:
        if not (QUALITY_SKILL / "scripts" / "score.py").is_file():
            errors.append(f"repo-quality-score scripts not found under {QUALITY_SKILL}")

    try:
        import openpyxl  # noqa: F401
    except ImportError:
        errors.append("openpyxl not installed (pip install -e .)")

    for pkg in ("requests", "openai"):
        try:
            __import__(pkg)
        except ImportError:
            errors.append(f"{pkg} not installed (pip install -e .)")

    if errors:
        for err in errors:
            log.error(f"Preflight failed: {err}")
        raise SystemExit(1)

    log.info("Preflight OK")


def discover_repos(ctx: RunContext, log: PipelineLogger) -> list[RepoEntry]:
    if ctx.platform == "local":
        return discover_local_repos(ctx, log)
    if ctx.platform == "github":
        token = ctx.tokens[ctx.github_token_name]
        names = list_github_repos(token, ctx.target, ctx.github_host)
        entries = [
            RepoEntry("github", name, ctx.target, ctx.target) for name in names
        ]
    elif ctx.platform == "github-repo":
        entries = []
        for repo in ctx.github_repos:
            owner = repo.split("/")[0]
            entries.append(RepoEntry("github", repo, owner, owner))
    elif ctx.platform == "bitbucket":
        token = ctx.tokens[BITBUCKET_TOKEN_NAME]
        names = list_bitbucket_repos(token, ctx.target, ctx.tokens.get(BITBUCKET_USERNAME_NAME, ""))
        entries = [
            RepoEntry("bitbucket", name, ctx.target, ctx.target) for name in names
        ]
    elif ctx.platform == "bitbucket-repo":
        entries = []
        for repo in ctx.bitbucket_repos:
            workspace = repo.split("/")[0]
            entries.append(RepoEntry("bitbucket", repo, workspace, workspace))
    elif ctx.platform == "gitlab-project":
        entries = []
        for project in ctx.gitlab_projects:
            namespace = "/".join(project.split("/")[:-1])
            entries.append(RepoEntry("gitlab", project, namespace, namespace))
    else:
        token = ctx.tokens[GITLAB_TOKEN_NAME]
        names = list_gitlab_projects(token, ctx.target, ctx.gitlab_host)
        entries = [
            RepoEntry("gitlab", name, ctx.target, ctx.target) for name in names
        ]
    log.info(f"Discovered {len(entries)} repos for {ctx.platform}:{ctx.target}")
    return entries


def run_merged_pr_counts(ctx: RunContext, log: PipelineLogger) -> dict[str, Any]:
    log.info("Phase 1: merged PR counts (always refetch)")
    ctx.merged_pr_dir.mkdir(parents=True, exist_ok=True)

    # Per-repo progress ("[10/13] latest=... count=0") is verbose and would
    # otherwise print raw to stdout, fighting the rich UI's live rendering
    # and cluttering plain-log runs alike. Captured here and written to the
    # phase log file instead -- same treatment pr-task-profile's subprocess
    # output already gets.
    progress_lines: list[str] = []
    progress_cb = progress_lines.append

    if ctx.platform == "github":
        summary = export_github_org(
            ctx.tokens[ctx.github_token_name],
            ctx.target,
            ctx.github_token_name,
            ctx.merged_pr_dir,
            ctx.github_host,
            progress_cb,
        )
    elif ctx.platform == "github-repo":
        summary = export_github_repos(
            ctx.tokens[ctx.github_token_name],
            ctx.github_repos,
            ctx.github_token_name,
            ctx.merged_pr_dir,
            ctx.github_host,
            progress_cb,
        )
    elif ctx.platform == "bitbucket":
        summary = export_bitbucket_workspace(
            ctx.tokens.get(BITBUCKET_TOKEN_NAME, ""),
            ctx.target,
            BITBUCKET_TOKEN_NAME,
            ctx.merged_pr_dir,
            ctx.tokens.get(BITBUCKET_USERNAME_NAME, ""),
            progress_cb,
        )
    elif ctx.platform == "bitbucket-repo":
        summary = export_bitbucket_repos(
            ctx.tokens.get(BITBUCKET_TOKEN_NAME, ""),
            ctx.bitbucket_repos,
            BITBUCKET_TOKEN_NAME,
            ctx.merged_pr_dir,
            ctx.tokens.get(BITBUCKET_USERNAME_NAME, ""),
            progress_cb,
        )
    elif ctx.platform == "gitlab-project":
        summary = export_gitlab_projects(
            ctx.tokens[GITLAB_TOKEN_NAME],
            ctx.gitlab_projects,
            GITLAB_TOKEN_NAME,
            ctx.merged_pr_dir,
            ctx.gitlab_host,
            progress_cb,
        )
    else:
        summary = export_gitlab_group(
            ctx.tokens[GITLAB_TOKEN_NAME],
            ctx.target,
            GITLAB_TOKEN_NAME,
            ctx.merged_pr_dir,
            ctx.gitlab_host,
            progress_cb,
        )

    if progress_lines:
        log.phase_log(ctx.logs_dir / "merged-pr-counts.log", "\n".join(progress_lines))

    summary_path = ctx.merged_pr_dir / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerow({k: summary.get(k, "") for k in SUMMARY_FIELDS})

    pr_manifest = {
        "phase": "merged-pr-counts",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "csv_fields": CSV_FIELDS,
    }
    (ctx.merged_pr_dir / "manifest.json").write_text(
        json.dumps(pr_manifest, indent=2), encoding="utf-8"
    )
    log.info(
        f"Merged PR counts done: {summary.get('repos_total', 0)} repos, "
        f"{summary.get('merged_total', 0)} total merged"
    )
    return summary


def clone_url(entry: RepoEntry, tokens: dict[str, str], ctx: RunContext) -> str:
    if entry.platform == "github":
        host = ctx.github_host
        if host == "github.com":
            return f"https://github.com/{entry.full_name}.git"
        return f"https://{host}/{entry.full_name}.git"
    if entry.platform == "bitbucket":
        return f"https://bitbucket.org/{entry.full_name}.git"
    host = ctx.gitlab_host.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/{entry.full_name}.git"


def copy_local_repo(entry: RepoEntry, ctx: RunContext) -> tuple[bool, str, Path | None]:
    """Local repos are never analysed in the user's actual checkout -- copied
    into the disposable clones area first, exactly like a remote clone, so
    the redact phase (which rewrites files in place) can never touch the
    user's real source tree."""
    src = entry.local_path
    dest = ctx.clone_path(entry)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dest, symlinks=True)
    except OSError as exc:
        return False, f"copy of local checkout failed: {exc}", None
    return True, f"copied local checkout {src} -> {dest}", dest


def fresh_clone(entry: RepoEntry, ctx: RunContext) -> tuple[bool, str, Path | None]:
    dest = ctx.clone_path(entry)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", *git_longpath_config(), "clone", "--recurse-submodules=0"]
    if ctx.clone_depth:
        cmd.extend(["--depth", str(ctx.clone_depth)])
    cmd.extend([clone_url(entry, ctx.tokens, ctx), str(dest)])

    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    # Auth via git's env-config channel. GIT_ASKPASS_OVERRIDE and
    # GIT_HTTP_EXTRAHEADER (used here previously) are not git variables at all
    # -- git never read them, so private clones were silently falling back to
    # whatever ambient credential helper the host happened to have.
    # GIT_CONFIG_COUNT/KEY/VALUE is the real mechanism, and unlike `-c` it keeps
    # the token off argv.
    token = ""
    user = "x-access-token"  # ignored by GitHub/GitLab; must be non-empty
    if entry.platform == "github":
        token = ctx.tokens.get(ctx.github_token_name, "")
    elif entry.platform == "gitlab":
        token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
    elif entry.platform == "bitbucket":
        token = ctx.tokens.get(BITBUCKET_TOKEN_NAME, "")
        bb_user = ctx.tokens.get(BITBUCKET_USERNAME_NAME, "").strip()
        # Git-over-HTTPS username differs from the REST API username -- see
        # resolve_bitbucket_git_auth's docstring (an Atlassian API token needs
        # the static "x-bitbucket-api-token-auth" sentinel here, unlike REST).
        user = resolve_bitbucket_git_auth(token, bb_user)
    if token:
        auth = base64.b64encode(f"{user}:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {auth}"

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return False, "clone timeout", None
    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "")[-800:]
        # scrub_secrets ignores empty/short strings; a bare str.replace("", ...)
        # on a missing token would splice the marker between every character.
        err_msg = scrub_secrets(
            err_msg,
            ctx.tokens.get(ctx.github_token_name, ""),
            ctx.tokens.get(GITLAB_TOKEN_NAME, ""),
        )
        return False, err_msg, None

    verify = subprocess.run(
        ["git", *git_longpath_config(), "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if verify.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return False, "clone verify failed", None
    return True, "cloned", dest


def extract_json(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text
    try:
        _, end = json.JSONDecoder().raw_decode(text, start)
        return text[start:end]
    except json.JSONDecodeError:
        return text


def run_py(
    script: Path,
    args: list[str],
    timeout: int = 900,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a Python script; return (exit_code, stdout, stderr).

    Pass secrets through extra_env, never through args: argv is world-readable
    via `ps` and /proc/<pid>/cmdline.
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            [PYTHON_BIN, str(script), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def run_module(
    module: str,
    args: list[str],
    timeout: int = 900,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    stream_log_path: Path | None = None,
    scrub_values: tuple[str, ...] = (),
) -> tuple[int, str, str]:
    """Run `python -m <module>` (resolves via cwd being on sys.path, no install required).

    Pass secrets through extra_env, never through args: argv is world-readable
    via `ps` and /proc/<pid>/cmdline.

    stream_log_path switches from buffer-then-return to line-by-line
    write-through: long phases (eval-kit, pr-task-profile) become `tail -f`-able
    while they run instead of dumping output only after exit. Lines are
    scrubbed of scrub_values before hitting disk, since this path bypasses the
    scrub that record() applies to buffered detail. Return contract unchanged
    either way: (returncode, stdout, stderr), stderr == "timeout" on timeout.
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    cmd = [PYTHON_BIN, "-m", module, *args]

    if stream_log_path is None:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return 1, "", "timeout"
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    stream_log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    sink_lock = threading.Lock()
    buffers: dict[str, list[str]] = {"stdout": [], "stderr": []}
    timed_out = False

    with stream_log_path.open("w", encoding="utf-8") as sink:

        def pump(pipe: Any, name: str) -> None:
            for line in iter(pipe.readline, ""):
                buffers[name].append(line)
                with sink_lock:
                    sink.write(scrub_secrets(line, *scrub_values))
                    sink.flush()
            pipe.close()

        readers = [
            threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True),
        ]
        for t in readers:
            t.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
        for t in readers:
            t.join(timeout=10)
        if timed_out:
            with sink_lock:
                sink.write(f"\n[org-analyser] killed after exceeding {timeout}s timeout\n")

    if timed_out:
        return 1, "".join(buffers["stdout"]), "timeout"
    return proc.returncode, "".join(buffers["stdout"]), "".join(buffers["stderr"])


def stdout_json(stdout: str) -> str:
    text = stdout.lstrip()
    if text.startswith("{"):
        return extract_json(stdout)
    return stdout


def run_py_message(
    script: Path,
    args: list[str],
    timeout: int = 900,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """Like run_py but merges stdout/stderr for error reporting."""
    code, out, err = run_py(script, args, timeout=timeout, cwd=cwd)
    if code == 0 and out.lstrip().startswith("{"):
        out = stdout_json(out)
    combined = out + (f"\n{err}" if err else "")
    return code, combined


def with_retries(
    fn: Callable[[], tuple[bool, str]],
    retries: int,
    phase: str,
) -> tuple[bool, str, int]:
    last_err = ""
    attempts = 0
    for attempt in range(1, retries + 1):
        attempts = attempt
        ok, msg = fn()
        if ok:
            return True, msg, attempts
        last_err = msg
        if attempt < retries:
            time.sleep(min(30, 2 ** attempt))
    return False, last_err, attempts


def run_redact(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    """Scrub secrets from the working tree in place, right after clone and
    before any phase below reads the tree. A hard gate: process_repo refuses
    to run codebase-profiler/repo-analyzer/eval-kit/repo-quality-score unless
    this phase is 'ok' in the state DB for this repo."""
    try:
        report = redact_working_tree(clone_path)
        out_dir = ctx.repo_log_dir(entry)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_redaction_report(report, out_dir / "redaction_report.json")
        total_secrets = sum(report.get("secrets_by_type", {}).values())
        detail = (
            f"redact ok: files_scanned={report['files_scanned']} "
            f"files_modified={report['files_modified']} "
            f"key_files_dropped={report['files_dropped']} "
            f"secrets_redacted={total_secrets} by_type={report['secrets_by_type']}"
        )
        if report["errors"]:
            detail += f" errors={report['errors'][:5]}"
        return True, detail
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


def run_profiler(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    try:
        from profiler.providers import make_provider  # noqa: WPS433
        from profiler.runner import profile_dataset
        from profiler.writer import append_row

        remote_ref = resolve_remote_ref(entry, ctx)
        if not remote_ref and entry.platform == "github":
            remote_ref = ("github", entry.full_name)
        elif not remote_ref and entry.platform == "gitlab":
            remote_ref = ("gitlab", entry.full_name)

        provider = remote = None
        remote_note = ""
        if remote_ref:
            platform, full_name = remote_ref
            if platform == "github":
                token = ctx.tokens.get(ctx.github_token_name, "")
                owner, name = full_name.split("/", 1)
                provider = make_provider("github", token=token, host=ctx.github_host)
            else:
                token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
                parts = full_name.split("/")
                owner, name = "/".join(parts[:-1]), parts[-1]
                provider = make_provider("gitlab", token=token, host=ctx.gitlab_host)
            try:
                remote = provider.get_repo(owner, name)
            except Exception as exc:
                # Remote metadata (PR/fork stats) is optional. A transient API
                # failure (e.g. GitHub 503) must NOT wipe out the whole profile —
                # fall back to analysing the local clone so we still emit a row.
                provider = remote = None
                remote_note = f" (remote metadata skipped: {type(exc).__name__}: {exc})"

        result = profile_dataset(
            str(clone_path),
            use_github=True,
            provider=provider,
            remote=remote,
            originating_company=entry.org,
            repo_name=entry.repo_slug,
        )
        with _PROFILER_WRITE_LOCK:
            append_row(result, template=str(ctx.profiler_template), out=str(ctx.profiler_out))
        return True, f"profiler ok ({len(result.values)} fields){remote_note}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


def run_eval_kit(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    out_dir = ctx.eval_kit_dir / entry.batch_org / entry.short_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"{entry.short_name}.json"

    # The token goes to the child via REPO_EVAL_TOKEN, never on argv.
    token = ""
    remote_ref = resolve_remote_ref(entry, ctx)
    if remote_ref:
        platform, full_name = remote_ref
        if platform == "github":
            token = ctx.tokens.get(ctx.github_token_name, "")
            repo_arg = full_name
        else:
            token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
            repo_arg = f"gitlab:{full_name}"
    elif entry.is_local:
        platform, repo_arg = "local", str(clone_path)
    elif entry.platform == "github":
        platform = "github"
        token = ctx.tokens[ctx.github_token_name]
        repo_arg = entry.full_name
    elif entry.platform == "bitbucket":
        platform = "bitbucket"
        token = ctx.tokens.get(BITBUCKET_TOKEN_NAME, "")
        repo_arg = f"bitbucket:{entry.full_name}"
    else:
        platform = "gitlab"
        token = ctx.tokens[GITLAB_TOKEN_NAME]
        repo_arg = f"gitlab:{entry.full_name}"

    args = [repo_arg, "--platform", platform, "--json", "--output", str(out_json)]
    if platform != "local":
        args.extend(["--repo-path", str(clone_path)])
    if ctx.skip_f2p:
        args.append("--skip-f2p")
    if ctx.local_only:
        # --local-only promises no repo content reaches an LLM provider; the
        # quality checks and PR-rubrics scoring both call out to OpenAI/Gemini.
        args.append("--skip-quality-llm")
        args.append("--skip-pr-rubrics")
    args.extend(["--pr-rubrics-provider", ctx.pr_rubrics_provider])
    # No longer forcing sync: auto is already the evaluator's own default,
    # so large candidate sets route to the OpenAI Batch API like everywhere
    # else in the shared broker. This reopens the trade-off the old sync
    # override existed to avoid: repo_pool workers running run_eval_kit can
    # now sit on one repo's batch job for up to the timeout below instead of
    # being killed after 2h, so a run with more large repos than workers can
    # have the pool pinned for a long time. Accepted trade-off: correctness
    # (don't kill a valid batch) over per-repo throughput.
    args.extend(["--taxonomy-llm-mode", "auto"])
    args.extend(["--rubrics-llm-mode", "auto"])

    extra_env: dict[str, str] = {}
    if token:
        extra_env["REPO_EVAL_TOKEN"] = token
    if platform == "bitbucket":
        bb_user = ctx.tokens.get(BITBUCKET_USERNAME_NAME, "").strip()
        if bb_user:
            extra_env["BITBUCKET_USERNAME"] = bb_user

    code, out, err = run_module(
        "eval.repo_evaluator",
        args,
        extra_env=extra_env or None,
        # Eval-kit can run up to three sequential Batch API jobs (rubrics
        # inference, rubrics scoring, taxonomy), each with a 24-hour
        # completion window. Do not kill a valid batch before it completes.
        # A failed batch call falls back to live sync for the whole item
        # set (llm.batch.run_batch_or_sync), which for a very large repo
        # could itself run long and push wall-clock past this budget; the
        # 5-minute buffer only covers clean completion, not that fallback.
        timeout=3 * 24 * 60 * 60 + 300,
        cwd=CODING,
        # The slowest, most opaque phase: stream its output live so a run on a
        # 1000+-PR repo can be watched with `tail -f` instead of looking hung.
        # (<phase>.log itself is overwritten with the summary by record() at
        # the end, hence the separate .stream.log.)
        stream_log_path=ctx.repo_log_dir(entry) / "eval-kit.stream.log",
        scrub_values=tuple(t for t in (token,) if t),
    )
    if code != 0:
        return False, (out + err)[-2000:]
    return True, f"eval-kit ok -> {out_json}"


def _known_merged_count(entry: RepoEntry, ctx: RunContext) -> int | None:
    """The real merged-PR count from the platform API, for repo-analyzer's
    --known-merged-count -- its own git-history heuristic (--provider local)
    is a floor estimate that can be wildly off (squash/rebase merges leave
    no trace; long-lived branches merged outside any PR/MR inflate it).
    One cheap, already-proven-safe API call (same pattern codebase-profiler
    uses for its own PR-count column). Returns None (no override) for local
    checkouts or on any lookup failure -- the heuristic stays as a fallback."""
    if entry.is_local:
        return None
    try:
        if entry.platform == "github":
            token = ctx.tokens.get(ctx.github_token_name, "")
            if not token:
                return None
            return count_github_merged(token, entry.full_name, ctx.github_host, None, None)
        if entry.platform == "gitlab":
            token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
            if not token:
                return None
            return count_gitlab_merged(token, entry.full_name, ctx.gitlab_host, None, None)
        if entry.platform == "bitbucket":
            token = ctx.tokens.get(BITBUCKET_TOKEN_NAME, "")
            if not token:
                return None
            username = ctx.tokens.get(BITBUCKET_USERNAME_NAME, "")
            return count_bitbucket_merged(token, entry.full_name, username, None, None)
    except Exception:
        return None
    return None


def run_repo_analyzer(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    """LLM-usage / training-data-quality / CI report, run against the local
    clone (no extra clone -- same clone the other phases already use)."""
    out_dir = ctx.repo_analyzer_dir / entry.batch_org / entry.short_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / f"{entry.short_name}.csv"

    args = [
        "--provider", "local",
        "--path", str(clone_path),
        "--name", entry.full_name,
        "--output", str(out_csv),
    ]
    known_merged = _known_merged_count(entry, ctx)
    if known_merged is not None:
        args += ["--known-merged-count", str(known_merged)]
    code, out, err = run_module(
        "analysis.repo_analyzer",
        args,
        timeout=3600,
        cwd=CODING,
    )
    if code != 0:
        return False, (out + err)[-2000:]
    return True, f"repo-analyzer ok -> {out_csv}"


def run_quality_score(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    from quality.agent_scorer import (  # noqa: WPS433
        build_scores_json,
        scores_for_seal,
        write_scoring_notes,
    )

    work_dir = ctx.quality_dir / "work" / entry.batch_org / entry.short_name
    repos_dir = ctx.quality_dir / "repos"
    sealed_path = repos_dir / f"{entry.short_name}.sealed.json"
    work_dir.mkdir(parents=True, exist_ok=True)
    repos_dir.mkdir(parents=True, exist_ok=True)

    for script, out_name in [
        (QUALITY_SKILL / "scripts" / "repo_stats.py", "repo_stats.json"),
        (QUALITY_SKILL / "scripts" / "git_stats.py", "git_stats.json"),
    ]:
        code, out, err = run_py(script, [str(clone_path)], timeout=900)
        if code != 0:
            return False, f"{out_name} failed: {(out + err)[-500:]}"
        (work_dir / out_name).write_text(stdout_json(out), encoding="utf-8")

    code, out, err = run_py(
        QUALITY_SKILL / "scripts" / "classify_repo.py",
        [
            str(work_dir / "repo_stats.json"),
            "--git-stats",
            str(work_dir / "git_stats.json"),
        ],
    )
    if code != 0:
        return False, f"classify failed: {(out + err)[-500:]}"
    (work_dir / "classify.json").write_text(stdout_json(out), encoding="utf-8")

    repo_stats = json.loads((work_dir / "repo_stats.json").read_text())
    git_stats = json.loads((work_dir / "git_stats.json").read_text())
    classify = json.loads((work_dir / "classify.json").read_text())

    scores = build_scores_json(repo_stats, git_stats, classify, str(clone_path))
    write_scoring_notes(work_dir, scores)
    (work_dir / "scores.json").write_text(
        json.dumps(scores_for_seal(scores), indent=2),
        encoding="utf-8",
    )

    code, out, err = run_py(
        QUALITY_SKILL / "scripts" / "score.py",
        [
            str(work_dir / "scores.json"),
            "--report",
            str(sealed_path),
            "--evidence",
            str(work_dir / "repo_stats.json"),
            "--evidence",
            str(work_dir / "git_stats.json"),
        ],
    )
    if code != 0:
        return False, f"seal failed: {(out + err)[-500:]}"
    return True, f"sealed -> {sealed_path}"


def merged_pr_totals(merged_pr_dir: Path) -> dict[str, int]:
    """repo full-name -> merged-PR count, from phase 1's CSVs. Used as the
    denominator for eval-kit sub-progress (PRs scanned so far vs total).
    Empty until merged-pr-counts has written its files (or local mode)."""
    totals: dict[str, int] = {}
    if not merged_pr_dir.is_dir():
        return totals
    for csv_path in merged_pr_dir.glob("*.csv"):
        if csv_path.name == "summary.csv":
            continue
        try:
            with csv_path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    repo = (row.get("repo") or "").strip()
                    org = (row.get("org") or "").strip()
                    if not repo:
                        continue
                    full = repo if "/" in repo else f"{org}/{repo}"
                    try:
                        totals[full] = int(row.get("merged_count") or 0)
                    except ValueError:
                        continue
        except OSError:
            continue
    return totals


def batch_state_summary(batch_state_dir: Path) -> str | None:
    """One-line rollup of llm.batch's state sidecars for the progress UI.

    The Batch API wait happens inside the pr-task-profile subprocess, which
    the parent can't see into -- but llm.batch persists every stage/count
    transition to *.state.json precisely so any process can reconstruct where
    each batch stands. None when no sidecars exist (sync path or no LLM work).
    """
    states: list[dict[str, Any]] = []
    if batch_state_dir.is_dir():
        for p in sorted(batch_state_dir.glob("**/*.state.json")):
            try:
                states.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    if not states:
        return None
    by_status: dict[str, int] = {}
    done = total = 0
    for s in states:
        by_status[s.get("status", "?")] = by_status.get(s.get("status", "?"), 0) + 1
        counts = s.get("request_counts") or {}
        done += counts.get("completed", 0) + counts.get("failed", 0)
        total += counts.get("total", 0) or s.get("request_count", 0)
    parts = ", ".join(f"{n} {status}" for status, n in sorted(by_status.items()))
    if total:
        return f"batch: {parts} [{done}/{total}]"
    return f"batch: {parts}"


def run_pr_task_profile(
    ctx: RunContext,
    log: PipelineLogger,
    entries: list[RepoEntry],
) -> dict[str, Any]:
    log.info("Phase: PR task-profile report (rules + LLM)")
    ctx.task_profile_dir.mkdir(parents=True, exist_ok=True)

    extra_env: dict[str, str] = {}
    gh_token = ctx.tokens.get(ctx.github_token_name, "")
    gl_token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
    bb_token = ctx.tokens.get(BITBUCKET_TOKEN_NAME, "")
    bb_user = ctx.tokens.get(BITBUCKET_USERNAME_NAME, "")
    if gh_token:
        extra_env["GITHUB_TOKEN"] = gh_token
    if gl_token:
        extra_env["GITLAB_TOKEN"] = gl_token
    if bb_token:
        extra_env["BITBUCKET_TOKEN"] = bb_token
    if bb_user:
        extra_env["BITBUCKET_USERNAME"] = bb_user

    args = [
        "--output-dir",
        str(ctx.task_profile_dir),
        "--max-workers",
        str(min(ctx.workers, 6)),
    ]

    if ctx.platform == "github":
        args.extend(["--org", ctx.target])
    elif ctx.platform == "github-repo":
        for repo in ctx.github_repos:
            args.extend(["--repo", repo])
    elif ctx.platform in ("bitbucket", "bitbucket-repo"):
        # pr_task_profile doesn't expand workspaces itself, so pass each
        # already-discovered repo.
        for entry in entries:
            args.extend(["--bitbucket-repo", entry.full_name])
    elif ctx.platform == "gitlab":
        args.extend(["--gitlab-group", ctx.target])
    elif ctx.platform == "gitlab-project":
        for project in ctx.gitlab_projects:
            args.extend(["--gitlab-project", project])
    else:
        github_repos: list[str] = []
        gitlab_projects: list[str] = []
        for entry in entries:
            ref = resolve_remote_ref(entry, ctx)
            if not ref:
                continue
            platform, full_name = ref
            if platform == "github":
                github_repos.append(full_name)
            else:
                gitlab_projects.append(full_name)
        if not github_repos and not gitlab_projects:
            log.info("PR task-profile skipped (local mode, no remote repo mapping)")
            return {"skipped": True, "reason": "local mode without remote mapping"}
        if github_repos and not gh_token:
            return {"ok": False, "error": "github token required for PR task-profile"}
        if gitlab_projects and not gl_token:
            return {"ok": False, "error": "gitlab token required for PR task-profile"}
        for repo in sorted(set(github_repos)):
            args.extend(["--repo", repo])
        for project in sorted(set(gitlab_projects)):
            args.extend(["--gitlab-project", project])

    code, out, err = run_module(
        "analysis.pr_task_profile",
        args,
        timeout=86400,
        cwd=CODING,
        extra_env=extra_env or None,
        # Runs last and can take hours on active orgs: stream output live so
        # progress is `tail -f`-able. pr-task-profile.log still gets the final
        # (truncated) copy below, preserving the existing behaviour.
        stream_log_path=ctx.logs_dir / "pr-task-profile.stream.log",
        scrub_values=tuple(t for t in (gh_token, gl_token, bb_token) if t),
    )
    if code != 0 and err == "timeout":
        log.phase_log(ctx.logs_dir / "pr-task-profile.log", "timeout after 24h")
        return {"ok": False, "error": "timeout"}

    detail = (out or "") + (err or "")
    log.phase_log(ctx.logs_dir / "pr-task-profile.log", detail[-50000:])

    if code != 0:
        log.error(f"PR task-profile failed: {detail[-500:]}")
        return {"ok": False, "error": detail[-1000:]}

    summary_csvs = sorted(ctx.task_profile_dir.glob("scan_*/org_summary.csv"))
    summary_jsons = sorted(ctx.task_profile_dir.glob("scan_*/org_summary.json"))
    result: dict[str, Any] = {"ok": True}
    if summary_csvs:
        result["org_summary_csv"] = str(summary_csvs[-1])
    if summary_jsons:
        result["org_summary_json"] = str(summary_jsons[-1])
    log.info(f"PR task-profile complete -> {result.get('org_summary_csv', ctx.task_profile_dir)}")
    return result


# Order matters: each phase's output depends on the working tree state left
# by the one before it. Used to invalidate everything downstream of a phase
# that gets redone from scratch (a fresh reclone, or an explicit
# `retry --force`) -- see _invalidate_downstream and retry_command.
REPO_PHASE_CHAIN = [
    "clone",
    "redact",
    "codebase-profiler",
    "repo-analyzer",
    "eval-kit",
    "repo-quality-score",
]


def _clone_is_valid(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _invalidate_downstream(state: StateStore, run_id: int, repo: str, from_phase: str) -> None:
    if from_phase not in REPO_PHASE_CHAIN:
        return
    for phase in REPO_PHASE_CHAIN[REPO_PHASE_CHAIN.index(from_phase) + 1 :]:
        state.reset_phase(run_id, phase, repo, force=True)


def process_repo(
    entry: RepoEntry,
    ctx: RunContext,
    log: PipelineLogger,
    on_phase: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    repo_log = ctx.repo_log_dir(entry)
    status: dict[str, Any] = {
        "full_name": entry.full_name,
        "platform": entry.platform,
        "short_name": entry.short_name,
        "phases": {},
    }
    secrets_to_scrub = [
        ctx.tokens.get(ctx.github_token_name, ""),
        ctx.tokens.get(GITLAB_TOKEN_NAME, ""),
        ctx.tokens.get(BITBUCKET_TOKEN_NAME, ""),
    ]

    def emit_phase(phase: str, event: str) -> None:
        # event: "start" | "ok" | "failed" | "skip" (resume: already ok).
        # Feeds the rich progress UI's per-phase rows; a no-op under --quiet
        # or non-TTY output (on_phase is None there). The repo name lets the
        # UI track which repos a phase is mid-flight for (sub-progress).
        if on_phase is not None:
            on_phase(phase, event, entry.full_name)

    def phase_log_path(phase: str) -> str:
        return str(repo_log / f"{phase}.log")

    def record(phase: str, ok: bool, detail: str, attempts: int) -> None:
        clean = scrub_secrets(detail, *secrets_to_scrub)
        status["phases"][phase] = {
            "ok": ok,
            "attempts": attempts,
            "detail": clean[-1000:],
        }
        log.phase_log(repo_log / f"{phase}.log", clean)

    def run_tracked(
        phase: str, fn: Callable[[], tuple[bool, str]], retries: int | None = None
    ) -> bool:
        """Skip if already ok (resume); else run with retries, write-through to
        the state DB before and after so a crash mid-phase leaves a durable
        'running' row instead of silence."""
        if ctx.state.repo_phase_status(ctx.run_id, entry.full_name, phase) in DONE_STATUSES:
            status["phases"][phase] = {
                "ok": True,
                "attempts": 0,
                "detail": "skipped (resume: already ok)",
            }
            emit_phase(phase, "skip")
            return True
        emit_phase(phase, "start")
        attempt_no = ctx.state.start_repo_phase(
            ctx.run_id, entry.full_name, entry.platform, phase, ctx.generation
        )
        t0 = time.monotonic()
        ok, detail, attempts = with_retries(
            fn, retries if retries is not None else ctx.retries, phase
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        record(phase, ok, detail, attempts)
        ctx.state.finish_repo_phase(
            ctx.run_id,
            entry.full_name,
            phase,
            ctx.generation,
            ok,
            error=detail if not ok else "",
            log_path=phase_log_path(phase),
            duration_ms=duration_ms,
            attempt=attempt_no,
        )
        emit_phase(phase, "ok" if ok else "failed")
        if not ok:
            log.info(f"  {entry.full_name}: {phase} failed after {attempts} attempt(s)")
        return ok

    # ---- clone. Local repos are copied into the disposable clones area
    # rather than analysed in place -- the redact phase rewrites files, and
    # that must never happen to the user's actual --local-repos-dir checkout. ----
    clone_path: Path
    if entry.is_local and entry.local_path:
        status["repo_path"] = str(entry.local_path)
        target_path = ctx.clone_path(entry)
        prior_clone_ok = ctx.state.repo_phase_status(ctx.run_id, entry.full_name, "clone") == OK
        if prior_clone_ok and target_path.is_dir() and any(target_path.iterdir()):
            clone_path = target_path
            record(
                "clone", True, f"reusing existing local copy from a previous run: {clone_path}", 1
            )
            emit_phase("clone", "skip")
        else:
            emit_phase("clone", "start")
            attempt_no = ctx.state.start_repo_phase(
                ctx.run_id, entry.full_name, entry.platform, "clone", ctx.generation
            )
            t0 = time.monotonic()
            ok, detail, _copied_path = copy_local_repo(entry, ctx)
            duration_ms = int((time.monotonic() - t0) * 1000)
            record("clone", ok, detail, 1)
            ctx.state.finish_repo_phase(
                ctx.run_id,
                entry.full_name,
                "clone",
                ctx.generation,
                ok,
                error=detail if not ok else "",
                clone_path=str(target_path) if ok else "",
                log_path=phase_log_path("clone"),
                duration_ms=duration_ms,
                attempt=attempt_no,
            )
            emit_phase("clone", "ok" if ok else "failed")
            if not ok:
                status["overall"] = "failed"
                return status
            clone_path = target_path
            _invalidate_downstream(ctx.state, ctx.run_id, entry.full_name, "clone")
    else:
        target_path = ctx.clone_path(entry)
        prior_clone_ok = ctx.state.repo_phase_status(ctx.run_id, entry.full_name, "clone") == OK
        if prior_clone_ok and _clone_is_valid(target_path):
            clone_path = target_path
            record("clone", True, f"reusing existing clone from a previous run: {clone_path}", 1)
            emit_phase("clone", "skip")
        else:
            emit_phase("clone", "start")
            attempt_no = ctx.state.start_repo_phase(
                ctx.run_id, entry.full_name, entry.platform, "clone", ctx.generation
            )
            t0 = time.monotonic()

            def do_clone() -> tuple[bool, str]:
                ok, msg, path = fresh_clone(entry, ctx)
                if ok and path:
                    status["clone_path"] = str(path)
                return ok, msg

            ok, detail, attempts = with_retries(do_clone, ctx.retries, "clone")
            duration_ms = int((time.monotonic() - t0) * 1000)
            record("clone", ok, detail, attempts)
            ctx.state.finish_repo_phase(
                ctx.run_id,
                entry.full_name,
                "clone",
                ctx.generation,
                ok,
                error=detail if not ok else "",
                clone_path=str(target_path) if ok else "",
                log_path=phase_log_path("clone"),
                duration_ms=duration_ms,
                attempt=attempt_no,
            )
            emit_phase("clone", "ok" if ok else "failed")
            if not ok:
                status["overall"] = "failed"
                return status
            clone_path = target_path
            # A fresh clone invalidates whatever the prior generation computed
            # from the old working tree -- without this, resume would skip
            # redact/analysis phases as "ok" against a tree that no longer
            # exists (this repo's very first clone is a no-op here: nothing
            # to invalidate yet).
            _invalidate_downstream(ctx.state, ctx.run_id, entry.full_name, "clone")

    # ---- redact: hard gate. No analysis phase below may read the tree
    # until this is ok for this repo. ----
    if not run_tracked("redact", lambda: run_redact(entry, clone_path, ctx)):
        status["overall"] = "failed"
        return status

    repo_phases: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("codebase-profiler", lambda: run_profiler(entry, clone_path, ctx)),
        ("repo-analyzer", lambda: run_repo_analyzer(entry, clone_path, ctx)),
        ("eval-kit", lambda: run_eval_kit(entry, clone_path, ctx)),
    ]
    if ctx.include_quality_score:
        repo_phases.append(
            ("repo-quality-score", lambda: run_quality_score(entry, clone_path, ctx)),
        )

    for phase_name, fn in repo_phases:
        run_tracked(phase_name, fn)

    phase_ok = all(p.get("ok") for p in status["phases"].values())
    status["overall"] = "ok" if phase_ok else "partial"
    return status


def aggregate_quality_org(ctx: RunContext, log: PipelineLogger) -> None:
    repos_dir = ctx.quality_dir / "repos"
    org_sealed = ctx.quality_dir / "org.sealed.json"
    if not repos_dir.exists():
        return
    files = list(repos_dir.glob("*.sealed.json"))
    if not files:
        return

    code, out, err = run_py(
        QUALITY_SKILL / "scripts" / "aggregate_org.py",
        [str(repos_dir), "--report", str(org_sealed)],
        timeout=600,
    )
    if code != 0:
        log.error(f"Org quality aggregate failed: {(out + err)[-300:]}")
        return

    unwrap = QUALITY_SKILL / "unwrap.py"
    if unwrap.is_file():
        run_py(
            unwrap,
            [
                str(ctx.quality_dir),
                "--csv",
                str(ctx.quality_dir / "summary.csv"),
                "--json",
                str(ctx.quality_dir / "summary.json"),
            ],
        )
    log.info(f"Quality org rollup: {org_sealed}")


def prune_old_runs(runs_root: Path, retention_days: int, log: PipelineLogger) -> None:
    """Delete run bundles past their retention window.

    Bundles carry contributor names, per-author stats and scores. Keeping them
    forever is the storage-limitation problem; this is the deletion path.
    """
    if retention_days <= 0 or not runs_root.is_dir():
        return
    cutoff = time.time() - retention_days * 86400
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir() or run_dir.stat().st_mtime >= cutoff:
            continue
        try:
            shutil.rmtree(run_dir)
            log.info(f"Retention: removed run older than {retention_days}d: {run_dir.name}")
        except OSError as exc:
            log.error(f"Retention: failed to remove {run_dir.name}: {exc}")


def remove_clones(ctx: RunContext, log: PipelineLogger) -> None:
    """Delete cloned repos after processing — they are not part of deliverables."""
    if not ctx.clones_dir.exists():
        return
    try:
        shutil.rmtree(ctx.clones_dir)
        log.info(f"Removed clones directory: {ctx.clones_dir}")
    except OSError as exc:
        log.error(f"Failed to remove clones directory: {exc}")


def create_run_zip(run_dir: Path, clones_dir: Path) -> Path:
    """Zip everything under run_dir. Clone source code must never end up in
    here -- enforced structurally (clones_dir lives outside run_dir, see
    resolve_clones_dir) and then verified twice: a precondition before
    writing anything, and a scan of the finished archive for .git/ entries.
    """
    run_dir_resolved = run_dir.resolve()
    clones_resolved = clones_dir.resolve()
    if clones_resolved == run_dir_resolved or clones_resolved.is_relative_to(run_dir_resolved):
        raise RuntimeError(
            f"refusing to zip: clones_dir ({clones_resolved}) is inside run_dir "
            f"({run_dir_resolved}) -- source code would leak into the deliverable"
        )

    zip_name = f"{run_dir.name}.zip"
    zip_path = run_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or path == zip_path:
                continue
            rel = path.relative_to(run_dir)
            if ".git" in rel.parts:
                continue
            zf.write(path, rel)

    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if ".git/" in name.replace("\\", "/"):
                raise RuntimeError(f"zip safety violation: .git path in bundle: {name}")
    return zip_path


def build_run_context(
    args: argparse.Namespace,
    include_quality_score: bool,
    run_dir_override: Path | None = None,
) -> RunContext:
    """Derive a RunContext from parsed args.

    run_dir_override forces a specific run directory instead of minting a new
    timestamped one -- used by `resume`/`retry`, which replay the exact args
    stored in state.db against the original run_dir so every derived path
    (clones_dir, logs_dir, etc.) comes out identical to the first run.
    """
    tokens = resolve_tokens(args.tokens_file)
    gitlab_projects: list[str] = []
    github_repos: list[str] = []
    bitbucket_repos: list[str] = []
    if args.local_repos_dir:
        platform = "local"
        target = args.local_batch_name
        local_repos_dir = Path(args.local_repos_dir).expanduser().resolve()
    elif args.github_org:
        platform = "github"
        target = args.github_org
        local_repos_dir = None
    elif args.github_repo:
        platform = "github-repo"
        github_repos = normalize_github_repos(args.github_repo)
        target = (
            github_repos[0]
            if len(github_repos) == 1
            else f"github-repos ({len(github_repos)} repos)"
        )
        local_repos_dir = None
    elif args.bitbucket_workspace:
        platform = "bitbucket"
        target = args.bitbucket_workspace
        local_repos_dir = None
    elif args.bitbucket_repo:
        platform = "bitbucket-repo"
        bitbucket_repos = normalize_bitbucket_repos(args.bitbucket_repo)
        target = (
            bitbucket_repos[0]
            if len(bitbucket_repos) == 1
            else f"bitbucket-repos ({len(bitbucket_repos)} repos)"
        )
        local_repos_dir = None
    elif args.gitlab_project:
        platform = "gitlab-project"
        gitlab_projects = normalize_gitlab_projects(args.gitlab_project)
        target = (
            gitlab_projects[0]
            if len(gitlab_projects) == 1
            else f"gitlab-projects ({len(gitlab_projects)} repos)"
        )
        local_repos_dir = None
    else:
        platform = "gitlab"
        target = args.gitlab_group
        local_repos_dir = None

    repos_manifest = load_repos_manifest(
        Path(args.repos_manifest).expanduser().resolve() if args.repos_manifest else None
    )

    # local-only: explicit flag, or auto for local repos with no repos-manifest.
    # A manifest is the explicit "map these folders to remotes" signal; without
    # one, a local run is pure-local. This keeps a leftover `origin` remote from
    # dragging the PR-based phases into tokenless API calls that just fail. A
    # stray env token is deliberately NOT the trigger -- intent is, not ambient
    # credentials.
    local_only = args.local_only or (platform == "local" and not repos_manifest)

    if run_dir_override is not None:
        run_dir = run_dir_override.resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if platform == "gitlab-project" and len(gitlab_projects) > 1:
            run_label = f"gitlab-projects-{len(gitlab_projects)}"
        elif platform == "github-repo" and len(github_repos) > 1:
            run_label = f"github-repos-{len(github_repos)}"
        elif platform == "bitbucket-repo" and len(bitbucket_repos) > 1:
            run_label = f"bitbucket-repos-{len(bitbucket_repos)}"
        else:
            run_label = safe_filename(target.replace("/", "_").replace(" ", "_"))
        run_name = f"org-analyser-{run_label}-{stamp}"
        output_parent = Path(args.output_dir)
        run_dir = (output_parent / run_name).resolve()

    profiler_template = PROFILER_ROOT / "codebase_sheet.xlsx"
    return RunContext(
        target=target,
        platform=platform,
        run_dir=run_dir,
        clones_dir=resolve_clones_dir(run_dir),
        merged_pr_dir=run_dir / "merged-pr-counts",
        profiler_dir=run_dir / "codebase-profiler",
        eval_kit_dir=run_dir / "eval-kit",
        quality_dir=run_dir / "repo-quality-score",
        task_profile_dir=run_dir / "pr-task-profile",
        repo_analyzer_dir=run_dir / "repo-analyzer",
        include_quality_score=include_quality_score,
        logs_dir=run_dir / "logs",
        tokens=tokens,
        workers=args.workers,
        retries=args.retries,
        clone_depth=args.clone_depth if args.clone_depth > 0 else None,
        skip_f2p=args.skip_f2p,
        skip_pr_task_profile=args.skip_pr_task_profile,
        pr_rubrics_provider=args.pr_rubrics_provider,
        local_only=local_only,
        github_host=args.github_host,
        gitlab_host=args.gitlab_host,
        github_token_name=args.github_token_name,
        local_repos_dir=local_repos_dir,
        repos_manifest=repos_manifest,
        gitlab_projects=gitlab_projects,
        github_repos=github_repos,
        bitbucket_repos=bitbucket_repos,
        profiler_template=profiler_template,
        profiler_out=run_dir / "codebase-profiler" / "codebase_sheet.filled.xlsx",
        pipeline_log=run_dir / "logs" / "pipeline.log",
    )


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add every `run`/`check` flag to `parser`.

    Shared between the `run` and `check` subparsers (check needs the same
    target/token/host configuration to know what to validate) and, for
    backward compatibility, the implicit top-level parser used when no
    subcommand is given at all (see main()).
    """
    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument(
        "--github-org",
        default=CONFIG.get("github_org"),
        help="GitHub organization to process",
    )
    target.add_argument(
        "--github-repo",
        action="append",
        metavar="OWNER/REPO",
        help=(
            "Single GitHub repo path (repeatable, or comma-separated). "
            "Example: --github-repo your-org/example-repo --github-repo your-org/backend"
        ),
    )
    target.add_argument(
        "--bitbucket-workspace",
        default=CONFIG.get("bitbucket_workspace"),
        help="Bitbucket workspace to process",
    )
    target.add_argument(
        "--bitbucket-repo",
        action="append",
        metavar="WORKSPACE/REPO",
        help=(
            "Bitbucket repo path (repeatable, or comma-separated). "
            "Example: --bitbucket-repo my-team/frontend --bitbucket-repo my-team/backend"
        ),
    )
    target.add_argument(
        "--gitlab-group",
        default=CONFIG.get("gitlab_group"),
        help="GitLab top-level group to process",
    )
    target.add_argument(
        "--gitlab-project",
        action="append",
        metavar="GROUP/PROJECT",
        help=(
            "GitLab project path (repeatable, or comma-separated). "
            "Example: --gitlab-project your-group/repo-a --gitlab-project your-group/repo-b"
        ),
    )
    target.add_argument(
        "--local-repos-dir",
        default=CONFIG.get("local_repos_dir"),
        help="Directory containing one repo per subfolder (downloaded/local checkouts)",
    )

    parser.add_argument(
        "--tokens-file",
        default=CONFIG.get("tokens_file"),
        help="Optional key=value tokens file. Default: read the `tokens:` "
        "mapping from config.yml instead.",
    )
    parser.add_argument(
        "--repos-manifest",
        default=CONFIG.get("repos_manifest"),
        help="Optional JSON map of folder_name -> owner/repo (or gitlab:group/repo) "
        "for API-backed PR analysis on local clones",
    )
    parser.add_argument(
        "--local-batch-name",
        default=CONFIG.get("local_batch_name", "local"),
        help="Batch label for local runs (output paths and run folder name)",
    )
    parser.add_argument(
        "--output-dir",
        default=CONFIG.get("output_dir", str(CODING / "outputs" / "org-analyser-runs")),
        help="Parent directory for timestamped run folders",
    )
    parser.add_argument(
        "--workers", type=int, default=CONFIG.get("workers", 10), help="Parallel repo workers"
    )
    parser.add_argument(
        "--retries", type=int, default=CONFIG.get("retries", 3), help="Retries per repo per phase"
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=CONFIG.get("retention_days", 90),
        help="Delete run folders older than this many days before starting "
        "(run bundles contain contributor data; 0 disables the sweep)",
    )
    parser.add_argument(
        "--clone-depth",
        type=int,
        default=CONFIG.get("clone_depth", 0),
        help="Git clone depth (0 = full clone, default)",
    )
    parser.add_argument("--github-host", default=CONFIG.get("github_host", "github.com"))
    parser.add_argument("--gitlab-host", default=CONFIG.get("gitlab_host", "gitlab.com"))
    parser.add_argument(
        "--github-token-name",
        default=CONFIG.get("github_token_name", GITHUB_TOKEN_NAME),
        help=f"Key in tokens file for GitHub API (default: {GITHUB_TOKEN_NAME})",
    )
    parser.add_argument(
        "--skip-quality-score",
        action="store_true",
        default=bool(CONFIG.get("skip_quality_score", False)),
        help="Skip the repo-quality-score phase and sealed-JSON org rollup",
    )
    parser.add_argument(
        "--skip-f2p",
        action=argparse.BooleanOptionalAction,
        default=bool(CONFIG.get("skip_f2p", True)),
        help="Skip F2P/P2P test verification in eval-kit (the slowest phase: it "
        "installs deps and runs the test suite 3x per accepted PR). Skipped by "
        "default; pass --no-skip-f2p (or skip_f2p: false in config.yml) when the "
        "output feeds a benchmark harness that needs execution-verified tasks.",
    )
    parser.add_argument(
        "--skip-pr-task-profile",
        action="store_true",
        default=bool(CONFIG.get("skip_pr_task_profile", False)),
        help="Skip the org-level PR task-profile phase (the flakiest/longest "
        "phase: network/GraphQL with a 24h timeout). Other phases are unaffected.",
    )
    parser.add_argument(
        "--pr-rubrics-provider",
        choices=("openai", "gemini"),
        default=CONFIG.get("pr_rubrics_provider", "openai"),
        help="LLM provider for eval-kit's PR-rubrics scoring: openai "
        "(OPENAI_API_KEY/Azure) or gemini (GEMINI_API_KEY / gemini_key in "
        "tokens). Default: openai.",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        default=bool(CONFIG.get("local_only", False)),
        help="Never contact a remote API. Runs only the code-based analyses on "
        "local checkouts and skips PR-based phases, even if a checkout still "
        "has an 'origin' remote. Auto-enabled with --local-repos-dir when no "
        "token is available.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=bool(CONFIG.get("quiet", False)),
        help="Console shows only the start line, final summary, and errors -- "
        "everything else still goes to the run's pipeline.log. Recommended for CI.",
    )

def finalize_run_args(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> argparse.Namespace:
    """Post-parse normalization + validation shared by `run` and `check`."""
    # action="append" defaults can't be set on the argument itself (argparse would
    # extend rather than replace them on CLI use), so fall back to config here.
    if not args.github_repo and CONFIG.get("github_repo"):
        args.github_repo = list(CONFIG["github_repo"])
    if not args.bitbucket_repo and CONFIG.get("bitbucket_repo"):
        args.bitbucket_repo = list(CONFIG["bitbucket_repo"])
    if not args.gitlab_project and CONFIG.get("gitlab_project"):
        args.gitlab_project = list(CONFIG["gitlab_project"])

    if not any(
        [
            args.github_org,
            args.github_repo,
            args.bitbucket_workspace,
            args.bitbucket_repo,
            args.gitlab_group,
            args.gitlab_project,
            args.local_repos_dir,
        ]
    ):
        parser.error(
            "one target required: --github-org, --github-repo, --bitbucket-workspace, "
            "--bitbucket-repo, --gitlab-group, --gitlab-project, or --local-repos-dir "
            "(or set one in config.yml)"
        )
    return args


DEFAULT_OUTPUT_DIR = CONFIG.get("output_dir", str(CODING / "outputs" / "org-analyser-runs"))
COMMANDS = ("run", "resume", "status", "retry", "check")


def build_top_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="org-analyser",
        description=(
            "Run (or resume, inspect, retry) the merged PR counts / PR task-profile / "
            "codebase profiler / eval-kit / repo-quality-score pipeline for one org/group. "
            "Defaults for `run`/`check` can be set in config.yml; CLI flags override it."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run the pipeline (default when no subcommand is given)")
    add_run_arguments(run_p)

    check_p = sub.add_parser(
        "check", help="Deep preflight only: verify credentials, tools, and quota, then exit"
    )
    add_run_arguments(check_p)

    resume_p = sub.add_parser(
        "resume", help="Resume the latest (or a given) incomplete run at its failed/pending phases"
    )
    resume_p.add_argument(
        "run_dir", nargs="?", default=None, help="Run directory to resume (default: most recent)"
    )
    resume_p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    resume_p.add_argument(
        "-q", "--quiet", action="store_true",
        help="Console shows only the start line, final summary, and errors. Recommended for CI.",
    )

    status_p = sub.add_parser("status", help="Show phase-by-phase status for a run")
    status_p.add_argument("run_dir", nargs="?", default=None)
    status_p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    status_p.add_argument("--repo", default=None, help="Show the full event trace for one repo")
    status_p.add_argument(
        "--failures", action="store_true", help="Show only failed/interrupted phases"
    )

    retry_p = sub.add_parser("retry", help="Re-run specific phases/repos of a run")
    retry_p.add_argument("run_dir", nargs="?", default=None)
    retry_p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    retry_p.add_argument(
        "--phase", default=None, help="Only this phase (an org phase name, or any repo phase)"
    )
    retry_p.add_argument("--repo", default=None, help="Only this repo (full_name as in status)")
    retry_p.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if already ok, and invalidate phases that depend on it",
    )

    return parser


def print_transparency_banner(local_only: bool) -> None:
    lines = [
        "org-analyser — what this run does with your data:",
        "  - Clones target repos locally; clones are deleted at the end of the run.",
        "  - Committer names are kept in the report; committer emails are pseudonymised, never written out.",
    ]
    if local_only:
        lines.append("  - --local-only: no code, diffs, or comments leave this machine.")
    else:
        lines.append(
            "  - Code samples, diffs, and PR/review text are sent to the configured LLM "
            "provider (OpenAI/Gemini) for scoring, redacted for secrets first."
        )
    lines.append("Full detail: SECURITY_AND_COMPLIANCE.md")
    print("\n".join(lines), file=sys.stderr)


def run_org_phase(
    ctx: RunContext, log: PipelineLogger, phase: str, fn: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Run one org-level phase (merged-pr-counts, pr-task-profile) with the
    same DB write-through and resume-skip as per-repo phases -- and,
    critically, isolated: an unhandled exception here used to abort the
    entire pipeline before a single repo was processed. It now always
    degrades to a recorded failure while discovery and the repo pool
    continue."""
    if ctx.state.org_phase_status(ctx.run_id, phase) in DONE_STATUSES:
        log.info(f"Skip {phase} (already ok from a previous run)")
        ctx.state.record_event(ctx.run_id, ctx.generation, "org", None, phase, "skipped-resume")
        return {"skipped": True, "reason": "already ok"}

    attempt = ctx.state.start_org_phase(ctx.run_id, phase, ctx.generation)
    t0 = time.monotonic()
    try:
        result = fn()
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        ctx.state.finish_org_phase(
            ctx.run_id, phase, ctx.generation, False, error=detail,
            duration_ms=duration_ms, attempt=attempt,
        )
        log.error(f"{phase} failed (isolated -- pipeline continues): {exc}")
        return {"ok": False, "error": str(exc)}

    duration_ms = int((time.monotonic() - t0) * 1000)
    ok = not (isinstance(result, dict) and result.get("ok") is False)
    error = result.get("error", "") if isinstance(result, dict) else ""
    ctx.state.finish_org_phase(
        ctx.run_id, phase, ctx.generation, ok, error=str(error),
        duration_ms=duration_ms, attempt=attempt,
    )
    return result


def write_failures_report(ctx: RunContext, log: PipelineLogger) -> Path | None:
    failures = ctx.state.failures(ctx.run_id)
    if not failures:
        return None
    lines = [f"# Failures -- {ctx.platform}:{ctx.target}", ""]
    for row in failures:
        header = row["phase"] if row["scope"] == "org" else f"{row['repo']}  {row['phase']}"
        lines.append(f"## {header}")
        lines.append(f"- status: {row['status']}, attempts: {row['attempts']}")
        lines.append(f"- log: {row['log_path'] or '(no log)'}")
        error_tail = "\n".join((row["error"] or "").strip().splitlines()[-8:])
        if error_tail:
            lines.append("```")
            lines.append(error_tail)
            lines.append("```")
        lines.append("")
    lines.append(f"Resume with: org-analyser resume {ctx.run_dir}")
    report_path = ctx.run_dir / "FAILURES.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Failures report: {report_path}")
    return report_path


def execute_pipeline(ctx: RunContext, args: argparse.Namespace, log: PipelineLogger) -> int:
    """Core pipeline body shared by a fresh `run` and a `resume` -- the only
    difference between the two is how ctx.state/run_id/generation got set up
    before this is called. Every phase inside checks the state DB first and
    skips anything already 'ok', so calling this twice on the same run_dir
    (i.e. resuming) never repeats completed work."""
    log.important(f"Starting org-analyser: {ctx.platform}={ctx.target} (generation {ctx.generation})")
    log.info(f"Include repo-quality-score: {ctx.include_quality_score}")
    if ctx.local_only:
        log.info("Local-only mode: code-based analyses only, PR/remote phases skipped.")
    log.info(f"Python: {PYTHON_BIN}")
    log.info(f"Run directory: {ctx.run_dir}")
    log.info(f"Clones directory (outside run_dir): {ctx.clones_dir}")

    prune_old_runs(ctx.run_dir.parent, args.retention_days, log)

    ctx.manifest.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    ctx.manifest.update(
        {
            "platform": ctx.platform,
            "target": ctx.target,
            "workers": ctx.workers,
            "retries": ctx.retries,
            "clone_depth": ctx.clone_depth,
            "include_quality_score": ctx.include_quality_score,
            "generation": ctx.generation,
        }
    )
    ctx.manifest.setdefault("repos", [])
    ctx.manifest.setdefault("phases", {})
    if ctx.platform == "local":
        ctx.manifest["local_repos_dir"] = str(ctx.local_repos_dir)
        if ctx.repos_manifest:
            ctx.manifest["repos_manifest"] = ctx.repos_manifest
    elif ctx.platform == "github-repo":
        ctx.manifest["github_repos"] = ctx.github_repos
    elif ctx.platform == "gitlab-project":
        ctx.manifest["gitlab_projects"] = ctx.gitlab_projects

    try:
        preflight(ctx, log)

        log.info("Sanity check: connectivity, git auth, LLM reachability...")
        sanity_ok, sanity_results, entries = run_sanity_checks(ctx, log, args.output_dir)
        for name, passed, detail in sanity_results:
            (log.info if passed else log.error)(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")
        if not sanity_ok:
            log.error("Sanity check failed -- aborting before any phase started. Fix the above and retry.")
            ctx.manifest["error"] = "sanity check failed"
            ctx.manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            (ctx.run_dir / "manifest.json").write_text(
                json.dumps(ctx.manifest, indent=2), encoding="utf-8"
            )
            return 1

        ctx.manifest["repo_count"] = len(entries)

        # merged-pr-counts runs concurrently with the repo pool (it only reads
        # the hosting API, not the clones). pr-task-profile is deliberately held
        # back to run LAST, once every other folder is written: it is the
        # flakiest and longest phase (network/GraphQL, 24h timeout), so running
        # it on its own at the end means a failure there can never block or
        # taint the rest of the run's output.
        log.info(
            f"Per-repo phases: processing {len(entries)} repos with {ctx.workers} workers "
            "(alongside merged-pr-counts; pr-task-profile runs last)"
        )
        repo_results: list[dict[str, Any]] = []
        use_rich = should_use_rich(args.quiet)
        console = Console() if use_rich else None
        progress_ui: RunProgress | None = None

        with ExitStack() as stack:
            if use_rich:
                stack.enter_context(rich_console_handler(log.logger, console))
                progress_ui = stack.enter_context(RunProgress(console, len(entries)))
            org_pool = stack.enter_context(ThreadPoolExecutor(max_workers=2))
            repo_pool = stack.enter_context(ThreadPoolExecutor(max_workers=ctx.workers))

            org_futures: dict[str, Any] = {}
            if ctx.platform == "local":
                log.info("merged-pr-counts skipped (local mode)")
                ctx.manifest["phases"]["merged-pr-counts"] = {
                    "skipped": True,
                    "reason": "local mode",
                }
            else:
                if progress_ui:
                    progress_ui.start_org_phase("merged-pr-counts")
                fut = org_pool.submit(
                    run_org_phase, ctx, log, "merged-pr-counts", lambda: run_merged_pr_counts(ctx, log)
                )
                org_futures["merged-pr-counts"] = fut
                if progress_ui:
                    # Flip the row the moment the phase resolves -- the harvest
                    # loop below only runs after the whole repo pool drains,
                    # which left this row spinning as "running" long after the
                    # work finished. Callback runs in the worker thread; rich
                    # task updates are thread-safe, and the manifest write
                    # stays in the harvest loop (re-finishing is idempotent).
                    def _flip_merged_pr_counts(f: Any) -> None:
                        exc = f.exception()
                        result = None if exc else f.result()
                        ok = exc is None and not (
                            isinstance(result, dict) and result.get("ok") is False
                        )
                        progress_ui.finish_org_phase("merged-pr-counts", ok)

                    fut.add_done_callback(_flip_merged_pr_counts)
            # pr-task-profile is NOT submitted here -- it runs last, after the
            # repo pool and merged-pr-counts finish (see below). Row appears
            # immediately anyway so the phase isn't invisible until then.
            if progress_ui and not ctx.skip_pr_task_profile:
                progress_ui.queue_org_phase("pr-task-profile", "queued (runs last)")
            on_phase = progress_ui.update_phase if progress_ui else None
            repo_futures = {
                repo_pool.submit(process_repo, e, ctx, log, on_phase): e for e in entries
            }

            # eval-kit sub-progress: with few repos the phase bar's whole-repo
            # granularity reads as "stuck at 0%" for the longest phase. The
            # subprocess streams one "Processing PR #..." line per PR, and
            # phase 1's CSVs give the denominator, so a watcher can move the
            # bar fractionally through the phase.
            stop_eval_watch = threading.Event()
            eval_watch: threading.Thread | None = None
            if progress_ui:
                entries_by_name = {e.full_name: e for e in entries}

                def _watch_eval_kit() -> None:
                    totals: dict[str, int] = {}
                    while not stop_eval_watch.wait(5.0):
                        if not totals:
                            totals = merged_pr_totals(ctx.merged_pr_dir)
                        running = progress_ui.phase_running("eval-kit")
                        if not running:
                            continue
                        scanned_sum, total_sum, frac_sum = 0, 0, 0.0
                        for name in running:
                            entry = entries_by_name.get(name)
                            if entry is None:
                                continue
                            stream = ctx.repo_log_dir(entry) / "eval-kit.stream.log"
                            try:
                                text = stream.read_text(encoding="utf-8", errors="replace")
                            except OSError:
                                continue
                            scanned = text.count("Processing PR #")
                            scanned_sum += scanned
                            total = totals.get(name, 0)
                            if total:
                                total_sum += total
                                frac_sum += min(scanned / total, 0.99)
                        if not scanned_sum:
                            continue
                        detail = (
                            f"[{scanned_sum}/{total_sum} PRs]"
                            if total_sum
                            else f"[~{scanned_sum} PRs]"
                        )
                        progress_ui.note_phase_progress("eval-kit", frac_sum, detail)

                eval_watch = threading.Thread(target=_watch_eval_kit, daemon=True)
                eval_watch.start()

            for i, fut in enumerate(as_completed(repo_futures), 1):
                entry = repo_futures[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    result = {
                        "full_name": entry.full_name,
                        "platform": entry.platform,
                        "overall": "error",
                        "error": str(exc),
                        "phases": {},
                    }
                repo_results.append(result)
                ok_count = sum(1 for r in repo_results if r.get("overall") == "ok")
                if progress_ui:
                    partial_count = sum(1 for r in repo_results if r.get("overall") == "partial")
                    failed_count = sum(1 for r in repo_results if r.get("overall") in ("failed", "error"))
                    progress_ui.advance_repo(i, ok_count, partial_count, failed_count)
                elif i % 5 == 0 or i == len(entries):
                    log.info(f"  Progress {i}/{len(entries)} ({ok_count} fully ok)")

            stop_eval_watch.set()
            if eval_watch is not None:
                eval_watch.join(timeout=2)

            for name, fut in org_futures.items():
                result = fut.result()
                ctx.manifest["phases"][name] = result
                if progress_ui:
                    ok = not (isinstance(result, dict) and result.get("ok") is False)
                    progress_ui.finish_org_phase(name, ok)

            # pr-task-profile runs LAST -- every repo phase and merged-pr-counts
            # is done and written by now, so its network/GraphQL flakiness can
            # only affect its own folder, never the rest of the run.
            if ctx.skip_pr_task_profile:
                log.info("pr-task-profile skipped (--skip-pr-task-profile)")
                ctx.manifest["phases"]["pr-task-profile"] = {
                    "skipped": True,
                    "reason": "--skip-pr-task-profile",
                }
            else:
                if progress_ui:
                    progress_ui.start_org_phase("pr-task-profile")
                stop_watch = threading.Event()
                watch_thread: threading.Thread | None = None
                if progress_ui:
                    # Surface the subprocess's Batch API stages on this row
                    # ("batch: 1 in_progress [120/500]") by polling the state
                    # sidecars llm.batch writes on every transition -- without
                    # this, a multi-hour batch wait is a bare spinner.
                    def _watch_batches() -> None:
                        while not stop_watch.wait(5.0):
                            summary = batch_state_summary(ctx.task_profile_dir / "batch_state")
                            if summary:
                                progress_ui.note_org_phase(
                                    "pr-task-profile", f"running ({summary})"
                                )

                    watch_thread = threading.Thread(target=_watch_batches, daemon=True)
                    watch_thread.start()
                try:
                    task_result = run_org_phase(
                        ctx, log, "pr-task-profile", lambda: run_pr_task_profile(ctx, log, entries)
                    )
                finally:
                    stop_watch.set()
                    if watch_thread is not None:
                        watch_thread.join(timeout=2)
                ctx.manifest["phases"]["pr-task-profile"] = task_result
                if progress_ui:
                    ok = not (isinstance(task_result, dict) and task_result.get("ok") is False)
                    progress_ui.finish_org_phase("pr-task-profile", ok)

        if ctx.include_quality_score:
            if ctx.state.org_phase_status(ctx.run_id, "aggregate-quality-org") in DONE_STATUSES:
                log.info("Skip aggregate-quality-org (already ok from a previous run)")
            else:
                run_org_phase(
                    ctx, log, "aggregate-quality-org", lambda: aggregate_quality_org(ctx, log) or {}
                )
        else:
            log.info("Repo-quality-score rollup skipped (disabled for this pipeline variant)")

        # This generation's repo pool is the source of truth for every repo it
        # touched; anything from a prior generation's manifest for a repo not
        # in this run's `entries` (impossible today, since discovery always
        # re-runs, but kept defensive) is left alone rather than dropped.
        prior_repos = {r.get("full_name"): r for r in ctx.manifest.get("repos", [])}
        for r in repo_results:
            prior_repos[r.get("full_name")] = r
        ctx.manifest["repos"] = sorted(prior_repos.values(), key=lambda r: r.get("full_name", ""))
        ctx.manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        all_repos = ctx.manifest["repos"]
        ctx.manifest["summary"] = {
            "total": len(all_repos),
            "fully_ok": sum(1 for r in all_repos if r.get("overall") == "ok"),
            "partial": sum(1 for r in all_repos if r.get("overall") == "partial"),
            "failed": sum(1 for r in all_repos if r.get("overall") in ("failed", "error")),
        }

        manifest_path = ctx.run_dir / "manifest.json"
        manifest_path.write_text(json.dumps(ctx.manifest, indent=2), encoding="utf-8")

        fully_ok = (
            ctx.manifest["summary"]["failed"] == 0
            and ctx.manifest["summary"]["partial"] == 0
            and not ctx.state.failures(ctx.run_id)
        )

        if fully_ok:
            remove_clones(ctx, log)
            ctx.manifest["clones_removed"] = True
        else:
            log.info(f"Run has failures -- keeping clones for resume: {ctx.clones_dir}")
            ctx.manifest["clones_removed"] = False
        if ctx.platform == "local":
            # The disposable working copy under clones_dir may have been
            # removed above; the user's actual --local-repos-dir checkout is
            # never touched (see copy_local_repo), regardless of run outcome.
            ctx.manifest["local_source_preserved"] = True
        manifest_path.write_text(json.dumps(ctx.manifest, indent=2), encoding="utf-8")

        failures_path = write_failures_report(ctx, log)
        ctx.state.finish_run(ctx.run_id, "ok" if fully_ok else "partial")

        if fully_ok:
            zip_path = create_run_zip(ctx.run_dir, ctx.clones_dir)
            log.important(f"Zip: {zip_path}")
        else:
            log.important(f"Skipping zip (run incomplete). Resume with: org-analyser resume {ctx.run_dir}")

        log.important(f"Run complete. Manifest: {manifest_path}")
        if failures_path:
            log.important(f"Failures: {failures_path}")
        log.important(
            f"Summary: {ctx.manifest['summary']['fully_ok']} ok, "
            f"{ctx.manifest['summary']['partial']} partial, "
            f"{ctx.manifest['summary']['failed']} failed"
        )
        if _target_repo_warning_count:
            log.info(
                f"({_target_repo_warning_count} syntax warnings from target-repo source "
                "suppressed -- not org-analyser code, informational only)"
            )
        return 0 if fully_ok else 1
    except KeyboardInterrupt:
        log.error(
            f"Pipeline interrupted (Ctrl-C). Every completed phase is durable in "
            f"state.db -- resume with: org-analyser resume {ctx.run_dir}"
        )
        raise
    except SystemExit as exc:
        raise exc
    except Exception as exc:
        log.error(f"Pipeline aborted: {exc}\n{traceback.format_exc()}")
        ctx.manifest["error"] = str(exc)
        ctx.manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        (ctx.run_dir / "manifest.json").write_text(
            json.dumps(ctx.manifest, indent=2), encoding="utf-8"
        )
        return 1
    finally:
        ctx.state.close()


def run_pipeline(args: argparse.Namespace) -> int:
    print_transparency_banner(args.local_only)
    include_quality_score = not args.skip_quality_score
    ctx = build_run_context(args, include_quality_score=include_quality_score)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    ctx.profiler_dir.mkdir(parents=True, exist_ok=True)

    ctx.state = StateStore(ctx.run_dir / "state.db")
    ctx.run_id = ctx.state.init_run(ctx.run_dir, ctx.target, ctx.platform, vars(args))
    ctx.generation = ctx.state.get_generation(ctx.run_id)

    log = PipelineLogger(ctx.pipeline_log, quiet=args.quiet)
    return execute_pipeline(ctx, args, log)


def _find_latest_run(
    output_dir: Path, predicate: Callable[[Any], bool] | None = None
) -> Path | None:
    if not output_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for child in sorted(output_dir.iterdir()):
        db_path = child / "state.db"
        if not child.is_dir() or not db_path.is_file():
            continue
        try:
            store = StateStore(db_path)
            row = store.load_run(child)
            store.close()
        except Exception:
            continue
        if row is None or (predicate and not predicate(row)):
            continue
        candidates.append((db_path.stat().st_mtime, child))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def resume_pipeline(run_dir_arg: str | None, output_dir: str, quiet: bool = False) -> int:
    if run_dir_arg:
        run_dir = Path(run_dir_arg).expanduser().resolve()
    else:
        found = _find_latest_run(
            Path(output_dir).expanduser().resolve(), lambda row: row["status"] != OK
        )
        if not found:
            print("No incomplete run found to resume.", file=sys.stderr)
            return 1
        run_dir = found

    db_path = run_dir / "state.db"
    if not db_path.is_file():
        print(f"No state.db under {run_dir} -- nothing to resume.", file=sys.stderr)
        return 1

    state = StateStore(db_path)
    row = state.load_run(run_dir)
    if not row:
        print(f"No run row found for {run_dir} in {db_path}.", file=sys.stderr)
        return 1

    config = json.loads(row["config_json"])
    args = argparse.Namespace(**config)
    include_quality_score = not args.skip_quality_score
    ctx = build_run_context(args, include_quality_score, run_dir_override=run_dir)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    ctx.profiler_dir.mkdir(parents=True, exist_ok=True)

    ctx.state = state
    ctx.run_id = int(row["id"])
    ctx.generation = state.resume_run(ctx.run_id)

    manifest_path = ctx.run_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            ctx.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ctx.manifest = {}

    log = PipelineLogger(ctx.pipeline_log, quiet=quiet)
    log.important(f"Resuming run {run_dir} at generation {ctx.generation}")
    print_transparency_banner(args.local_only)
    return execute_pipeline(ctx, args, log)


def status_command(run_dir_arg: str | None, output_dir: str, repo: str | None, failures_only: bool) -> int:
    if run_dir_arg:
        run_dir = Path(run_dir_arg).expanduser().resolve()
    else:
        found = _find_latest_run(Path(output_dir).expanduser().resolve())
        if not found:
            print("No run found.", file=sys.stderr)
            return 1
        run_dir = found

    db_path = run_dir / "state.db"
    if not db_path.is_file():
        print(f"No state.db under {run_dir}.", file=sys.stderr)
        return 1

    state = StateStore(db_path)
    row = state.load_run(run_dir)
    if not row:
        print(f"No run row in {db_path}.", file=sys.stderr)
        return 1
    run_id = int(row["id"])

    print(f"Run: {run_dir}")
    print(f"Target: {row['platform']}:{row['target']}  status={row['status']}  generation={row['generation']}")
    print()

    if repo:
        events = state.trace(run_id, repo)
        if not events:
            print(f"No events recorded for repo {repo!r}.")
        for e in events:
            line = (
                f"  [{e['ts']}] gen={e['generation']} {e['phase']:<20} {e['event']:<16} "
                f"attempt={e['attempt']}"
            )
            if e["duration_ms"]:
                line += f" ({e['duration_ms']}ms)"
            if e["error_tail"]:
                line += f"  ERROR: {e['error_tail']}"
            print(line)
        state.close()
        return 0

    if failures_only:
        failures = state.failures(run_id)
        if not failures:
            print("No failures.")
        for f in failures:
            header = f["phase"] if f["scope"] == "org" else f"{f['repo']}  {f['phase']}"
            print(f"  [{f['status'].upper()}] {header}  attempts={f['attempts']}  log={f['log_path']}")
            if f["error"]:
                print(f"      {f['error'].strip().splitlines()[-1][:200]}")
        state.close()
        return 0

    summary = state.status_summary(run_id)
    print("Org phases:")
    for p in summary["org_phases"]:
        print(f"  [{p['status'].upper():<11}] {p['phase']:<24} attempts={p['attempts']}")
    print()
    counts = summary["repo_counts"]
    print(
        f"Repos: {summary['repo_count']} total -- ok={counts['ok']} partial={counts['partial']} "
        f"failed={counts['failed']} running={counts['running']} pending={counts['pending']}"
    )
    if counts["failed"] or counts["running"]:
        print("\nRepos needing attention:")
        seen: set[str] = set()
        for r in summary["repo_phases"]:
            if r["status"] in ("failed", "interrupted", "running") and r["repo"] not in seen:
                seen.add(r["repo"])
                print(f"  {r['repo']}: {r['phase']} -> {r['status']}")
    state.close()
    return 0


def retry_command(
    run_dir_arg: str | None,
    output_dir: str,
    phase: str | None,
    repo: str | None,
    force: bool,
) -> int:
    if run_dir_arg:
        run_dir = Path(run_dir_arg).expanduser().resolve()
    else:
        found = _find_latest_run(Path(output_dir).expanduser().resolve())
        if not found:
            print("No run found.", file=sys.stderr)
            return 1
        run_dir = found

    db_path = run_dir / "state.db"
    if not db_path.is_file():
        print(f"No state.db under {run_dir}.", file=sys.stderr)
        return 1

    state = StateStore(db_path)
    row = state.load_run(run_dir)
    if not row:
        print(f"No run row in {db_path}.", file=sys.stderr)
        return 1
    run_id = int(row["id"])

    quality_touched = False
    if not phase and not repo:
        n = state.reset_repo_all_failed(run_id)
        print(f"Reset {n} failed/interrupted/running phase row(s) to pending.")
        quality_touched = True
    elif phase and not repo:
        if phase in ("merged-pr-counts", "pr-task-profile", "aggregate-quality-org"):
            state.reset_phase(run_id, phase, force=force)
            print(f"Reset org phase {phase!r}.")
        else:
            n = state.reset_phase_all_repos(run_id, phase, force=force)
            print(f"Reset {n} row(s) for phase {phase!r} across all repos.")
            if force:
                repos = {r["repo"] for r in state.status_summary(run_id)["repo_phases"]}
                for r in repos:
                    _invalidate_downstream(state, run_id, r, phase)
            quality_touched = phase == "repo-quality-score"
    elif repo and not phase:
        phases = state.repo_phases_for(run_id, repo)
        for p in phases:
            state.reset_phase(run_id, p.phase, repo, force=force)
        print(f"Reset all {len(phases)} phase(s) for repo {repo!r}.")
        quality_touched = True
    else:
        state.reset_phase(run_id, phase, repo, force=force)
        print(f"Reset phase {phase!r} for repo {repo!r}.")
        if force:
            _invalidate_downstream(state, run_id, repo, phase)
        quality_touched = phase == "repo-quality-score"

    if quality_touched:
        # Invalidation, not a retry-scope decision -- the aggregate is stale
        # the moment any repo's repo-quality-score is reset, regardless of
        # the aggregate's own current status or the CLI --force flag.
        state.reset_phase(run_id, "aggregate-quality-org", force=True)

    state.close()
    print(f"Apply with: org-analyser resume {run_dir}")
    return 0


def _print_check_table(results: list[tuple[str, bool, str]]) -> bool:
    all_ok = True
    print("\norg-analyser check -- sanity results:", file=sys.stderr)
    for name, ok, detail in results:
        if not ok:
            all_ok = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", file=sys.stderr)
    print(
        "\nAll checks passed -- safe to run."
        if all_ok
        else "\nOne or more checks failed -- fix the above before running.",
        file=sys.stderr,
    )
    return all_ok


def run_sanity_checks(
    ctx: RunContext, log: PipelineLogger, output_dir: str
) -> tuple[bool, list[tuple[str, bool, str]], list[RepoEntry]]:
    """Live checks shared by `org-analyser check` and by every `run`/`resume`
    before any phase starts: list repos, git ls-remote one of them, ping the
    LLM endpoint, check disk space. Any failure means abort with zero clones
    made and zero API quota burned on the real phases."""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn: Callable[[], str]) -> None:
        try:
            results.append((name, True, fn()))
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    def _check_disk() -> str:
        probe_dir = Path(output_dir).expanduser()
        usage = shutil.disk_usage(probe_dir if probe_dir.exists() else probe_dir.parent)
        free_gb = usage.free / (1024**3)
        if free_gb < 2:
            raise RuntimeError(f"only {free_gb:.1f} GB free under {probe_dir}")
        return f"{free_gb:.1f} GB free"

    check("disk space", _check_disk)

    def _check_output_dir() -> str:
        d = Path(output_dir).expanduser().resolve()
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".org-analyser-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return str(d)

    check("output directory writable", _check_output_dir)

    entries: list[RepoEntry] = []

    def _check_discovery() -> str:
        nonlocal entries
        entries = discover_repos(ctx, log)
        if not entries:
            raise RuntimeError("no repos discovered")
        return f"{len(entries)} repos"

    check(f"list repos ({ctx.platform}:{ctx.target})", _check_discovery)

    def _check_clone_auth() -> str:
        if not entries:
            raise RuntimeError("skipped: repo listing failed above")
        sample = entries[0]
        if sample.is_local:
            return f"local checkout: {sample.local_path}"
        url = clone_url(sample, ctx.tokens, ctx)
        token = ""
        user = "x-access-token"
        if sample.platform == "github":
            token = ctx.tokens.get(ctx.github_token_name, "")
        elif sample.platform == "gitlab":
            token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
        elif sample.platform == "bitbucket":
            token = ctx.tokens.get(BITBUCKET_TOKEN_NAME, "")
            user = resolve_bitbucket_git_auth(token, ctx.tokens.get(BITBUCKET_USERNAME_NAME, "").strip())
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        if token:
            auth = base64.b64encode(f"{user}:{token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {auth}"
        proc = subprocess.run(
            ["git", *git_longpath_config(), "ls-remote", url],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(scrub_secrets((proc.stderr or proc.stdout or "")[-400:], token))
        return f"git ls-remote ok ({sample.full_name})"

    check("git clone auth", _check_clone_auth)

    def _check_llm() -> str:
        if ctx.local_only:
            return "skipped (--local-only)"
        if ctx.pr_rubrics_provider == "gemini":
            return "skipped (gemini live-check not implemented)"
        from llm.llm_safety import safe_openai  # noqa: WPS433
        from analysis.pr_task_profile import DEFAULT_MODEL  # noqa: WPS433

        # A real completion call, not models.list() -- that only proves the
        # key is authenticated, not that the account has completions quota.
        # insufficient_quota comes back as a 429 here, same as the real
        # per-PR calls pr-task-profile makes, so it's caught before any
        # phase runs instead of 11+ calls deep into a real run.
        safe_openai().chat.completions.create(
            model=DEFAULT_MODEL,
            temperature=0,
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        return f"LLM completion call succeeded (model={DEFAULT_MODEL})"

    check("LLM credentials live", _check_llm)

    return all(ok for _, ok, _ in results), results, entries


def run_check(args: argparse.Namespace) -> int:
    """`org-analyser check`: prove the run will complete before spending any
    time on it. Deeper than preflight() (which only checks presence) -- this
    makes live calls via run_sanity_checks(). Any failure means abort with
    zero clones made and zero API quota burned on the real phases."""
    print_transparency_banner(args.local_only)
    include_quality_score = not args.skip_quality_score
    ctx = build_run_context(args, include_quality_score=include_quality_score)

    check_dir = Path(args.output_dir).expanduser().resolve() / ".check"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = PipelineLogger(check_dir / f"check-{stamp}.log")

    results: list[tuple[str, bool, str]] = []

    try:
        preflight(ctx, log)
        results.append(("preflight (tokens/tools present)", True, "ok"))
    except SystemExit:
        results.append(("preflight (tokens/tools present)", False, "see errors logged above"))
        return 0 if _print_check_table(results) else 1

    _, sanity_results, _entries = run_sanity_checks(ctx, log, args.output_dir)
    results.extend(sanity_results)

    return 0 if _print_check_table(results) else 1


def main() -> int:
    argv = sys.argv[1:]
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv = ["run", *argv]
    parser = build_top_parser()
    args = parser.parse_args(argv)

    if args.command in ("run", "check"):
        args = finalize_run_args(args, parser)
    if args.command == "run":
        return run_pipeline(args)
    if args.command == "check":
        return run_check(args)
    if args.command == "resume":
        return resume_pipeline(args.run_dir, args.output_dir, quiet=args.quiet)
    if args.command == "status":
        return status_command(args.run_dir, args.output_dir, args.repo, args.failures)
    if args.command == "retry":
        return retry_command(args.run_dir, args.output_dir, args.phase, args.repo, args.force)
    parser.error("a command is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
