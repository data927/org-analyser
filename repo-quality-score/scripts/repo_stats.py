#!/usr/bin/env python3
"""
repo_stats.py — static codebase metadata collector for the repo-quality-score skill.

Usage:
    python repo_stats.py <repo-path> [--top-files N]

Positional arguments:
    repo-path           Path to the repository (or sub-directory) to analyze.

Options:
    --top-files N       Number of largest source files to report (default 10).

Environment variables: none. This script reads no secrets and no credentials. It
only opens files read-only with bounded reads, never executes code, never installs
dependencies, and never touches the network.

Emits JSON to stdout. Works on any language/framework by pattern-matching well-known
file structures.

Covers:
- Language detection and LOC breakdown
- Framework detection
- File size distribution (median, p90, top-N largest)
- Code-file and test-file counts and test:source ratio
- Dependency count from manifests and lockfiles
- CI/CD config detection
- Security signal scanning (hardcoded secrets, .env files)
- Linting/formatting config detection
- Documentation signals (README, CHANGELOG, CONTRIBUTING)
- Observability signals
- Coverage tooling detection
- Class signals (frontend / backend / ML / AI-research / data-eng / security / infra)
  used by classify_repo.py to assign the repo to one or more classes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Language detection by file extension
# ---------------------------------------------------------------------------

LANGUAGE_BY_EXT: dict[str, str] = {
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".py": "Python",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".h": "C/C++",
    ".swift": "Swift",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".scala": "Scala",
    ".clj": "Clojure",
    ".hs": "Haskell",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".sql": "SQL",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
    ".less": "CSS",
    ".html": "HTML",
    ".htm": "HTML",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".tf": "Terraform",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".toml": "TOML",
    ".md": "Markdown",
    ".mdx": "Markdown",
}

# Extensions that are code (contribute to LOC, file counts, etc.)
CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".go", ".rs", ".java", ".kt", ".kts",
    ".rb", ".php", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h",
    ".swift", ".dart", ".ex", ".exs", ".erl", ".hrl",
    ".scala", ".clj", ".hs", ".sh", ".bash", ".zsh",
    ".sql", ".vue", ".svelte",
}

# Directories to always skip
SKIP_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build", "out",
    ".cache", "__pycache__", ".pytest_cache", ".mypy_cache",
    "venv", ".venv", "env", ".env", "target",  # Rust target
    ".next", ".nuxt", ".output", ".turbo",
    "coverage", ".nyc_output", "htmlcov",
    "migrations",  # usually generated
    "generated", "gen", "__generated__",
    ".tox", "eggs", "*.egg-info",
}

# Files/patterns to exclude from "source" counts (generated / vendored)
GENERATED_PATTERNS = [
    re.compile(r"// Code generated", re.I),
    re.compile(r"# DO NOT EDIT", re.I),
    re.compile(r"# This file is auto-generated", re.I),
    re.compile(r"// AUTO-GENERATED", re.I),
    re.compile(r"/* eslint-disable \*/"),
]

# Test file patterns
TEST_PATTERNS = [
    re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs)$", re.I),
    re.compile(r"(^|/)test_[^/]+\.(py)$", re.I),
    re.compile(r"(^|/)[^/]+_test\.(go|py|rb)$", re.I),
    re.compile(r"(^|/)__tests__/", re.I),
    re.compile(r"(^|/)spec/[^/]+\.(rb|js|ts)$", re.I),
    re.compile(r"[A-Z][^/]*Test[s]?\.(java|kt|cs)$"),
    re.compile(r"\.(test|spec)\.rs$", re.I),
]

FIXTURE_PATTERNS = [
    re.compile(r"(^|/)tests?/fixtures?/", re.I),
    re.compile(r"(^|/)__snapshots__/", re.I),
    re.compile(r"(^|/)__mocks__/", re.I),
    re.compile(r"\.snap$", re.I),
    re.compile(r"\.fixture\.(json|ts|js|yaml)$", re.I),
]

# ---------------------------------------------------------------------------
# Framework detection (by file presence)
# ---------------------------------------------------------------------------

FRAMEWORK_MARKERS: list[tuple[str, str]] = [
    # File pattern → framework name
    ("next.config.js", "Next.js"),
    ("next.config.ts", "Next.js"),
    ("next.config.mjs", "Next.js"),
    ("nuxt.config.ts", "Nuxt"),
    ("nuxt.config.js", "Nuxt"),
    ("svelte.config.js", "SvelteKit"),
    ("svelte.config.ts", "SvelteKit"),
    ("angular.json", "Angular"),
    ("vite.config.ts", "Vite"),
    ("vite.config.js", "Vite"),
    ("remix.config.js", "Remix"),
    ("remix.config.ts", "Remix"),
    ("astro.config.mjs", "Astro"),
    ("astro.config.ts", "Astro"),
    ("gatsby-config.js", "Gatsby"),
    ("gatsby-config.ts", "Gatsby"),
    ("vue.config.js", "Vue CLI"),
    ("manage.py", "Django"),
    ("settings.py", "Django"),  # usually inside a package
    ("wsgi.py", "WSGI (Flask/Django)"),
    ("asgi.py", "ASGI (FastAPI/Django Channels)"),
    ("Cargo.toml", "Rust (Cargo)"),
    ("go.mod", "Go Modules"),
    ("pom.xml", "Maven (Java)"),
    ("build.gradle", "Gradle"),
    ("build.gradle.kts", "Gradle (Kotlin DSL)"),
    ("Gemfile", "Ruby (Bundler)"),
    ("composer.json", "PHP (Composer)"),
    ("pubspec.yaml", "Flutter/Dart"),
    ("mix.exs", "Elixir (Mix)"),
    ("rebar.config", "Erlang (Rebar)"),
    ("project.clj", "Clojure (Leiningen)"),
    ("stack.yaml", "Haskell (Stack)"),
    ("cabal.project", "Haskell (Cabal)"),
    ("flake.nix", "Nix"),
    ("Makefile", "Make"),
    ("CMakeLists.txt", "CMake"),
    ("terraform.tfstate", "Terraform"),
    ("main.tf", "Terraform"),
    ("serverless.yml", "Serverless Framework"),
    ("serverless.yaml", "Serverless Framework"),
    ("amplify.yml", "AWS Amplify"),
    ("vercel.json", "Vercel"),
    ("netlify.toml", "Netlify"),
    ("fly.toml", "Fly.io"),
    ("helm/Chart.yaml", "Helm"),
    ("Chart.yaml", "Helm"),
    ("docker-compose.yml", "Docker Compose"),
    ("docker-compose.yaml", "Docker Compose"),
    ("Dockerfile", "Docker"),
    (".devcontainer/devcontainer.json", "Dev Container"),
    ("devcontainer.json", "Dev Container"),
]

# Dep manifest → package manager name
MANIFEST_TO_PM: dict[str, str] = {
    "package.json": "npm/yarn/pnpm",
    "pyproject.toml": "pip/poetry/uv",
    "requirements.txt": "pip",
    "Pipfile": "pipenv",
    "go.mod": "Go modules",
    "Cargo.toml": "Cargo",
    "Gemfile": "Bundler",
    "composer.json": "Composer",
    "pom.xml": "Maven",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    "pubspec.yaml": "pub (Dart)",
    "mix.exs": "Mix (Elixir)",
    "Package.swift": "Swift Package Manager",
}

LOCKFILE_PATTERNS = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "uv.lock",
    "go.sum", "Cargo.lock", "Gemfile.lock",
    "composer.lock", "packages.lock.json", "gradle.lockfile",
    "pubspec.lock",
]

# CI config patterns
CI_CONFIGS: list[tuple[str, str]] = [
    (".github/workflows", "GitHub Actions"),
    (".circleci/config.yml", "CircleCI"),
    (".gitlab-ci.yml", "GitLab CI"),
    ("Jenkinsfile", "Jenkins"),
    ("azure-pipelines.yml", "Azure Pipelines"),
    (".travis.yml", "Travis CI"),
    ("bitbucket-pipelines.yml", "Bitbucket Pipelines"),
    (".buildkite/pipeline.yml", "Buildkite"),
    ("cloudbuild.yaml", "GCP Cloud Build"),
    (".woodpecker.yml", "Woodpecker CI"),
    (".drone.yml", "Drone CI"),
]

# Linting/formatting configs
LINT_CONFIGS = {
    ".eslintrc.js": "ESLint", ".eslintrc.cjs": "ESLint", ".eslintrc.ts": "ESLint",
    ".eslintrc.json": "ESLint", ".eslintrc.yaml": "ESLint", "eslint.config.js": "ESLint",
    "eslint.config.mjs": "ESLint", "eslint.config.ts": "ESLint",
    ".prettierrc": "Prettier", ".prettierrc.js": "Prettier", ".prettierrc.json": "Prettier",
    ".prettierrc.yaml": "Prettier",
    "ruff.toml": "Ruff", ".ruff.toml": "Ruff",
    ".pylintrc": "Pylint", "pylintrc": "Pylint",
    "pyproject.toml": "Ruff/Black (check content)",  # may contain [tool.ruff] etc.
    ".flake8": "Flake8", "setup.cfg": "Flake8 (check content)",
    "golangci-lint.yml": "golangci-lint", ".golangci.yml": "golangci-lint",
    ".golangci.yaml": "golangci-lint",
    ".rubocop.yml": "RuboCop",
    "phpcs.xml": "PHP_CodeSniffer",
    ".editorconfig": "EditorConfig",
    "biome.json": "Biome", "biome.jsonc": "Biome",
    ".oxlintrc": "Oxlint",
}

# Test framework detection (by dep name and config file)
TEST_FRAMEWORK_SIGNALS = {
    "vitest": "vitest", "jest": "jest", "mocha": "mocha", "jasmine": "jasmine",
    "ava": "ava", "tape": "tape", "qunit": "qunit", "cypress": "Cypress",
    "playwright": "Playwright", "@playwright/test": "Playwright",
    "pytest": "pytest", "unittest": "unittest (stdlib)", "nose2": "nose2",
    "rspec": "RSpec", "minitest": "Minitest",
    "go test": "go test", "testing": "go test",
    "JUnit": "JUnit", "TestNG": "TestNG",
    "RustTest": "rust built-in tests",
    "PHPUnit": "PHPUnit",
    "NUnit": "NUnit", "xUnit": "xUnit",
}

# Security scan patterns (file-level)
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.I),                        # OpenAI key
    re.compile(r"AKIA[A-Z0-9]{16}"),                                   # AWS access key
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                               # GitHub PAT
    re.compile(r"ghs_[A-Za-z0-9]{36}"),                               # GitHub App secret
    re.compile(r"xoxb-[A-Za-z0-9\-]{24,}", re.I),                    # Slack bot token
    re.compile(r"AIza[A-Za-z0-9_\-]{35}"),                            # Google API key
    re.compile(r'password\s*[=:]\s*["\'][^"\']{4,}["\']', re.I),     # hardcoded password
    re.compile(r'api[_-]?key\s*[=:]\s*["\'][^"\']{8,}["\']', re.I), # api_key = "..."
    re.compile(r'secret[_-]?key\s*[=:]\s*["\'][^"\']{8,}["\']', re.I),
    re.compile(r'private[_-]?key\s*[=:]\s*["\'][^"\']{8,}["\']', re.I),
    re.compile(r'bearer\s+[A-Za-z0-9\-_\.]{20,}', re.I),
]


def mask_secrets(text: str) -> str:
    """Replace all detected secrets with [REDACTED] in text."""
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text

# Observability signal patterns (grep in source)
LOGGING_FRAMEWORKS = {
    "js": ["pino", "winston", "bunyan", "log4js", "loglevel", "@nestjs/common"],
    "py": ["structlog", "loguru", "logging.getLogger", "logzero"],
    "go": ["zerolog", "zap", "logrus", "slog"],
    "java": ["slf4j", "log4j", "logback"],
    "ruby": ["Rails.logger", "Logger.new"],
}

ERROR_TRACKING_LIBS = ["@sentry/", "sentry-sdk", "sentry_sdk", "rollbar", "honeybadger", "bugsnag", "airbrake"]

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def count_lines(path: Path, max_lines: int = 50_000) -> int:
    """Count non-empty lines in a file. Returns 0 on binary/error."""
    try:
        count = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    return max_lines
                if line.strip():
                    count += 1
        return count
    except (OSError, PermissionError):
        return 0


def is_generated(path: Path) -> bool:
    """Heuristic: is this a generated file?"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(500)
        return any(p.search(head) for p in GENERATED_PATTERNS)
    except (OSError, PermissionError):
        return False


