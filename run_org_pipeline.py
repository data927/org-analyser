#!/usr/bin/env python3
"""
Unified org pipeline: merged PR counts, PR task-profile report, codebase profiler,
eval-kit (full LLM), and optionally repo-quality-score (sealed) for one GitHub org,
one GitLab group, or a folder of downloaded/local repos per run.

Usage:
    python run_org_pipeline.py --github-org data-tech --tokens-file tokens --workers 10
    python run_org_pipeline.py --github-repo data-tech/frontend --tokens-file tokens --workers 1
    python run_org_pipeline.py --github-repo data-tech/repo-a --github-repo data-tech/repo-b --tokens-file tokens --workers 4
    python run_org_pipeline.py --gitlab-group my-group --tokens-file tokens --workers 10
    python run_org_pipeline.py --gitlab-project my-group/repo-a --gitlab-project my-group/repo-b --tokens-file tokens --workers 4
    python run_org_pipeline.py --local-repos-dir ./my-repos --tokens-file tokens --workers 4
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CODING = Path(__file__).resolve().parent
EVAL_KIT = CODING / "repo-eval-kit"
PROFILER_ROOT = CODING / "codebase_profiler"
QUALITY_SKILL = CODING / "repo-quality-score"
QUALITY_AGENT = CODING / "outputs" / "repo-quality-score-agent"
SIGNAL_SCORER_DIR = CODING / "outputs" / "repo-quality-score"
TASK_PROFILE_SCRIPT = CODING / "pr_task_profile_report.py"
REPO_ANALYZER_SCRIPT = CODING / "repo_analyzer.py"

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
    os.environ.get("ORG_PIPELINE_PYTHON", ""),
    *active_venv_python_candidates(),
    sys.executable,
    str(CODING / ".venv" / "bin" / "python"),
    str(CODING / ".venv" / "Scripts" / "python.exe"),
    str(CODING / "env311" / "bin" / "python"),
    str(PROFILER_ROOT / ".venv" / "bin" / "python"),
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
    """Pick a clone root that stays under Windows MAX_PATH limits."""
    nested = run_dir / "clones"
    if os.name != "nt":
        return nested
    # Deep run folders (e.g. under Downloads) exceed git's GIT_DIR limit on Windows.
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TEMP")
        or "C:\\Temp"
    )
    return base / "org-pipeline-clones" / run_dir.name


sys.path.insert(0, str(CODING))
sys.path.insert(0, str(PROFILER_ROOT))

from count_merged_prs import list_github_repos, list_gitlab_projects  # noqa: E402
from export_all_merged_prs import (  # noqa: E402
    CSV_FIELDS,
    SUMMARY_FIELDS,
    export_github_org,
    export_github_repos,
    export_gitlab_group,
    export_gitlab_projects,
    safe_filename,
)
GITHUB_TOKEN_NAME = "github-data-token"
GITLAB_TOKEN_NAME = "gitlab_token"
OPENAI_TOKEN_NAMES = ("openai_key", "OPENAI_API_KEY")


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
    repo_analyzer_dir: Path
    quality_dir: Path
    task_profile_dir: Path
    include_quality_score: bool
    logs_dir: Path
    tokens: dict[str, str]
    workers: int
    retries: int
    clone_depth: int | None
    github_host: str
    gitlab_host: str
    github_token_name: str
    local_repos_dir: Path | None
    repos_manifest: dict[str, str]
    gitlab_projects: list[str]
    github_repos: list[str]
    profiler_template: Path
    profiler_out: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    pipeline_log: Path = field(default_factory=Path)

    def repo_log_dir(self, entry: RepoEntry) -> Path:
        return self.logs_dir / entry.platform / entry.batch_org / entry.short_name

    def clone_path(self, entry: RepoEntry) -> Path:
        return self.clones_dir / entry.platform / entry.batch_org / entry.short_name


class PipelineLogger:
    def __init__(self, master_log: Path) -> None:
        self.master_log = master_log
        master_log.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("org_pipeline")
        self._logger.setLevel(logging.INFO)
        if not self._logger.handlers:
            fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            fh = logging.FileHandler(master_log, encoding="utf-8")
            fh.setFormatter(fmt)
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            self._logger.addHandler(fh)
            self._logger.addHandler(sh)

    def info(self, msg: str) -> None:
        self._logger.info(msg)

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


def resolve_openai_key(tokens: dict[str, str]) -> str | None:
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key
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


def _ensure_quality_imports() -> None:
    for path in (QUALITY_AGENT, SIGNAL_SCORER_DIR):
        s = str(path)
        if s not in sys.path:
            sys.path.insert(0, s)


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

    openai_key = resolve_openai_key(ctx.tokens)
    if not openai_key:
        errors.append(
            "Missing OpenAI API key: set OPENAI_API_KEY or openai_key= in tokens file"
        )
    else:
        os.environ["OPENAI_API_KEY"] = openai_key

    if ctx.platform in ("github", "github-repo"):
        if not ctx.tokens.get(ctx.github_token_name):
            errors.append(f"Missing {ctx.github_token_name} in tokens file")
        if ctx.platform == "github-repo":
            for repo in ctx.github_repos:
                if "/" not in repo:
                    errors.append(
                        f"GitHub repo path must be owner/repo (got {repo!r})"
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

    if not (EVAL_KIT / "repo_evaluator.py").is_file():
        errors.append(f"eval-kit not found: {EVAL_KIT / 'repo_evaluator.py'}")

    if not REPO_ANALYZER_SCRIPT.is_file():
        errors.append(f"repo-analyzer not found: {REPO_ANALYZER_SCRIPT}")

    if not TASK_PROFILE_SCRIPT.is_file():
        errors.append(f"PR task-profile script not found: {TASK_PROFILE_SCRIPT}")

    if ctx.include_quality_score:
        if not (QUALITY_SKILL / "scripts" / "score.py").is_file():
            errors.append(f"repo-quality-score scripts not found under {QUALITY_SKILL}")

    try:
        import openpyxl  # noqa: F401
    except ImportError:
        errors.append("openpyxl not installed (pip install -r org_pipeline_requirements.txt)")

    for pkg in ("requests", "openai"):
        try:
            __import__(pkg)
        except ImportError:
            errors.append(f"{pkg} not installed (pip install -r org_pipeline_requirements.txt)")

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

    if ctx.platform == "github":
        summary = export_github_org(
            ctx.tokens[ctx.github_token_name],
            ctx.target,
            ctx.github_token_name,
            ctx.merged_pr_dir,
            ctx.github_host,
        )
    elif ctx.platform == "github-repo":
        summary = export_github_repos(
            ctx.tokens[ctx.github_token_name],
            ctx.github_repos,
            ctx.github_token_name,
            ctx.merged_pr_dir,
            ctx.github_host,
        )
    elif ctx.platform == "gitlab-project":
        summary = export_gitlab_projects(
            ctx.tokens[GITLAB_TOKEN_NAME],
            ctx.gitlab_projects,
            GITLAB_TOKEN_NAME,
            ctx.merged_pr_dir,
            ctx.gitlab_host,
        )
    else:
        summary = export_gitlab_group(
            ctx.tokens[GITLAB_TOKEN_NAME],
            ctx.target,
            GITLAB_TOKEN_NAME,
            ctx.merged_pr_dir,
            ctx.gitlab_host,
        )

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
    host = ctx.gitlab_host.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/{entry.full_name}.git"


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
    # Pass token via environment variable instead of embedding in URL
    if entry.platform == "github":
        token = ctx.tokens.get(ctx.github_token_name, "")
        if token:
            env["GIT_ASKPASS_OVERRIDE"] = token
            env["GIT_HTTP_EXTRAHEADER"] = f"AUTHORIZATION: basic {__import__('base64').b64encode((f':{token}').encode()).decode()}"
    elif entry.platform == "gitlab":
        token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
        if token:
            env["GIT_ASKPASS_OVERRIDE"] = token
            env["GIT_HTTP_EXTRAHEADER"] = f"PRIVATE-TOKEN: {token}"

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return False, "clone timeout", None
    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "")[-800:]
        # Sanitize error message to remove any accidentally exposed tokens
        err_msg = err_msg.replace(ctx.tokens.get(ctx.github_token_name, ""), "[REDACTED]")
        err_msg = err_msg.replace(ctx.tokens.get(GITLAB_TOKEN_NAME, ""), "[REDACTED]")
        return False, err_msg, None

    verify = subprocess.run(
        ["git", *git_longpath_config(), "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
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
) -> tuple[int, str, str]:
    """Run a Python script; return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [PYTHON_BIN, str(script), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


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
            remote = provider.get_repo(owner, name)
            result = profile_dataset(
                str(clone_path),
                use_github=True,
                provider=provider,
                remote=remote,
                originating_company=entry.org,
                repo_name=entry.repo_slug,
            )
        else:
            result = profile_dataset(
                str(clone_path),
                use_github=True,
                originating_company=entry.org,
                repo_name=entry.repo_slug,
            )
        append_row(result, template=str(ctx.profiler_template), out=str(ctx.profiler_out))
        return True, f"profiler ok ({len(result.values)} fields)"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


def run_eval_kit(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    out_dir = ctx.eval_kit_dir / entry.batch_org / entry.short_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"{entry.short_name}.json"

    remote_ref = resolve_remote_ref(entry, ctx)
    if remote_ref:
        platform, full_name = remote_ref
        if platform == "github":
            token = ctx.tokens.get(ctx.github_token_name, "")
            repo_arg = full_name
        else:
            token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
            repo_arg = f"gitlab:{full_name}"
        args = [
            repo_arg,
            "--token", token,
            "--platform", platform,
            "--repo-path", str(clone_path),
            "--json",
            "--output", str(out_json),
        ]
    elif entry.is_local:
        args = [
            str(clone_path),
            "--platform", "local",
            "--json",
            "--output", str(out_json),
        ]
    elif entry.platform == "github":
        token = ctx.tokens[ctx.github_token_name]
        repo_arg = entry.full_name
        platform = "github"
        args = [
            repo_arg,
            "--token", token,
            "--platform", platform,
            "--repo-path", str(clone_path),
            "--json",
            "--output", str(out_json),
        ]
    else:
        token = ctx.tokens[GITLAB_TOKEN_NAME]
        repo_arg = f"gitlab:{entry.full_name}"
        platform = "gitlab"
        args = [
            repo_arg,
            "--token", token,
            "--platform", platform,
            "--repo-path", str(clone_path),
            "--json",
            "--output", str(out_json),
        ]
    code, out, err = run_py(
        EVAL_KIT / "repo_evaluator.py",
        args,
        timeout=7200,
        cwd=EVAL_KIT,
    )
    if code != 0:
        return False, (out + err)[-2000:]
    return True, f"eval-kit ok -> {out_json}"


def run_repo_analyzer(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    """LLM-usage / training-data-quality / CI report, run against the local
    clone (no extra API calls — same clone the other phases already use)."""
    out_dir = ctx.repo_analyzer_dir / entry.batch_org / entry.short_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / f"{entry.short_name}.csv"

    args = [
        "--provider", "local",
        "--path", str(clone_path),
        "--name", entry.full_name,
        "--output", str(out_csv),
    ]
    code, out, err = run_py(
        REPO_ANALYZER_SCRIPT,
        args,
        timeout=3600,
        cwd=CODING,
    )
    if code != 0:
        return False, (out + err)[-2000:]
    return True, f"repo-analyzer ok -> {out_csv}"


def run_quality_score(entry: RepoEntry, clone_path: Path, ctx: RunContext) -> tuple[bool, str]:
    _ensure_quality_imports()
    from agent_rubric_scorer import (  # noqa: WPS433
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


def run_pr_task_profile(
    ctx: RunContext,
    log: PipelineLogger,
    entries: list[RepoEntry],
) -> dict[str, Any]:
    log.info("Phase: PR task-profile report (rules + LLM)")
    ctx.task_profile_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    gh_token = ctx.tokens.get(ctx.github_token_name, "")
    gl_token = ctx.tokens.get(GITLAB_TOKEN_NAME, "")
    if gh_token:
        env["GITHUB_TOKEN"] = gh_token
    if gl_token:
        env["GITLAB_TOKEN"] = gl_token

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

    try:
        proc = subprocess.run(
            [PYTHON_BIN, str(TASK_PROFILE_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=86400,
            env=env,
            cwd=str(CODING),
        )
    except subprocess.TimeoutExpired:
        log.phase_log(ctx.logs_dir / "pr-task-profile.log", "timeout after 24h")
        return {"ok": False, "error": "timeout"}

    detail = (proc.stdout or "") + (proc.stderr or "")
    log.phase_log(ctx.logs_dir / "pr-task-profile.log", detail[-50000:])

    if proc.returncode != 0:
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


def process_repo(entry: RepoEntry, ctx: RunContext, log: PipelineLogger) -> dict[str, Any]:
    repo_log = ctx.repo_log_dir(entry)
    status: dict[str, Any] = {
        "full_name": entry.full_name,
        "platform": entry.platform,
        "short_name": entry.short_name,
        "phases": {},
    }

    def record(phase: str, ok: bool, detail: str, attempts: int) -> None:
        status["phases"][phase] = {
            "ok": ok,
            "attempts": attempts,
            "detail": detail[-1000:],
        }
        log.phase_log(repo_log / f"{phase}.log", detail)

    # Clone (skipped for local repos — use existing checkout)
    if entry.is_local and entry.local_path:
        clone_path = entry.local_path
        status["repo_path"] = str(clone_path)
        record("clone", True, f"using local path {clone_path}", 1)
    else:
        def do_clone() -> tuple[bool, str]:
            ok, msg, path = fresh_clone(entry, ctx)
            if ok and path:
                status["clone_path"] = str(path)
            return ok, msg

        ok, detail, attempts = with_retries(do_clone, ctx.retries, "clone")
        record("clone", ok, detail, attempts)
        if not ok:
            status["overall"] = "failed"
            return status
        clone_path = ctx.clone_path(entry)

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
        ok, detail, attempts = with_retries(fn, ctx.retries, phase_name)
        record(phase_name, ok, detail, attempts)
        if not ok:
            log.info(f"  {entry.full_name}: {phase_name} failed after {attempts} attempt(s)")

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

    unwrap = QUALITY_AGENT / "unwrap.py"
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


def remove_clones(ctx: RunContext, log: PipelineLogger) -> None:
    """Delete cloned repos after processing — they are not part of deliverables."""
    if not ctx.clones_dir.exists():
        return
    try:
        shutil.rmtree(ctx.clones_dir)
        log.info(f"Removed clones directory: {ctx.clones_dir}")
    except OSError as exc:
        log.error(f"Failed to remove clones directory: {exc}")


def create_run_zip(run_dir: Path) -> Path:
    zip_name = f"{run_dir.name}.zip"
    zip_path = run_dir / zip_name
    skip_top_dirs = {"clones"}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or path == zip_path:
                continue
            rel = path.relative_to(run_dir)
            if rel.parts and rel.parts[0] in skip_top_dirs:
                continue
            zf.write(path, rel)
    return zip_path


def build_run_context(args: argparse.Namespace, include_quality_score: bool) -> RunContext:
    tokens = parse_tokens_file(Path(args.tokens_file))
    gitlab_projects: list[str] = []
    github_repos: list[str] = []
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

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if platform == "gitlab-project" and len(gitlab_projects) > 1:
        run_label = f"gitlab-projects-{len(gitlab_projects)}"
    elif platform == "github-repo" and len(github_repos) > 1:
        run_label = f"github-repos-{len(github_repos)}"
    else:
        run_label = safe_filename(target.replace("/", "_").replace(" ", "_"))
    run_name = f"org-pipeline-{run_label}-{stamp}"
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
        repo_analyzer_dir=run_dir / "repo-analyzer",
        quality_dir=run_dir / "repo-quality-score",
        task_profile_dir=run_dir / "pr-task-profile",
        include_quality_score=include_quality_score,
        logs_dir=run_dir / "logs",
        tokens=tokens,
        workers=args.workers,
        retries=args.retries,
        clone_depth=args.clone_depth if args.clone_depth > 0 else None,
        github_host=args.github_host,
        gitlab_host=args.gitlab_host,
        github_token_name=args.github_token_name,
        local_repos_dir=local_repos_dir,
        repos_manifest=repos_manifest,
        gitlab_projects=gitlab_projects,
        github_repos=github_repos,
        profiler_template=profiler_template,
        profiler_out=run_dir / "codebase-profiler" / "codebase_sheet.filled.xlsx",
        pipeline_log=run_dir / "logs" / "pipeline.log",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run merged PR counts, PR task-profile, codebase profiler, eval-kit, "
            "and optionally repo-quality-score for one org/group."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--github-org", help="GitHub organization to process")
    target.add_argument(
        "--github-repo",
        action="append",
        metavar="OWNER/REPO",
        help=(
            "Single GitHub repo path (repeatable, or comma-separated). "
            "Example: --github-repo data-tech/frontend --github-repo data-tech/backend"
        ),
    )
    target.add_argument("--gitlab-group", help="GitLab top-level group to process")
    target.add_argument(
        "--gitlab-project",
        action="append",
        metavar="GROUP/PROJECT",
        help=(
            "GitLab project path (repeatable, or comma-separated). "
            "Example: --gitlab-project my-group/repo-a --gitlab-project my-group/repo-b"
        ),
    )
    target.add_argument(
        "--local-repos-dir",
        help="Directory containing one repo per subfolder (downloaded/local checkouts)",
    )

    parser.add_argument("--tokens-file", default="tokens", help="Path to tokens file")
    parser.add_argument(
        "--repos-manifest",
        help="Optional JSON map of folder_name -> owner/repo (or gitlab:group/repo) "
        "for API-backed PR analysis on local clones",
    )
    parser.add_argument(
        "--local-batch-name",
        default="local",
        help="Batch label for local runs (output paths and run folder name)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(CODING / "outputs" / "org-pipeline-runs"),
        help="Parent directory for timestamped run folders",
    )
    parser.add_argument("--workers", type=int, default=10, help="Parallel repo workers")
    parser.add_argument("--retries", type=int, default=3, help="Retries per repo per phase")
    parser.add_argument(
        "--clone-depth",
        type=int,
        default=0,
        help="Git clone depth (0 = full clone, default)",
    )
    parser.add_argument("--github-host", default="github.com")
    parser.add_argument("--gitlab-host", default="gitlab.com")
    parser.add_argument(
        "--github-token-name",
        default=GITHUB_TOKEN_NAME,
        help=f"Key in tokens file for GitHub API (default: {GITHUB_TOKEN_NAME})",
    )
    return parser.parse_args()


def run_pipeline(include_quality_score: bool = True) -> int:
    args = parse_args()
    ctx = build_run_context(args, include_quality_score=include_quality_score)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    ctx.profiler_dir.mkdir(parents=True, exist_ok=True)

    log = PipelineLogger(ctx.pipeline_log)
    started = datetime.now(timezone.utc).isoformat()
    log.info(f"Starting org pipeline: {ctx.platform}={ctx.target}")
    log.info(f"Include repo-quality-score: {ctx.include_quality_score}")
    log.info(f"Python: {PYTHON_BIN}")
    log.info(f"Run directory: {ctx.run_dir}")
    if ctx.clones_dir != ctx.run_dir / "clones":
        log.info(f"Clones directory (short path): {ctx.clones_dir}")

    ctx.manifest = {
        "started_at": started,
        "platform": ctx.platform,
        "target": ctx.target,
        "workers": ctx.workers,
        "retries": ctx.retries,
        "clone_depth": ctx.clone_depth,
        "include_quality_score": ctx.include_quality_score,
        "repos": [],
        "phases": {},
    }
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
        entries = discover_repos(ctx, log)
        ctx.manifest["repo_count"] = len(entries)

        if ctx.platform == "local":
            log.info("Phase 1: merged PR counts skipped (local mode)")
            ctx.manifest["phases"]["merged-pr-counts"] = {"skipped": True, "reason": "local mode"}
        else:
            pr_summary = run_merged_pr_counts(ctx, log)
            ctx.manifest["phases"]["merged-pr-counts"] = pr_summary

        task_profile_summary = run_pr_task_profile(ctx, log, entries)
        ctx.manifest["phases"]["pr-task-profile"] = task_profile_summary

        log.info(f"Per-repo phases: processing {len(entries)} repos with {ctx.workers} workers")
        repo_results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=ctx.workers) as pool:
            futures = {pool.submit(process_repo, e, ctx, log): e for e in entries}
            for i, fut in enumerate(as_completed(futures), 1):
                entry = futures[fut]
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
                if i % 5 == 0 or i == len(entries):
                    log.info(f"  Progress {i}/{len(entries)} ({ok_count} fully ok)")

        if ctx.include_quality_score:
            aggregate_quality_org(ctx, log)
        else:
            log.info("Repo-quality-score rollup skipped (disabled for this pipeline variant)")

        ctx.manifest["repos"] = sorted(repo_results, key=lambda r: r.get("full_name", ""))
        ctx.manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        ctx.manifest["summary"] = {
            "total": len(entries),
            "fully_ok": sum(1 for r in repo_results if r.get("overall") == "ok"),
            "partial": sum(1 for r in repo_results if r.get("overall") == "partial"),
            "failed": sum(1 for r in repo_results if r.get("overall") in ("failed", "error")),
        }

        manifest_path = ctx.run_dir / "manifest.json"
        manifest_path.write_text(json.dumps(ctx.manifest, indent=2), encoding="utf-8")

        remove_clones(ctx, log)
        if ctx.platform != "local":
            ctx.manifest["clones_removed"] = True
        else:
            ctx.manifest["clones_removed"] = False
            ctx.manifest["local_source_preserved"] = True
        manifest_path.write_text(json.dumps(ctx.manifest, indent=2), encoding="utf-8")

        zip_path = create_run_zip(ctx.run_dir)
        log.info(f"Run complete. Manifest: {manifest_path}")
        log.info(f"Zip: {zip_path}")
        log.info(
            f"Summary: {ctx.manifest['summary']['fully_ok']} ok, "
            f"{ctx.manifest['summary']['partial']} partial, "
            f"{ctx.manifest['summary']['failed']} failed"
        )
        return 0
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


if __name__ == "__main__":
    raise SystemExit(run_pipeline(include_quality_score=True))