def is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in TEST_PATTERNS)


def is_fixture_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in FIXTURE_PATTERNS)


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".") and name not in {".github", ".gitlab", ".circleci", ".devcontainer"}


def walk_source_files(root: Path) -> list[tuple[Path, str]]:
    """Yield (abs_path, rel_path) for every non-skipped source file."""
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for fname in filenames:
            abs_path = Path(dirpath) / fname
            rel_path = str(abs_path.relative_to(root))
            results.append((abs_path, rel_path))
    return results


def read_file_safe(path: Path, max_bytes: int = 16_384) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(max_bytes)
    except (OSError, PermissionError):
        return ""


# ---------------------------------------------------------------------------
# Core analysis functions
# ---------------------------------------------------------------------------

def analyze_languages(all_files: list[tuple[Path, str]]) -> dict:
    loc_by_lang: dict[str, int] = defaultdict(int)
    file_count_by_lang: dict[str, int] = defaultdict(int)

    for abs_path, rel_path in all_files:
        ext = abs_path.suffix.lower()
        if ext not in CODE_EXTENSIONS:
            continue
        if is_generated(abs_path):
            continue
        lang = LANGUAGE_BY_EXT.get(ext, "Other")
        lines = count_lines(abs_path)
        loc_by_lang[lang] += lines
        file_count_by_lang[lang] += 1

    total_loc = sum(loc_by_lang.values())
    sorted_langs = sorted(loc_by_lang.items(), key=lambda x: x[1], reverse=True)

    primary = sorted_langs[0][0] if sorted_langs else "Unknown"
    secondary = [lang for lang, _ in sorted_langs[1:4]] if len(sorted_langs) > 1 else []

    return {
        "primary_language": primary,
        "secondary_languages": secondary,
        "total_loc": total_loc,
        "loc_by_language": dict(sorted_langs),
        "file_count_by_language": dict(file_count_by_lang),
    }


def analyze_file_sizes(all_files: list[tuple[Path, str]], top_n: int = 10) -> dict:
    sizes: list[tuple[int, str]] = []
    generated_excluded = 0

    for abs_path, rel_path in all_files:
        ext = abs_path.suffix.lower()
        if ext not in CODE_EXTENSIONS:
            continue
        if is_generated(abs_path):
            generated_excluded += 1
            continue
        loc = count_lines(abs_path)
        if loc > 0:
            sizes.append((loc, rel_path))

    if not sizes:
        return {"total_source_files": 0, "median_loc": 0, "p90_loc": 0,
                "largest_files": [], "generated_excluded": 0}

    sizes.sort(key=lambda x: x[0], reverse=True)
    locs = sorted([s[0] for s in sizes])
    n = len(locs)
    median = locs[n // 2]
    p90 = locs[int(n * 0.9)]

    return {
        "total_source_files": n,
        "median_loc": median,
        "p90_loc": p90,
        "largest_files": [{"path": p, "loc": l} for l, p in sizes[:top_n]],
        "god_files_over_500": sum(1 for l, _ in sizes if l > 500),
        "god_files_over_1000": sum(1 for l, _ in sizes if l > 1000),
        "generated_excluded": generated_excluded,
    }


def analyze_tests(all_files: list[tuple[Path, str]]) -> dict:
    spec_files: list[str] = []
    fixture_files: list[str] = []

    for abs_path, rel_path in all_files:
        if not is_test_file(rel_path):
            continue
        loc = count_lines(abs_path)
        if is_fixture_file(rel_path) or loc > 5000:
            fixture_files.append(rel_path)
        else:
            spec_files.append(rel_path)

    return {
        "spec_files": len(spec_files),
        "fixture_and_snapshot_files": len(fixture_files),
        "total_test_files": len(spec_files) + len(fixture_files),
        "spec_file_paths_sample": spec_files[:20],
    }


def analyze_frameworks(root: Path) -> list[str]:
    detected = []
    for marker, framework in FRAMEWORK_MARKERS:
        if "/" in marker:
            if (root / marker).exists():
                detected.append(framework)
        else:
            # Check at root and one level deep
            if (root / marker).exists():
                detected.append(framework)

    # Check for FastAPI/Flask in requirements
    req_content = read_file_safe(root / "requirements.txt") + read_file_safe(root / "pyproject.toml")
    if "fastapi" in req_content.lower():
        detected.append("FastAPI")
    if re.search(r'\bflask\b', req_content, re.I):
        detected.append("Flask")
    if "sqlalchemy" in req_content.lower():
        detected.append("SQLAlchemy")
    if "pydantic" in req_content.lower():
        detected.append("Pydantic")

    # Check for React (not Next.js/Remix already detected)
    pkg_content = read_file_safe(root / "package.json")
    if pkg_content:
        try:
            pkg = json.loads(pkg_content)
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react" in all_deps and "Next.js" not in detected and "Remix" not in detected and "Gatsby" not in detected:
                detected.append("React")
            if "express" in all_deps:
                detected.append("Express")
            if "@nestjs/core" in all_deps:
                detected.append("NestJS")
            if "hono" in all_deps:
                detected.append("Hono")
            if "elysia" in all_deps:
                detected.append("Elysia")
            if "koa" in all_deps:
                detected.append("Koa")
            if "fastify" in all_deps:
                detected.append("Fastify")
            if "trpc" in str(all_deps) or "@trpc/server" in all_deps:
                detected.append("tRPC")
            if "prisma" in all_deps or "@prisma/client" in all_deps:
                detected.append("Prisma")
            if "drizzle-orm" in all_deps:
                detected.append("Drizzle ORM")
            if "typeorm" in all_deps:
                detected.append("TypeORM")
            if "zod" in all_deps:
                detected.append("Zod")
            if "tailwindcss" in all_deps:
                detected.append("Tailwind CSS")
        except (json.JSONDecodeError, KeyError):
            pass

    return list(dict.fromkeys(detected))  # deduplicate preserving order


def analyze_dependencies(root: Path) -> dict:
    result: dict = {
        "package_managers": [],
        "manifests_found": [],
        "lockfiles_found": [],
        "lockfiles_expected": [],
        "direct_runtime_deps": 0,
        "direct_dev_deps": 0,
        "total_transitive_deps": 0,
        "dep_update_tooling": "none",
        "external_services_detected": [],
    }

    # npm/yarn/pnpm
    pkg_path = root / "package.json"
    if pkg_path.exists():
        result["manifests_found"].append("package.json")
        result["package_managers"].append("npm/yarn/pnpm")
        result["lockfiles_expected"].append("package-lock.json or yarn.lock or pnpm-lock.yaml")
        try:
            pkg = json.loads(read_file_safe(pkg_path))
            result["direct_runtime_deps"] += len(pkg.get("dependencies", {}))
            result["direct_dev_deps"] += len(pkg.get("devDependencies", {}))
        except (json.JSONDecodeError, ValueError):
            pass

    # Python
    for pymanifest in ["pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "setup.cfg"]:
        if (root / pymanifest).exists():
            result["manifests_found"].append(pymanifest)
            if "pip" not in str(result["package_managers"]):
                result["package_managers"].append("pip/poetry/uv")
            result["lockfiles_expected"].append("poetry.lock / Pipfile.lock / uv.lock / requirements.txt (pinned)")
            break

    # Go
    if (root / "go.mod").exists():
        result["manifests_found"].append("go.mod")
        result["package_managers"].append("Go modules")
        result["lockfiles_expected"].append("go.sum")
        go_mod_content = read_file_safe(root / "go.mod")
        # Count "require" entries
        in_require = False
        req_count = 0
        for line in go_mod_content.splitlines():
            line = line.strip()
            if line.startswith("require ("):
                in_require = True
            elif in_require and line == ")":
                in_require = False
            elif in_require and line and not line.startswith("//"):
                req_count += 1
            elif line.startswith("require ") and not line.startswith("require ("):
                req_count += 1
        result["direct_runtime_deps"] += req_count

    # Rust
    if (root / "Cargo.toml").exists():
        result["manifests_found"].append("Cargo.toml")
        result["package_managers"].append("Cargo")
        result["lockfiles_expected"].append("Cargo.lock")

    # Ruby
    if (root / "Gemfile").exists():
        result["manifests_found"].append("Gemfile")
        result["package_managers"].append("Bundler")
        result["lockfiles_expected"].append("Gemfile.lock")

    # PHP
    if (root / "composer.json").exists():
        result["manifests_found"].append("composer.json")
        result["package_managers"].append("Composer")
        result["lockfiles_expected"].append("composer.lock")

    # Detect lockfiles
    for lf in LOCKFILE_PATTERNS:
        for candidate in root.rglob(lf):
            rel = str(candidate.relative_to(root))
            if "node_modules" not in rel and ".git" not in rel:
                result["lockfiles_found"].append(rel)
                break

    # Count transitive deps from lockfiles
    for lf in result["lockfiles_found"]:
        lf_path = root / lf
        if not lf_path.exists():
            continue
        content = read_file_safe(lf_path, max_bytes=1_000_000)
        if lf.endswith("package-lock.json"):
            try:
                data = json.loads(content)
                # v3 lockfile uses "packages"
                pkgs = data.get("packages", data.get("dependencies", {}))
                result["total_transitive_deps"] += len(pkgs)
            except (json.JSONDecodeError, ValueError):
                pass
        elif lf.endswith("yarn.lock"):
            result["total_transitive_deps"] += content.count("\n\n")
        elif lf.endswith("pnpm-lock.yaml"):
            result["total_transitive_deps"] += content.count("\n  /")
        elif lf.endswith("poetry.lock"):
            result["total_transitive_deps"] += content.count("[[package]]")
        elif lf.endswith("go.sum"):
            result["total_transitive_deps"] += content.count("\n") // 2
        elif lf.endswith("Cargo.lock"):
            result["total_transitive_deps"] += content.count("[[package]]")
        elif lf.endswith("Gemfile.lock"):
            # GEM specs section
            in_specs = False
            for line in content.splitlines():
                if line.strip() == "specs:":
                    in_specs = True
                elif in_specs and line.startswith("  ") and not line.startswith("    "):
                    result["total_transitive_deps"] += 1

    # Dep update tooling
    if (root / ".github/dependabot.yml").exists() or (root / ".github/dependabot.yaml").exists():
        result["dep_update_tooling"] = "Dependabot"
    elif (root / "renovate.json").exists() or (root / ".renovaterc").exists() or (root / ".renovaterc.json").exists():
        result["dep_update_tooling"] = "Renovate"

    return result


def analyze_ci(root: Path) -> dict:
    ci_systems = []
    ci_configs = []
    jobs_summary: list[str] = []
    runs_tests = False
    runs_lint = False
    runs_typecheck = False
    has_deploy = False

    for config_path, system in CI_CONFIGS:
        full = root / config_path
        if config_path.endswith("/"):  # directory
            if full.is_dir():
                ci_systems.append(system)
                for f in full.glob("*.yml"):
                    ci_configs.append(str(f.relative_to(root)))
                    content = read_file_safe(f)
                    if re.search(r'\btest\b|\bvitest\b|\bjest\b|\bpytest\b|\bgo test\b|\brspec\b', content, re.I):
                        runs_tests = True
                    if re.search(r'\blint\b|\beslint\b|\bpylint\b|\bruff\b|\bgolangci\b', content, re.I):
                        runs_lint = True
                    if re.search(r'\btsc\b|\btype.?check\b|\bmypy\b|\bpyright\b', content, re.I):
                        runs_typecheck = True
                    if re.search(r'\bdeploy\b|\brelease\b|\bpublish\b|\bpush.*ecr\b|\bpush.*registry\b', content, re.I):
                        has_deploy = True
        elif full.exists():
            ci_systems.append(system)
            ci_configs.append(config_path)
            content = read_file_safe(full)
            if re.search(r'\btest\b|\bvitest\b|\bjest\b|\bpytest\b|\bgo test\b|\brspec\b', content, re.I):
                runs_tests = True
            if re.search(r'\blint\b|\beslint\b|\bpylint\b|\bruff\b|\bgolangci\b', content, re.I):
                runs_lint = True
            if re.search(r'\btsc\b|\btype.?check\b|\bmypy\b|\bpyright\b', content, re.I):
                runs_typecheck = True
            if re.search(r'\bdeploy\b|\brelease\b|\bpublish\b', content, re.I):
                has_deploy = True

    # Also check .github/workflows/ explicitly
    gha_dir = root / ".github" / "workflows"
    if gha_dir.is_dir() and "GitHub Actions" not in ci_systems:
        ci_systems.append("GitHub Actions")
        for f in gha_dir.glob("*.yml"):
            ci_configs.append(str(f.relative_to(root)))
            content = read_file_safe(f)
            if re.search(r'\btest\b|\bvitest\b|\bjest\b|\bpytest\b|\bgo test\b', content, re.I):
                runs_tests = True
            if re.search(r'\blint\b|\beslint\b', content, re.I):
                runs_lint = True
            if re.search(r'\btsc\b|\btype.?check\b|\bmypy\b', content, re.I):
                runs_typecheck = True
            if re.search(r'\bdeploy\b|\brelease\b|\bpublish\b', content, re.I):
                has_deploy = True

    return {
        "ci_systems": list(dict.fromkeys(ci_systems)),
        "ci_configs": ci_configs,
        "runs_tests": runs_tests,
        "runs_lint": runs_lint,
        "runs_typecheck": runs_typecheck,
        "has_deploy_pipeline": has_deploy,
        "ci_present": len(ci_systems) > 0,
    }


def analyze_security(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    secret_hits: list[dict] = []
    env_committed: list[str] = []
    dep_audit_in_ci = False
    input_validation_patterns: list[str] = []

    # Check for committed .env files
    for abs_path, rel_path in source_files:
        name = abs_path.name
        if name == ".env" or (name.startswith(".env.") and "example" not in name.lower() and "sample" not in name.lower() and "template" not in name.lower()):
            env_committed.append(rel_path)

    # Scan source (non-test) files for secrets, capping at 500 files to stay fast
    scan_files = [(p, r) for p, r in source_files
                  if p.suffix.lower() in CODE_EXTENSIONS
                  and not is_test_file(r)
                  and ".env" not in r][:500]

    for abs_path, rel_path in scan_files:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    for pat in SECRET_PATTERNS:
                        if pat.search(line):
                            hit_text = line.strip()[:120]
                            # Mask secrets before adding to artifact
                            hit_text = mask_secrets(hit_text)
                            secret_hits.append({
                                "file": rel_path,
                                "line": i,
                                "snippet": hit_text,
                            })
                            break  # one hit per line is enough
                    if len(secret_hits) >= 20:
                        break
        except (OSError, PermissionError):
            pass
        if len(secret_hits) >= 20:
            break

    # Check dep audit in CI
    for config_path, _ in CI_CONFIGS:
        full = root / config_path
        if full.exists():
            content = read_file_safe(full)
            if re.search(r'\baudit\b|\bsnyk\b|\btrivy\b|\bgrype\b|\bsafety\b|\bpip.audit\b', content, re.I):
                dep_audit_in_ci = True
                break
    # Also check .github/workflows
    gha = root / ".github" / "workflows"
    if gha.is_dir():
        for f in gha.glob("*.yml"):
            if re.search(r'\baudit\b|\bsnyk\b|\btrivy\b|\bgrype\b', read_file_safe(f), re.I):
                dep_audit_in_ci = True

    # Input validation signals
    all_code = []
    for abs_path, rel_path in scan_files[:100]:
        content = read_file_safe(abs_path, max_bytes=4096)
        all_code.append(content)
    combined = "\n".join(all_code)
    if re.search(r'\bzod\b|z\.object|z\.string', combined):
        input_validation_patterns.append("zod")
    if re.search(r'\bpydantic\b|BaseModel', combined):
        input_validation_patterns.append("pydantic")
    if re.search(r'\bjoi\b|Joi\.object', combined):
        input_validation_patterns.append("joi")
    if re.search(r'\byup\b', combined):
        input_validation_patterns.append("yup")
    if re.search(r'\bclass-validator\b|@IsString|@IsNotEmpty', combined):
        input_validation_patterns.append("class-validator")
    if re.search(r'\bvalibot\b', combined):
        input_validation_patterns.append("valibot")
    if re.search(r'\bjsonschema\b|jsonschema\.validate', combined):
        input_validation_patterns.append("jsonschema")

    return {
        "hardcoded_secret_hits": len(secret_hits),
        "secret_hit_details": secret_hits[:5],
        "env_files_committed": env_committed,
        "dep_audit_in_ci": dep_audit_in_ci,
        "input_validation_patterns": input_validation_patterns,
    }


def analyze_lint_config(root: Path) -> dict:
    detected: dict[str, str] = {}
    for filename, tool in LINT_CONFIGS.items():
        if (root / filename).exists():
            # For pyproject.toml, check if it actually contains lint config
            if filename == "pyproject.toml":
                content = read_file_safe(root / filename)
                for linter in ["ruff", "black", "pylint", "flake8", "mypy", "pyright"]:
                    if f"[tool.{linter}]" in content:
                        detected[linter] = filename
            elif filename == "setup.cfg":
                content = read_file_safe(root / filename)
                if "[flake8]" in content:
                    detected["flake8"] = filename
            else:
                detected[tool] = filename
    return {"linters_and_formatters": detected, "has_lint_config": len(detected) > 0}


def analyze_test_framework(root: Path) -> dict:
    frameworks: list[str] = []
    config_files: list[str] = []
    coverage_tooling = None
    coverage_threshold = None

    # JS/TS
    for config in ["vitest.config.ts", "vitest.config.js", "vitest.config.mjs",
                   "jest.config.ts", "jest.config.js", "jest.config.cjs", "jest.config.json",
                   "playwright.config.ts", "playwright.config.js",
                   "cypress.config.ts", "cypress.config.js",
                   "karma.conf.js", ".mocharc.js", ".mocharc.json", ".mocharc.yaml"]:
        if (root / config).exists():
            config_files.append(config)
            if "vitest" in config:
                frameworks.append("vitest")
            elif "jest" in config:
                frameworks.append("jest")
            elif "playwright" in config:
                frameworks.append("Playwright")
            elif "cypress" in config:
                frameworks.append("Cypress")
            elif "karma" in config:
                frameworks.append("Karma")
            elif "mocha" in config:
                frameworks.append("Mocha")

    # Python
    for config in ["pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"]:
        if (root / config).exists():
            content = read_file_safe(root / config)
            if "[tool.pytest" in content or "[pytest]" in content or "pytest" in content:
                if "pytest" not in frameworks:
                    frameworks.append("pytest")
                    config_files.append(config)

    # Go
    if (root / "go.mod").exists():
        frameworks.append("go test")

    # Rust
    if (root / "Cargo.toml").exists():
        frameworks.append("cargo test")

    # Ruby
    if (root / ".rspec").exists() or (root / "spec").is_dir():
        frameworks.append("RSpec")
    if (root / "test").is_dir() and (root / "Gemfile").exists():
        frameworks.append("Minitest")

    # PHP
    if (root / "phpunit.xml").exists() or (root / "phpunit.xml.dist").exists():
        frameworks.append("PHPUnit")

    # Coverage tooling
    pkg_content = read_file_safe(root / "package.json")
    if "c8" in pkg_content or '"coverage"' in pkg_content:
        coverage_tooling = "c8/v8"
    elif "nyc" in pkg_content or "istanbul" in pkg_content:
        coverage_tooling = "NYC/Istanbul"
    elif "lcov" in pkg_content:
        coverage_tooling = "lcov"

    for config_file in ["vitest.config.ts", "vitest.config.js", "jest.config.ts", "jest.config.js"]:
        content = read_file_safe(root / config_file)
        if "coverage" in content:
            if not coverage_tooling:
                coverage_tooling = "configured in " + config_file
            m = re.search(r'threshold.*?lines.*?:.*?(\d+)', content, re.S)
            if m:
                coverage_threshold = int(m.group(1))

    pyproject = read_file_safe(root / "pyproject.toml")
    if "coverage" in pyproject or "pytest-cov" in pyproject:
        coverage_tooling = coverage_tooling or "pytest-cov"

    return {
        "frameworks": list(dict.fromkeys(frameworks)),
        "config_files": config_files,
        "coverage_tooling": coverage_tooling,
        "coverage_threshold": coverage_threshold,
    }


def analyze_documentation(root: Path) -> dict:
    readme = None
    for name in ["README.md", "README.rst", "README.txt", "README", "readme.md"]:
        if (root / name).exists():
            readme = name
            break

    readme_sections = []
    readme_loc = 0
    if readme:
        content = read_file_safe(root / readme)
        readme_loc = len([l for l in content.splitlines() if l.strip()])
        for section in ["install", "setup", "getting started", "run", "test", "environment", "contributing", "architecture", "usage", "api"]:
            if re.search(section, content, re.I):
                readme_sections.append(section)

    changelog = next((n for n in ["CHANGELOG.md", "CHANGELOG.rst", "CHANGELOG", "CHANGES.md", "HISTORY.md", "RELEASES.md"] if (root / n).exists()), None)
    contributing = next((n for n in ["CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING"] if (root / n).exists()), None)
    has_pr_template = (root / ".github" / "PULL_REQUEST_TEMPLATE.md").exists() or (root / ".github" / "pull_request_template.md").exists()
    has_issue_template = (root / ".github" / "ISSUE_TEMPLATE").is_dir() or (root / ".github" / "issue_template.md").exists()

    return {
        "readme": readme,
        "readme_loc": readme_loc,
        "readme_sections_detected": readme_sections,
        "changelog": changelog,
        "contributing_guide": contributing,
        "has_pr_template": has_pr_template,
        "has_issue_template": has_issue_template,
    }


def analyze_reproducibility(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    has_dockerfile = (root / "Dockerfile").exists() or any((root / f"Dockerfile.{s}").exists() for s in ["dev", "development", "local"])
    has_compose = (root / "docker-compose.yml").exists() or (root / "docker-compose.yaml").exists()
    has_devcontainer = (root / ".devcontainer").is_dir() or (root / "devcontainer.json").exists()
    has_nix = (root / "flake.nix").exists() or (root / "shell.nix").exists()

    # .env.example
    env_example = next((n for n in [".env.example", ".env.template", ".env.sample", ".env.test.example"] if (root / n).exists()), None)

    # Count env var references in source
    env_var_refs: set[str] = set()
    env_vars_in_example: set[str] = set()

    for abs_path, rel_path in source_files:
        if abs_path.suffix.lower() not in CODE_EXTENSIONS:
            continue
        if is_test_file(rel_path):
            continue
        content = read_file_safe(abs_path, max_bytes=8192)
        # JS/TS: process.env.FOO or process.env["FOO"]
        env_var_refs.update(re.findall(r'process\.env\.([A-Z_][A-Z0-9_]+)', content))
        env_var_refs.update(re.findall(r'process\.env\[[\'""]([A-Z_][A-Z0-9_]+)[\'""]', content))
        # Python: os.environ["FOO"] or os.getenv("FOO")
        env_var_refs.update(re.findall(r'os\.environ\.get\([\'"]([A-Z_][A-Z0-9_]+)[\'"]', content))
        env_var_refs.update(re.findall(r'os\.getenv\([\'"]([A-Z_][A-Z0-9_]+)[\'"]', content))
        env_var_refs.update(re.findall(r'os\.environ\[[\'"]([A-Z_][A-Z0-9_]+)[\'"]', content))
        # Go: os.Getenv("FOO")
        env_var_refs.update(re.findall(r'os\.Getenv\("([A-Z_][A-Z0-9_]+)"', content))

    if env_example:
        example_content = read_file_safe(root / env_example)
        for line in example_content.splitlines():
            m = re.match(r'^([A-Z_][A-Z0-9_]+)\s*=', line)
            if m:
                env_vars_in_example.add(m.group(1))

    missing_in_example = sorted(env_var_refs - env_vars_in_example) if env_example else []

    return {
        "has_dockerfile": has_dockerfile,
        "has_docker_compose": has_compose,
        "has_devcontainer": has_devcontainer,
        "has_nix": has_nix,
        "env_example_file": env_example,
        "env_vars_referenced_in_source": sorted(env_var_refs),
        "env_vars_in_example": sorted(env_vars_in_example),
        "env_vars_missing_from_example": missing_in_example[:20],
    }


def analyze_observability(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    logging_framework = None
    error_tracking = None
    has_health_endpoint = False
    has_metrics = False

    pkg_content = read_file_safe(root / "package.json")
    for lib in ["pino", "winston", "bunyan", "log4js"]:
        if lib in pkg_content:
            logging_framework = lib
            break

    req_content = read_file_safe(root / "requirements.txt") + read_file_safe(root / "pyproject.toml")
    if "structlog" in req_content:
        logging_framework = "structlog"
    elif "loguru" in req_content:
        logging_framework = "loguru"

    for lib in ERROR_TRACKING_LIBS:
        if lib in pkg_content or lib.replace("-", "_") in req_content:
            error_tracking = lib
            break

    # Scan source for health/metrics
    scan_count = 0
    for abs_path, rel_path in source_files:
        if abs_path.suffix.lower() not in CODE_EXTENSIONS or is_test_file(rel_path):
            continue
        content = read_file_safe(abs_path, max_bytes=4096)
        if re.search(r'["\'/](health|ping|readiness|liveness)["\']', content, re.I):
            has_health_endpoint = True
        if re.search(r'prometheus|prom-client|metrics|statsd|datadog', content, re.I):
            has_metrics = True
        scan_count += 1
        if scan_count > 200:
            break

    # Check go.mod for logging
    go_mod_content = read_file_safe(root / "go.mod")
    if "zerolog" in go_mod_content:
        logging_framework = "zerolog"
    elif "zap" in go_mod_content:
        logging_framework = "zap"
    elif "logrus" in go_mod_content:
        logging_framework = "logrus"

    return {
        "logging_framework": logging_framework,
        "error_tracking": error_tracking,
        "has_health_endpoint": has_health_endpoint,
        "has_metrics": has_metrics,
    }


def _read_all_dep_text(root: Path) -> str:
    """Concatenate the text of every dependency manifest we know about (lowercased).

    Used for keyword-based class detection (ML libs, data-eng libs, etc.). Reads
    are bounded by read_file_safe's max_bytes cap.
    """
    manifests = [
        "package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml",
        "Pipfile", "setup.py", "setup.cfg", "environment.yml", "environment.yaml",
        "go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml",
        "build.gradle", "build.gradle.kts", "pubspec.yaml",
    ]
    chunks = []
    for m in manifests:
        if (root / m).exists():
            chunks.append(read_file_safe(root / m, max_bytes=32_768))
    return "\n".join(chunks).lower()


# Keyword groups for class detection. Each value is a list of substrings searched
# (case-insensitively) in the concatenated dependency-manifest text.
CLASS_DEP_KEYWORDS: dict[str, list[str]] = {
    "ml_libs": [
        "torch", "pytorch", "tensorflow", "\"jax\"", "jaxlib", "flax",
        "scikit-learn", "sklearn", "keras", "xgboost", "lightgbm", "catboost",
        "transformers", "timm", "onnx", "diffusers", "sentence-transformers",
    ],
    "experiment_tracking": [
        "wandb", "mlflow", "tensorboard", "sacred", "neptune", "clearml",
        "hydra-core", "optuna", "accelerate", "deepspeed", "pytorch-lightning",
        "lightning",
    ],
    "data_eng": [
        "airflow", "dagster", "prefect", "dbt-core", "\"dbt\"", "pyspark",
        "apache-beam", "luigi", "kafka", "confluent-kafka", "great-expectations",
        "great_expectations", "pandera", "\"dask\"", "apache-flink", "kedro",
        "feast", "delta-spark",
    ],
    "security_libs": [
        "bandit", "semgrep", "trufflehog", "nuclei", "scapy", "pwntools",
        "cryptography", "pycryptodome", "paramiko", "python-nmap", "yara-python",
        "volatility", "impacket", "mitmproxy", "angr", "capstone",
    ],
    "backend_frameworks": [
        "express", "fastapi", "flask", "django", "@nestjs", "spring-boot",
        "sinatra", "laravel", "\"koa\"", "fastify", "\"hono\"", "gin-gonic",
        "gofiber", "labstack/echo", "actix-web", "axum", "rocket",
    ],
    "frontend_frameworks": [
        "\"react\"", "react-dom", "\"vue\"", "svelte", "@angular", "\"next\"",
        "\"nuxt\"", "solid-js", "preact", "tailwindcss", "@mui/", "styled-components",
        "@chakra-ui", "remix", "gatsby",
    ],
    "orm_db": [
        "prisma", "drizzle-orm", "typeorm", "sequelize", "sqlalchemy", "mongoose",
        "gorm", "\"pg\"", "psycopg", "mysql", "redis", "mongodb", "alembic",
    ],
    "infra_libs": [
        "pulumi", "boto3", "aws-cdk", "@aws-cdk", "google-cloud", "azure-mgmt",
        "kubernetes", "ansible",
    ],
}


def analyze_class_signals(
    root: Path,
    all_files: list[tuple[Path, str]],
    loc_by_language: dict[str, int],
    frameworks: list[str],
) -> dict:
    """Collect raw signals that classify_repo.py turns into per-class confidences.

    Pure counting and keyword matching — no scoring happens here.
    """
    dep_text = _read_all_dep_text(root)

    keyword_hits: dict[str, list[str]] = {}
    for group, kws in CLASS_DEP_KEYWORDS.items():
        keyword_hits[group] = [kw.strip('"') for kw in kws if kw.lower() in dep_text]

    notebook_count = 0
    tf_count = 0
    dockerfile_count = 0
    sql_file_count = 0
    data_file_count = 0
    ui_component_count = 0  # .tsx/.jsx/.vue/.svelte
    yaml_candidates: list[Path] = []

    for abs_path, rel_path in all_files:
        name = abs_path.name.lower()
        ext = abs_path.suffix.lower()
        if ext == ".ipynb":
            notebook_count += 1
        elif ext == ".tf" or ext == ".tfvars":
            tf_count += 1
        elif ext in (".tsx", ".jsx", ".vue", ".svelte"):
            ui_component_count += 1
        elif ext == ".sql":
            sql_file_count += 1
        elif ext in (".parquet", ".avro", ".orc", ".feather"):
            data_file_count += 1
        if name == "dockerfile" or name.startswith("dockerfile."):
            dockerfile_count += 1
        if ext in (".yaml", ".yml") and len(yaml_candidates) < 400:
            yaml_candidates.append(abs_path)

    # Bounded scan of YAML files for Kubernetes manifests.
    k8s_manifest_count = 0
    for p in yaml_candidates:
        head = read_file_safe(p, max_bytes=2048)
        if "apiVersion:" in head and "kind:" in head:
            k8s_manifest_count += 1

    pulumi_present = (root / "Pulumi.yaml").exists() or "pulumi" in keyword_hits.get("infra_libs", [])
    ansible_present = (
        (root / "ansible.cfg").exists()
        or (root / "playbook.yml").exists()
        or (root / "playbooks").is_dir()
        or "ansible" in keyword_hits.get("infra_libs", [])
    )
    helm_present = "Helm" in frameworks or (root / "Chart.yaml").exists()
    terraform_present = tf_count > 0 or "Terraform" in frameworks

    css_loc = loc_by_language.get("CSS", 0)
    sql_loc = loc_by_language.get("SQL", 0)
    total_loc = sum(loc_by_language.values()) or 1

    return {
        "dep_keyword_hits": keyword_hits,
        "notebook_count": notebook_count,
        "terraform_file_count": tf_count,
        "terraform_present": terraform_present,
        "k8s_manifest_count": k8s_manifest_count,
        "helm_present": helm_present,
        "pulumi_present": pulumi_present,
        "ansible_present": ansible_present,
        "dockerfile_count": dockerfile_count,
        "ui_component_file_count": ui_component_count,
        "sql_file_count": sql_file_count,
        "sql_loc": sql_loc,
        "css_loc": css_loc,
        "css_loc_ratio": round(css_loc / total_loc, 4),
        "data_file_count": data_file_count,
    }


def infer_project_type(root: Path, frameworks: list[str]) -> str:
    has_pkg = (root / "package.json").exists()
    if has_pkg:
        pkg = {}
        try:
            pkg = json.loads(read_file_safe(root / "package.json"))
        except (json.JSONDecodeError, ValueError):
            pass
        is_lib = pkg.get("private") is not True and pkg.get("main") and not any(
            f in frameworks for f in ["Next.js", "Remix", "Gatsby", "Nuxt", "SvelteKit", "Angular"]
        )
        if is_lib:
            return "library"
    if any(f in frameworks for f in ["Next.js", "Remix", "Gatsby", "Nuxt", "SvelteKit", "Angular", "React", "Vue CLI"]):
        return "web app"
    if any(f in frameworks for f in ["Express", "NestJS", "FastAPI", "Flask", "Django", "Hono", "Fastify", "Elysia", "Koa", "tRPC"]):
        return "API service"
    if "Flutter/Dart" in frameworks:
        return "mobile"
    if "Terraform" in frameworks or "Helm" in frameworks or "Fly.io" in frameworks:
        return "infrastructure"
    if any(f in frameworks for f in ["Rust (Cargo)", "Go Modules"]):
        # Could be CLI or library — check for main
        if (root / "main.go").exists() or (root / "cmd").is_dir():
            return "CLI / service"
        if (root / "src" / "main.rs").exists() or (root / "src" / "lib.rs").exists():
            src_main = read_file_safe(root / "src" / "main.rs")
            return "CLI" if "fn main()" in src_main else "library"
    return "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Static metadata collector for repo-quality-score.")
    parser.add_argument("repo", help="Path to the repository")
    parser.add_argument("--top-files", type=int, default=10, help="Number of largest files to report")
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    if not root.exists():
        print(json.dumps({"error": f"path not found: {root}"}))
        return 1

    sys.stderr.write(f"Scanning {root} ...\n")
    all_files = walk_source_files(root)
    sys.stderr.write(f"  Found {len(all_files)} files (pre-filter)\n")

    # Core analyses
    languages = analyze_languages(all_files)
    file_sizes = analyze_file_sizes(all_files, top_n=args.top_files)
    tests = analyze_tests(all_files)
    frameworks = analyze_frameworks(root)
    dependencies = analyze_dependencies(root)
    ci = analyze_ci(root)
    security = analyze_security(root, all_files)
    lint = analyze_lint_config(root)
    test_fw = analyze_test_framework(root)
    docs = analyze_documentation(root)
    repro = analyze_reproducibility(root, all_files)
    observability = analyze_observability(root, all_files)
    class_signals = analyze_class_signals(root, all_files, languages["loc_by_language"], frameworks)
    project_type = infer_project_type(root, frameworks)

    # Test:source ratio
    total_source = file_sizes.get("total_source_files", 0)
    spec_count = tests.get("spec_files", 0)
    test_source_ratio = f"1:{round(total_source / spec_count)}" if spec_count > 0 and total_source > 0 else "0 tests"

    output = {
        "schema_version": "1.0",
        "repo_path": str(root),
        "repo_name": root.name,

        # Identity
        "primary_language": languages["primary_language"],
        "secondary_languages": languages["secondary_languages"],
        "loc_by_language": languages["loc_by_language"],
        "file_count_by_language": languages["file_count_by_language"],
        "detected_frameworks": frameworks,
        "project_type": project_type,

        # Size
        "total_loc": languages["total_loc"],
        "total_source_files": file_sizes.get("total_source_files", 0),
        "median_file_size_loc": file_sizes.get("median_loc", 0),
        "p90_file_size_loc": file_sizes.get("p90_loc", 0),
        "god_files_over_500_loc": file_sizes.get("god_files_over_500", 0),
        "god_files_over_1000_loc": file_sizes.get("god_files_over_1000", 0),
        "top_largest_files": file_sizes.get("largest_files", []),
        "generated_files_excluded": file_sizes.get("generated_excluded", 0),

        # Tests
        "test_spec_files": tests["spec_files"],
        "test_fixture_files": tests["fixture_and_snapshot_files"],
        "test_source_ratio": test_source_ratio,
        "test_spec_sample": tests["spec_file_paths_sample"],
        "test_framework": test_fw["frameworks"],
        "test_config_files": test_fw["config_files"],
        "coverage_tooling": test_fw["coverage_tooling"],
        "coverage_threshold": test_fw["coverage_threshold"],

        # Dependencies
        "package_managers": dependencies["package_managers"],
        "manifests_found": dependencies["manifests_found"],
        "lockfiles_found": dependencies["lockfiles_found"],
        "lockfiles_expected": dependencies["lockfiles_expected"],
        "direct_runtime_deps": dependencies["direct_runtime_deps"],
        "direct_dev_deps": dependencies["direct_dev_deps"],
        "total_transitive_deps": dependencies["total_transitive_deps"],
        "dep_update_tooling": dependencies["dep_update_tooling"],

        # CI
        "ci_systems": ci["ci_systems"],
        "ci_config_files": ci["ci_configs"],
        "ci_runs_tests": ci["runs_tests"],
        "ci_runs_lint": ci["runs_lint"],
        "ci_runs_typecheck": ci["runs_typecheck"],
        "ci_has_deploy": ci["has_deploy_pipeline"],
        "ci_present": ci["ci_present"],

        # Security
        "hardcoded_secret_hits": security["hardcoded_secret_hits"],
        "secret_hit_details": security["secret_hit_details"],
        "env_files_committed": security["env_files_committed"],
        "dep_audit_in_ci": security["dep_audit_in_ci"],
        "input_validation_patterns": security["input_validation_patterns"],

        # Linting
        "linters_and_formatters": lint["linters_and_formatters"],
        "has_lint_config": lint["has_lint_config"],

        # Documentation
        "readme": docs["readme"],
        "readme_loc": docs["readme_loc"],
        "readme_sections": docs["readme_sections_detected"],
        "changelog": docs["changelog"],
        "contributing_guide": docs["contributing_guide"],
        "has_pr_template": docs["has_pr_template"],
        "has_issue_template": docs["has_issue_template"],

        # Reproducibility
        "has_dockerfile": repro["has_dockerfile"],
        "has_docker_compose": repro["has_docker_compose"],
        "has_devcontainer": repro["has_devcontainer"],
        "has_nix": repro["has_nix"],
        "env_example_file": repro["env_example_file"],
        "env_vars_referenced_in_source": repro["env_vars_referenced_in_source"],
        "env_vars_in_example": repro["env_vars_in_example"],
        "env_vars_missing_from_example": repro["env_vars_missing_from_example"],

        # Observability
        "logging_framework": observability["logging_framework"],
        "error_tracking": observability["error_tracking"],
        "has_health_endpoint": observability["has_health_endpoint"],
        "has_metrics": observability["has_metrics"],

        # Class signals (consumed by classify_repo.py)
        "class_signals": class_signals,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
