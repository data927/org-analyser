#!/usr/bin/env python3
"""
Universal Codebase Analyzer (GitLab + GitHub + Offline/Local repo)
==================================================================
One script to analyze three kinds of sources:
  1. GitLab   (gitlab.com OR self-hosted)  - via API, no clone needed
  2. GitHub   (github.com OR GitHub Enterprise) - via API
  3. Offline  (repo cloned on local disk) - only git required

Fields that get extracted:
  - Project/Group name
  - Established year, Years active, Last activity
  - # of contributors
  - Primary coding language + language breakdown
  - Total LoC (estimate; ACTUAL line count for local repos)
  - # of Repos (projects)
  - # of MRs/PRs, # of Merged
  - Avg LoC per MR
  - % Simple fixes / % Standard work / % Rich tasks / % Automated / % Other
  - # of Commits
  - CI/CD analysis (NEW):
      * Whether CI/CD is configured or not
      * Which CI systems (GitLab CI, GitHub Actions, Jenkins, CircleCI,
        Travis, Azure Pipelines, Drone, Buildkite, etc.)
      * CI config files count + jobs/stages (approx)
      * Total pipelines / workflow runs
      * Recent pipeline success rate %
      * Avg pipeline duration (min)
      * Unit test coverage % (from GitLab pipeline coverage)
  - Test files count, Test cases count (approx) + genuineness check
  - AI/LLM detection (NEW):
      * LLM usage %  — what % of commits/MRs carry an explicit AI signature
      * Which LLM    — Claude Code, GitHub Copilot, Cursor, Aider, Devin,
        ChatGPT/Codex, Gemini/Jules, Sweep, Windsurf/Codeium, OpenHands...
      * Detection sources: commit trailers (Co-Authored-By), commit messages,
        AI bot authors, MR/PR descriptions, code comments, tool config files
        (CLAUDE.md, .cursorrules, copilot-instructions.md, AGENTS.md, etc.)
      * NOTE: only EXPLICIT signatures are detected. If a developer
        copy-pasted AI code and stripped the attribution, it will NOT be
        detected — so this is a LOWER BOUND, not an upper bound.
  - Training-data quality (NEW) — factors for LLM post-training curation:
      * License + risk (permissive/weak-copyleft/copyleft/no-license)
        [heuristic detection — not legal advice]
      * Syntax validity % (Python: exact via ast.parse)
      * Quality metrics: avg/long line %, comment ratio, docstring %,
        avg function length, nesting depth
      * Duplicate files % (normalized content hash)
      * Secrets/PII: AWS/GitHub/OpenAI keys, private keys, hardcoded
        passwords (always MASKED in the report), email count
      * Eval contamination: known HumanEval/MBPP signatures
      * Composite Quality score (0-100) + Training suitability grade (A-D)
  - Code availability (public/private/local)

How to create a token:
  GitLab -> Profile -> Preferences -> Access Tokens  (scope: read_api)
  GitHub -> Settings -> Developer settings -> Personal access tokens
            (classic: 'repo' scope for private repos, nothing for public)

Usage:
  # --- GitLab ---
  python repo_analyzer.py --provider gitlab --project "group/repo" --token glpat-XXXX
  python repo_analyzer.py --provider gitlab --group "my-group" --token glpat-XXXX
  python repo_analyzer.py --project "team/app" --token XXXX \\
      --gitlab-url https://gitlab.mycompany.com

  # --- GitHub ---
  python repo_analyzer.py --provider github --project "owner/repo" --token ghp_XXXX
  python repo_analyzer.py --provider github --org "my-org" --token ghp_XXXX

  # --- Offline / Local repo (no internet required) ---
  python repo_analyzer.py --provider local --path /home/user/my-repo
  python repo_analyzer.py --path C:\\code\\repo1 --path C:\\code\\repo2

  # Provider auto-detection also works:
  #   --path given              -> local
  #   token starts with glpat-  -> gitlab
  #   token ghp_/github_        -> github
  #   --org given               -> github,  --group given -> gitlab

Options:
  --max-mrs 0        how many MRs/PRs to fetch (0 = ALL, default)
  --sample-mrs 0     how many MRs/PRs to deep-analyze (0 = all fetched)
  --max-test-files 0   how many test files to check
                       (default 0 = auto: ALL files locally, 200 via API)
  --max-commit-scan 0     how many commits to scan for LLM signatures
                          (default 0 = ALL commits are scanned)
  --max-ai-file-scan 0    how many source files to search for AI-attribution
                          comments (0 = auto: ALL locally, 30 via API)
  LOCAL MODE GUARANTEE: with default settings EVERY code file, EVERY test
  file and EVERY commit of a local repo is scanned — no sampling.
  --workers 8        parallel API requests
  --local-mrs auto   MR detection for local repos:
                       auto   = prefer explicit #N/!N refs (default)
                       strict = ONLY explicit refs (most accurate)
                       merges = also count all merge commits (approx)
                       off    = leave MR fields blank
  --output report.csv

Note: analyzing all MRs on large repos takes time — each MR needs 1-2
extra API calls. Progress is shown, and on rate limits the script waits
and resumes on its own.

Requirements:  pip install requests   (local mode only needs git)
"""

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

try:
    import requests
    from requests.adapters import HTTPAdapter
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False   # local mode also works without requests

# ---------------------------------------------------------------------------
# Patterns (covers both GitLab and GitHub bots)
# ---------------------------------------------------------------------------

BOT_PATTERNS = [
    r"\bbot\b", r"\[bot\]", r"dependabot", r"renovate", r"gitlab-bot",
    r"group_\d+_bot", r"project_\d+_bot", r"ghost", r"semantic-release",
    r"snyk", r"codecov", r"greenkeeper", r"release-tools", r"-bot$", r"^bot-",
    r"github-actions", r"web-flow", r"mergify", r"kodiak", r"imgbot",
    r"allcontributors", r"pre-commit-ci", r"pyup", r"whitesource", r"mend",
    # AI coding agents that commit/PR via bot accounts
    r"devin-ai-integration", r"sweep-ai", r"copilot-swe-agent",
    r"google-labs-jules", r"cursor-agent", r"openhands-agent",
    r"claude\[bot\]", r"codegen-sh", r"ellipsis-dev", r"coderabbitai",
]

SIMPLE_FIX_TITLE_PATTERNS = [
    r"\btypo\b", r"\bbump\b", r"\bupgrade\b", r"\bupdate dep", r"\blint\b",
    r"\bformat(ting)?\b", r"\bwhitespace\b", r"^chore\b", r"^docs?\b",
    r"^style\b", r"\bconfig\b", r"\breadme\b", r"\bversion\b",
]

ISSUE_LINK_PATTERNS = [
    r"(close[sd]?|fix(e[sd])?|resolve[sd]?|implement[sed]*)\s*:?\s*#\d+",
    r"#\d+",
    r"\b[A-Z][A-Z0-9]{1,9}-\d+\b",           # JIRA style
    r"(issues|merge_requests|pull)/\d+",
]

TEST_FILE_PATTERNS = [
    r"(^|/)test_[^/]*\.py$", r"[^/]*_test\.py$", r"(^|/)tests?\.py$",
    r"\.test\.(js|jsx|ts|tsx|mjs|cjs)$", r"\.spec\.(js|jsx|ts|tsx|rb|php)$",
    r"Tests?\.(java|kt|cs|php|swift|m)$", r"_test\.(go|rb|ex|exs|c|cc|cpp|rs)$",
    r"_spec\.rb$", r"\.feature$", r"(^|/)Test[A-Z][^/]*\.(java|kt|php)$",
    r"IT\.java$", r"Spec\.(scala|kt|groovy)$",
]

# NOTE: "/feature/" and "/unit/" used to be here too — they wrongly flagged
# app source folders (src/feature/login, app/unit/converter) as tests.
# Now only definite test directories. "/testing/" was also removed (ambiguous).
TEST_DIR_HINTS = ("/test/", "/tests/", "/spec/", "/specs/", "/__tests__/",
                  "/src/test/", "/androidtest/", "/cypress/", "/e2e/")

# Support files inside test folders — these are NOT tests (fixtures,
# helpers, config). They must not be counted as test files.
TEST_SUPPORT_PATTERNS = [
    r"(^|/)conftest\.py$", r"(^|/)__init__\.py$", r"(^|/)setup\.py$",
    r"/fixtures?/", r"/__mocks__/", r"/mocks?/", r"/testdata/",
    r"/test_data/", r"/factories/", r"/stubs?/", r"/helpers?/",
    r"(^|/)jest\.(config|setup)\.", r"(^|/)setup[-_]?tests?\.",
    r"(^|/)test[-_]?(utils?|helpers?|setup|config)\.",
]

# --- Test-case declaration patterns (what counts as a test case) ---
# NOTE: "async def test_" is not a separate pattern — "def test_" matches it
# too; keeping it separate counted every async test TWICE (bug fix).
TEST_CASE_PATTERNS = [
    r"\bdef test_\w+",
    r"\b(?:it|test)\s*\(\s*['\"`]", r"\bit\.each\s*\(",
    r"\b(?:it|test)\.todo\s*\(",     # stub/pending tests (without assertions
                                     # the verdict will come out suspicious)
    r"@Test\b", r"@ParameterizedTest\b", r"\bfunc Test[A-Z]\w*",
    r"#\[(?:tokio::)?test\]", r"\bTEST(_F|_P)?\s*\(",
    r"\bscenario\s*['\"(]",
    r"\bpublic function test\w+",              # PHPUnit / Laravel
    r"/\*\*\s*@test\s*\*/", r"#\[Test\]",       # PHPUnit annotations
    r"\btest\s+['\"].+['\"]\s+do\b",            # ruby minitest
]

# --- Assertion patterns (the mark of a genuine test) ---
ASSERTION_PATTERNS = [
    r"\bassert\s+\w", r"\bself\.assert\w+\s*\(", r"\bpytest\.raises\b",
    r"\bexpect\s*\(", r"\bassert\.\w+\s*\(", r"\.should[\.\(]",
    r"\bassert(Equals|True|False|That|NotNull|Null|Same|Throws)\s*\(",
    r"\bverify\s*\(", r"\bt\.(Error|Fatal|Errorf|Fatalf)\b",
    r"\brequire\.\w+\s*\(", r"\bassert_\w+", r"\bmust_\w+",
    r"\$this->assert\w+\s*\(", r"\bAssert::\w+\s*\(",   # PHP
    r"->assert\w+\s*\(", r"\bassertDatabaseHas\b",       # Laravel
    r"\bXCTAssert\w*\s*\(", r"\bEXPECT_\w+\s*\(", r"\bASSERT_\w+\s*\(",
]

# --- Fake/trivial assertions (ones that always pass) ---
TRIVIAL_ASSERTION_PATTERNS = [
    r"assert\s+True\b", r"assert\s+1\b", r"assertTrue\s*\(\s*true\s*\)",
    r"assertTrue\s*\(\s*1\s*\)", r"expect\s*\(\s*true\s*\)\s*\.\s*toBe(Truthy)?\s*\(\s*true?\s*\)",
    r"assertEquals?\s*\(\s*(\d+)\s*,\s*\1\s*\)", r"expect\s*\(\s*(\d+)\s*\)\s*\.\s*toBe\s*\(\s*\1\s*\)",
    r"\$this->assertTrue\s*\(\s*true\s*\)", r"XCTAssertTrue\s*\(\s*true\s*\)",
]

# --- Skipped/disabled tests ---
SKIP_PATTERNS = [
    r"@pytest\.mark\.skip", r"@unittest\.skip", r"\bit\.skip\s*\(",
    r"\bxit\s*\(", r"\bxdescribe\s*\(", r"\btest\.skip\s*\(",
    r"@Disabled\b", r"@Ignore\b", r"markTestSkipped", r"markTestIncomplete",
    r"\bt\.Skip\s*\(", r"\bskip\s*[:=]\s*true",
]

CODE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go",
                   ".rb", ".rs", ".c", ".h", ".cpp", ".cc", ".cs", ".php",
                   ".swift", ".m", ".scala", ".ex", ".exs", ".dart", ".sh",
                   ".lua", ".r", ".pl", ".vue", ".svelte", ".blade.php"}

# ---------------------------------------------------------------------------
# AI / LLM detection (NEW)
# ---------------------------------------------------------------------------
# Detection happens from 4 places:
#   1. Commit messages/trailers  (most reliable — Co-Authored-By etc.)
#   2. Commit/MR author          (AI agent bot accounts)
#   3. MR/PR title+description   (agents write their own attribution)
#   4. Tool config files in repo (CLAUDE.md, .cursorrules... = tool was used)
#   5. Code comments             ("Generated by ..." headers)
#
# IMPORTANT: these are all EXPLICIT markers. Silent AI use (pasting with
# attribution stripped) cannot be detected — the result is a LOWER BOUND.

LLM_SIGNATURES = [
    ("Claude (Claude Code)", [
        r"co-authored-by:\s*claude\b",
        r"noreply@anthropic\.com",
        r"generated with \[?claude",
        r"claude\.(ai|com)/(code|claude-code)",
        r"\bclaude code\b",
        r"🤖 generated with",
    ]),
    ("GitHub Copilot", [
        r"co-authored-by:.{0,40}copilot",
        r"copilot-swe-agent",
        r"generated (by|with|using) (github )?copilot",
        r"copilot@github\.com",
        r"\bcopilot workspace\b",
    ]),
    ("Cursor", [
        r"co-authored-by:\s*cursor",
        r"cursoragent@cursor\.(sh|com)",
        r"generated (by|with|using) cursor\b",
        r"\bcursor (agent|composer)\b",
    ]),
    ("Aider", [
        r"co-authored-by:\s*aider",
        r"\baider \(.*\) *$",
        r"aider\.chat",
        r"generated (by|with|using) aider\b",
        r"aider:\s",
    ]),
    ("Devin", [
        r"devin-ai-integration",
        r"co-authored-by:\s*devin",
        r"devin\.ai",
        r"created by devin\b",
    ]),
    ("ChatGPT / OpenAI Codex", [
        r"co-authored-by:.{0,40}(chatgpt|openai|codex)",
        r"generated (by|with|using) (chatgpt|gpt-?[345o]|openai codex|codex)",
        r"generated by (an? )?llm.{0,60}(openai|gpt-?\d)",
        r"\bopenai'?s\s+gpt", r"\bgpt-?[34](\.\d)?[o]?\s+model\b",
        r"chatgpt\.com/codex",
        r"codex-cli",
    ]),
    ("Gemini / Jules", [
        r"co-authored-by:.{0,40}(gemini|jules|google-labs)",
        r"generated (by|with|using) gemini",
        r"google-labs-jules",
        r"\bgemini (cli|code assist)\b",
    ]),
    ("Sweep AI", [r"sweep-ai\b", r"sweep\.dev", r"generated by sweep\b"]),
    ("Windsurf / Codeium", [
        r"co-authored-by:.{0,40}(windsurf|codeium|cascade)",
        r"generated (by|with|using) (windsurf|codeium)",
    ]),
    ("Amazon Q / CodeWhisperer", [
        r"\bamazon q developer\b", r"codewhisperer",
    ]),
    ("OpenHands", [r"openhands", r"all-hands\.dev"]),
    ("Codegen", [r"codegen-sh\b", r"codegen\.com"]),
    ("Generic AI", [
        r"co-authored-by:.{0,40}\bai\b",
        r"(auto-?)?generated (by|with|using) (an? )?(ai|llm|large language model)\b",
        r"this (commit|change|code|pr|mr) was (written|generated|created) by (an? )?(ai|llm)\b",
        # Reversed word-order: "29 AI-generated", "LLM-generated tests"
        # (strict word boundaries — so project names like "ClientAI" do NOT match)
        r"\b(ai|llm)[- ]generated\b",
        r"\b(ai|llm)[- ](written|created|assisted)\b",
        r"\bwritten (by|with) (an? )?(ai|llm)\b",
    ]),
]

# Author/username -> LLM (bot accounts that commit/PR by themselves)
LLM_BOT_AUTHORS = [
    ("Claude (Claude Code)", [r"^claude(\[bot\])?$", r"claude-code"]),
    ("GitHub Copilot",       [r"copilot", r"copilot-swe-agent"]),
    ("Devin",                [r"devin-ai-integration", r"^devin\b"]),
    ("Sweep AI",             [r"sweep-ai"]),
    ("Gemini / Jules",       [r"google-labs-jules", r"^jules\b"]),
    ("Cursor",               [r"^cursor(\[bot\]|-agent)?$", r"cursoragent"]),
    ("OpenHands",            [r"openhands"]),
    ("Codegen",              [r"codegen-sh"]),
]

# AI tool config/instruction files in the repo = the tool was actively used
AI_TOOL_CONFIGS = [
    ("Claude (Claude Code)", [r"(^|/)CLAUDE(\.local)?\.md$", r"^\.claude/"]),
    ("Cursor",               [r"^\.cursorrules$", r"^\.cursor/",
                              r"^\.cursorignore$"]),
    ("GitHub Copilot",       [r"^\.github/copilot-instructions\.md$",
                              r"^\.github/copilot/"]),
    ("Aider",                [r"^\.aider\.conf\.ya?ml$", r"^\.aiderignore$",
                              r"(^|/)CONVENTIONS\.md$"]),
    ("Windsurf / Codeium",   [r"^\.windsurfrules$", r"^\.windsurf/",
                              r"^\.codeium/"]),
    ("Gemini / Jules",       [r"(^|/)GEMINI\.md$", r"^\.gemini/",
                              r"^\.jules/"]),
    ("Continue",             [r"^\.continue/", r"^\.continuerc"]),
    ("Sourcegraph Cody",     [r"^\.sourcegraph/", r"^\.cody/"]),
    ("Generic AI agents",    [r"(^|/)AGENTS?\.md$", r"^\.ai/", r"^\.mcp\.json$"]),
]

# Explicit AI-attribution comments inside source code
LLM_CODE_COMMENT_SIGNATURES = [
    ("Claude (Claude Code)", [r"generated (by|with|using) claude",
                              r"written (by|with) claude\b"]),
    ("ChatGPT / OpenAI Codex", [r"generated (by|with|using) (chatgpt|gpt-?[345o]|codex)",
                              r"generated by (an? )?llm.{0,60}(openai|gpt-?\d)",
                              r"\bopenai'?s\s+gpt"]),
    ("GitHub Copilot",       [r"generated (by|with|using) (github )?copilot",
                              r"suggested by copilot"]),
    ("Cursor",               [r"generated (by|with|using) cursor\b"]),
    ("Gemini / Jules",       [r"generated (by|with|using) gemini"]),
    ("Generic AI",           [r"(auto-?)?generated (by|with|using) (an? )?(ai|llm)\b",
                              r"ai-generated (code|file|module|test)",
                              r"\b(ai|llm)[- ]generated\b"]),
]


def detect_llms_in_text(text):
    """Which LLM signatures appear in text (commit msg / MR description).
    Returns: set of LLM names."""
    found = set()
    t = text or ""
    for name, pats in LLM_SIGNATURES:
        if any(re.search(p, t, re.IGNORECASE) for p in pats):
            found.add(name)
    # Keep "Generic AI" only when no specific LLM was found
    if len(found) > 1:
        found.discard("Generic AI")
    return found


def detect_llm_author(author):
    """Is the author/username an AI agent bot? Returns the name or None."""
    a = (author or "").lower()
    for name, pats in LLM_BOT_AUTHORS:
        if any(re.search(p, a, re.IGNORECASE) for p in pats):
            return name
    return None


def detect_ai_tool_configs(paths):
    """AI tool config files in the file tree. Returns {tool: [paths...]}"""
    found = {}
    for p in paths:
        norm = (p or "").replace("\\", "/").lstrip("/")
        for tool, pats in AI_TOOL_CONFIGS:
            if any(re.search(pat, norm, re.IGNORECASE) for pat in pats):
                found.setdefault(tool, []).append(norm)
                break
    return found


def detect_llms_in_code(content):
    """Explicit AI attribution in a source file's comments — including the
    EXACT LOCATION (line number).

    Previously this only returned a `set` of LLM names (just "yes/no" — you
    couldn't tell WHERE inside the file it matched). Now every match also
    comes with its exact line number, so the evidence can show file + line
    (e.g. "utils.py:3").

    Returns: {llm_name: [line_no, line_no, ...]}   (1-indexed lines)
    """
    found = {}
    head = (content or "")[:8000]   # attribution headers sit at the top
    lines = head.splitlines()
    for name, pats in LLM_CODE_COMMENT_SIGNATURES:
        hit_lines = [i for i, line in enumerate(lines, start=1)
                    if any(re.search(p, line, re.IGNORECASE) for p in pats)]
        if hit_lines:
            found[name] = hit_lines
    # Keep "Generic AI" only when no specific LLM was found
    if len(found) > 1:
        found.pop("Generic AI", None)
    return found


# ---------------------------------------------------------------------------
# Training-data quality detection (NEW)
# ---------------------------------------------------------------------------
# Factors needed when curating code for LLM post-training:
#   1. License          — permissive/copyleft/none (legal filter)
#   2. Syntax validity  — Python files ast.parse se, baaki binary-junk check
#   3. Quality metrics  — line length, comment ratio, function length, nesting
#   4. Deduplication    — duplicate files % (normalized content hash)
#   5. Secrets/PII      — API keys, private keys, emails (masked report)
#   6. Eval contamination — known HumanEval/MBPP function signatures
#   7. Composite score + Training suitability grade (A/B/C/D)
#
# NOTE: License classification is HEURISTIC — not legal advice.
# Secrets are never reported in PLAIN text — always masked.

LICENSE_CLASSIFIERS = [
    # Order matters: specific ones first (AGPL before GPL, BSD-3 before BSD-2)
    ("AGPL-3.0",     r"gnu affero general public license"),
    ("LGPL",         r"gnu lesser general public license"),
    ("GPL-3.0",      r"gnu general public license[\s\S]{0,80}version 3"),
    ("GPL-2.0",      r"gnu general public license[\s\S]{0,80}version 2"),
    ("Apache-2.0",   r"apache license[,\s]*version 2\.0"),
    ("MPL-2.0",      r"mozilla public license[,\s]*(v(ersion)?\.?\s*)?2\.0"),
    ("MIT",          r"permission is hereby granted, free of charge"),
    ("BSD-3-Clause", r"redistribution and use in source and binary forms"
                     r"[\s\S]{0,600}neither the name"),
    ("BSD-2-Clause", r"redistribution and use in source and binary forms"),
    ("ISC",          r"permission to use, copy, modify, and(/or)? distribute "
                     r"this software"),
    ("Unlicense",    r"this is free and unencumbered software"),
    ("CC0-1.0",      r"cc0|creative commons zero"),
    ("WTFPL",        r"do what the f\w+ you want"),
]

LICENSE_RISK = {
    "MIT": "permissive", "Apache-2.0": "permissive",
    "BSD-2-Clause": "permissive", "BSD-3-Clause": "permissive",
    "ISC": "permissive", "Unlicense": "permissive", "CC0-1.0": "permissive",
    "WTFPL": "permissive", "MPL-2.0": "weak-copyleft", "LGPL": "weak-copyleft",
    "GPL-2.0": "copyleft", "GPL-3.0": "copyleft", "AGPL-3.0": "copyleft",
}

LICENSE_FILE_RE = re.compile(
    r"(^|/)(un)?licen[cs]e(\.(md|txt|rst))?$|(^|/)copying(\.txt)?$",
    re.IGNORECASE)

SECRET_PATTERNS = [
    ("AWS Access Key",   r"\bAKIA[0-9A-Z]{16}\b"),
    ("GitHub Token",     r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    ("GitLab Token",     r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    ("Google API Key",   r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("Slack Token",      r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    ("Stripe Key",       r"\b[sp]k_(live|test)_[A-Za-z0-9]{16,}\b"),
    ("OpenAI Key",       r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    ("Anthropic Key",    r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    ("Private Key",      r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?"
                         r"PRIVATE KEY"),
    ("JWT",              r"\beyJ[A-Za-z0-9_\-]{15,}\.eyJ[A-Za-z0-9_\-]{15,}"),
    ("Hardcoded secret", r"(?i)\b(password|passwd|secret|api[_-]?key|"
                         r"auth[_-]?token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
]

# Skip "hardcoded secret" false positives (placeholders)
_PLACEHOLDER_RE = re.compile(
    r"(?i)(change|example|placeholder|your[_-]|dummy|sample|test|xxxx|\*\*\*|"
    r"<[^>]*>|\{\{|\$\{|os\.environ|process\.env|getenv|None|null|TODO)")

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
_EMAIL_IGNORE_RE = re.compile(
    r"(?i)(example\.|noreply|no-reply|test@|@test\.|localhost|\.png|\.jpg|"
    r"@example|users\.noreply|@sentry|@2x)")

# Distinctive HumanEval / MBPP signatures — if these are in training data
# it's benchmark contamination (eval scores become fake)
EVAL_CONTAMINATION_SIGNATURES = [
    "def has_close_elements(", "def separate_paren_groups(",
    "def truncate_number(", "def below_zero(",
    "def mean_absolute_deviation(", "def intersperse(",
    "def parse_nested_parens(", "def similar_elements(",
    "def is_not_prime(", "def heap_queue_largest(",
    "def count_ways(", "def differ_At_One_Bit_Pos(",
]

_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", "<!--", "%", ";", "'")
_FUNC_DEF_RE = re.compile(
    r"^\s*(?:async\s+)?(?:def|function|func|fn)\s+\w+|"
    r"^\s*(?:public|private|protected|static)[\w<>\[\],\s]*\s\w+\s*\([^;]*\)\s*\{",
    re.MULTILINE)


def _mask_secret(v):
    """Secrets are never reported in plain text — masked version."""
    return "[REDACTED]"


def scan_secrets_and_pii(content, path):
    """Secrets (masked) + email count for one file. Returns (secrets, emails)."""
    secrets = []
    for name, pat in SECRET_PATTERNS:
        m = re.search(pat, content)
        if not m:
            continue
        val = m.group(0)
        if name == "Hardcoded secret":
            # skip placeholder values / env lookups
            line_start = content.rfind("\n", 0, m.start()) + 1
            line_end = content.find("\n", m.end())
            if line_end == -1:
                line_end = len(content)
            if _PLACEHOLDER_RE.search(content[line_start:line_end]):
                continue
        secrets.append({"type": name, "file": path,
                        "masked": _mask_secret(val)})
    emails = 0
    for m in EMAIL_RE.finditer(content):
        if not _EMAIL_IGNORE_RE.search(m.group(0)):
            emails += 1
    return secrets, emails


def scan_file_quality(path, content):
    """Per-file quality metrics — the standard signals for training-data curation."""
    lines = content.splitlines()
    n = len(lines) or 1
    lens = [len(l) for l in lines]
    stripped = [l.strip() for l in lines]
    nonblank = [s for s in stripped if s]
    blank = n - len(nonblank)
    comment = sum(1 for s in stripped if s.startswith(_COMMENT_PREFIXES))
    code_lines = max(1, n - blank - comment)
    long_lines = sum(1 for L in lens if L > 120)
    funcs = len(_FUNC_DEF_RE.findall(content))
    # nesting depth (approx): max leading indent / 4 (tabs = 1 level)
    depth = 0
    for l in lines:
        ind = len(l) - len(l.lstrip(" \t"))
        tabs = l[:ind].count("\t")
        depth = max(depth, tabs + (ind - tabs) // 4)
    alnum = sum(c.isalnum() for c in content)
    # syntax check — exact for Python (ast), binary-junk heuristic for the rest
    syntax_valid = None
    if path.endswith(".py"):
        try:
            ast.parse(content)
            syntax_valid = True
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            syntax_valid = False
    elif "\x00" in content[:4000]:
        syntax_valid = False
    return {
        "lines": n, "code_lines": code_lines,
        "avg_len": sum(lens) / n, "max_len": max(lens) if lens else 0,
        "long_lines": long_lines,
        "comment_lines": comment, "blank_lines": blank,
        "funcs": funcs, "max_depth": depth,
        "alnum_ratio": alnum / max(1, len(content)),
        "syntax_valid": syntax_valid,
        "has_docstring": (path.endswith(".py")
                          and bool(re.search(r'^\s*(?:\'\'\'|""")',
                                             content[:2000], re.M))),
        # hash=None for near-empty files (e.g. blank __init__.py) so they
        # don't all collide and massively inflate the duplicate-file %
        "hash": (hashlib.sha1(
            "\n".join(nonblank).encode("utf-8", "ignore")).hexdigest()
            if len(nonblank) >= 3 else None),
        "eval_contam": any(sig in content
                           for sig in EVAL_CONTAMINATION_SIGNATURES),
    }


_SPDX_SHORT = {"mit": "MIT", "apache-2.0": "Apache-2.0", "apache2": "Apache-2.0",
               "gpl-2.0": "GPL-2.0", "gpl-3.0": "GPL-3.0", "gplv2": "GPL-2.0",
               "gplv3": "GPL-3.0", "agpl-3.0": "AGPL-3.0", "lgpl": "LGPL",
               "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause",
               "isc": "ISC", "mpl-2.0": "MPL-2.0", "unlicense": "Unlicense",
               "cc0": "CC0-1.0", "wtfpl": "WTFPL"}


def classify_license(text):
    """SPDX-like name from license file text. Heuristic — not legal advice."""
    t = (text or "").lower()
    # Short form: the file contains only an SPDX id ("MIT", "Apache-2.0"...)
    if len(t.strip()) <= 40 and t.strip() in _SPDX_SHORT:
        return _SPDX_SHORT[t.strip()]
    for name, pat in LICENSE_CLASSIFIERS:
        if re.search(pat, t):
            return name
    return ""

VENDOR_DIR_HINTS = ("/vendor/", "/node_modules/", "/dist/", "/build/",
                    "/.git/", "/bower_components/", "/storage/framework/",
                    "/__pycache__/", "/.venv/", "/venv/", "/.tox/",
                    "/site-packages/", "/pods/", "/.next/", "/.nuxt/",
                    "/coverage/", "/htmlcov/", "/.gradle/", "/deriveddata/",
                    "/.terraform/", "/target/debug/", "/target/release/")

# Generated/minified files — neither source nor tests; they skew the counts
GENERATED_FILE_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".chunk.js",
                           ".map", ".pb.go", "_pb2.py", "_pb2_grpc.py",
                           ".g.dart", ".freezed.dart", ".generated.ts",
                           ".d.ts")

# Extension -> language (for local repos)
EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".rb": "Ruby", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".cs": "C#",
    ".php": "PHP", ".swift": "Swift", ".m": "Objective-C",
    ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir", ".dart": "Dart",
    ".sh": "Shell", ".lua": "Lua", ".r": "R", ".pl": "Perl",
    ".vue": "Vue", ".svelte": "Svelte", ".html": "HTML", ".css": "CSS",
}

# ---------------------------------------------------------------------------
# CI/CD detection (NEW) — which config files indicate which CI system
# ---------------------------------------------------------------------------

CI_SYSTEMS = [
    ("GitLab CI",           [r"^\.gitlab-ci\.ya?ml$", r"^\.gitlab/ci/.+\.ya?ml$"]),
    ("GitHub Actions",      [r"^\.github/workflows/.+\.ya?ml$"]),
    ("Jenkins",             [r"(^|/)Jenkinsfile([^/]*)$"]),
    ("CircleCI",            [r"^\.circleci/config\.ya?ml$"]),
    ("Travis CI",           [r"^\.travis\.ya?ml$"]),
    ("Azure Pipelines",     [r"^azure-pipelines[^/]*\.ya?ml$", r"^\.azure-pipelines/.+\.ya?ml$"]),
    ("Bitbucket Pipelines", [r"^bitbucket-pipelines\.ya?ml$"]),
    ("Drone CI",            [r"^\.drone\.ya?ml$"]),
    ("AppVeyor",            [r"^\.?appveyor\.ya?ml$"]),
    ("Buildkite",           [r"^\.buildkite/.+\.ya?ml$"]),
    ("TeamCity",            [r"^\.teamcity/"]),
    ("Google Cloud Build",  [r"^cloudbuild\.ya?ml$"]),
    ("Woodpecker CI",       [r"^\.woodpecker\.ya?ml$", r"^\.woodpecker/.+\.ya?ml$"]),
    ("Tekton",              [r"^\.tekton/.+\.ya?ml$"]),
    ("Bamboo",              [r"^bamboo-specs/"]),
]

GITLAB_CI_RESERVED = {"stages", "variables", "include", "default", "workflow",
                      "image", "services", "before_script", "after_script",
                      "cache", "pages", "types"}


def detect_ci_configs(paths):
    """Find CI config files in a list of file paths.
    Returns: {system_name: [paths...]}"""
    found = {}
    for p in paths:
        norm = (p or "").replace("\\", "/").lstrip("/")
        for system, pats in CI_SYSTEMS:
            if any(re.search(pat, norm, re.IGNORECASE) for pat in pats):
                found.setdefault(system, []).append(norm)
                break
    return found


def analyze_ci_config(system, content):
    """Approximate count of jobs/stages inside a CI config file (regex-based,
    no YAML parser dependency). Returns: {jobs, stages}"""
    jobs, stages = 0, 0
    if not content:
        return {"jobs": 0, "stages": 0}
    try:
        if system == "GitLab CI":
            # top-level keys that are not reserved = jobs
            top_keys = re.findall(r"^([A-Za-z_.][\w.\- ]*):", content, re.MULTILINE)
            jobs = len([k for k in top_keys
                        if k.strip().lower() not in GITLAB_CI_RESERVED
                        and not k.startswith(".")])          # .hidden = templates
            m = re.search(r"^stages:\s*\n((?:\s*-\s*.+\n?)+)", content, re.MULTILINE)
            if m:
                stages = len(re.findall(r"^\s*-\s*\S", m.group(1), re.MULTILINE))
            inline = re.search(r"^stages:\s*\[(.*?)\]", content, re.MULTILINE)
            if inline:
                stages = len([s for s in inline.group(1).split(",") if s.strip()])
        elif system == "GitHub Actions":
            # the whole indented block under 'jobs:'; its 2-space keys = jobs
            m = re.search(r"^jobs:[ \t]*\n((?:(?:[ \t]+[^\n]*)?\n)+)",
                          content, re.MULTILINE)
            block = m.group(1) if m else ""
            jobs = len(re.findall(r"^  ([\w\-]+):", block, re.MULTILINE))
            stages = 1 if jobs else 0
        elif system == "Jenkins":
            jobs = len(re.findall(r"\bstage\s*[\('\"]", content))
            stages = jobs
        else:
            # generic YAML: count 'steps'/'jobs' entries
            jobs = len(re.findall(r"^\s*-\s*(name|step|task|script)\s*:",
                                  content, re.MULTILINE))
    except Exception:
        pass
    return {"jobs": jobs, "stages": stages}


def matches_any(text, patterns, flags=re.IGNORECASE):
    return any(re.search(p, text or "", flags) for p in patterns)


def is_bot(username: str) -> bool:
    return matches_any(username, BOT_PATTERNS)


def is_vendor_path(path: str) -> bool:
    p = "/" + (path or "").replace("\\", "/").lower() + "/"
    return any(h in p for h in VENDOR_DIR_HINTS)


def is_test_support_file(path: str) -> bool:
    """Support file inside a test folder (fixture/helper/config) — NOT a test."""
    p = "/" + (path or "").replace("\\", "/").lower()
    return matches_any(p, TEST_SUPPORT_PATTERNS)


def is_test_path(path: str) -> bool:
    p = "/" + (path or "").replace("\\", "/").lower()
    if is_vendor_path(path):
        return False
    if is_test_support_file(path):
        return False
    # top-level test/tests/spec folder (full segment match — names like
    # "testimonials" or "test_data_loader.py" no longer match incorrectly)
    if re.match(r"^/(tests?|specs?|__tests__|e2e|cypress)/", p):
        return True
    if any(h in p for h in TEST_DIR_HINTS):
        return True
    return matches_any(p, TEST_FILE_PATTERNS, flags=re.IGNORECASE)


def is_code_file(path: str) -> bool:
    p = (path or "").lower()
    if is_vendor_path(path):
        return False
    if p.endswith(GENERATED_FILE_SUFFIXES):
        return False
    return any(p.endswith(ext) for ext in CODE_EXTENSIONS)


def analyze_test_content(content: str) -> dict:
    """
    Genuineness analysis of a test file's content.
    verdict: 'genuine' | 'suspicious' | 'not_a_test'
    """
    content = content[:2_000_000]   # cap regex work on pathological files
    cases = sum(len(re.findall(p, content)) for p in TEST_CASE_PATTERNS)
    assertions = sum(len(re.findall(p, content)) for p in ASSERTION_PATTERNS)
    trivial = sum(len(re.findall(p, content)) for p in TRIVIAL_ASSERTION_PATTERNS)
    skipped = sum(len(re.findall(p, content)) for p in SKIP_PATTERNS)

    cleaned = content
    for p in TRIVIAL_ASSERTION_PATTERNS:
        cleaned = re.sub(p, "", cleaned)
    real_assertions = sum(len(re.findall(p, cleaned)) for p in ASSERTION_PATTERNS)

    if cases == 0:
        verdict = "not_a_test"
    elif real_assertions == 0:
        verdict = "suspicious"
    elif skipped >= cases:
        verdict = "suspicious"
    else:
        verdict = "genuine"
    return {"cases": cases, "assertions": assertions, "trivial": trivial,
            "skipped": skipped, "verdict": verdict}


# ---------------------------------------------------------------------------
# HTTP base client (used by both GitLab and GitHub)
# ---------------------------------------------------------------------------

def _retry_after_seconds(r, default=30):
    """Parse Retry-After safely (it may be seconds OR an HTTP date)."""
    v = r.headers.get("Retry-After", "")
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


class HttpBase:
    def __init__(self, api_base, headers, workers=8):
        if not HAS_REQUESTS:
            raise SystemExit("ERROR: install this first ->  pip install requests")
        self.api = api_base.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=workers * 2,
                              pool_maxsize=workers * 2, max_retries=2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(headers)

    def _rate_limit_wait(self, r):
        """How long to wait on a 429/403 rate limit. None = not a rate limit."""
        raise NotImplementedError

    def get(self, path, params=None):
        url = path if path.startswith("http") else self.api + path
        for attempt in range(6):
            try:
                r = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                # transient network error — back off and retry instead of
                # killing the whole analysis run
                if attempt == 5:
                    print(f"  [warn] network error, giving up on {url}: {e}")
                    return None
                time.sleep(min(2 ** attempt, 30))
                continue
            wait = self._rate_limit_wait(r)
            if wait is not None:
                print(f"  [rate limit] {wait}s wait...")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise SystemExit(
                    "ERROR: Access denied (401/403). Check the token — "
                    "the scope must be correct, or the repo is private.")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        return None


# ---------------------------------------------------------------------------
# GitLab provider
# ---------------------------------------------------------------------------

def enc(project_path):
    """group/subgroup/repo -> URL-encoded ID (numeric IDs pass through as-is)."""
    s = str(project_path)
    return s if s.isdigit() else quote(s, safe="")


class GitLabProvider(HttpBase):
    kind = "gitlab"
    mr_word = "MRs"

    def __init__(self, base_url="https://gitlab.com", token=None, workers=8):
        headers = {"User-Agent": "codebase-analyzer"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        super().__init__(base_url.rstrip("/") + "/api/v4", headers, workers)

    def _rate_limit_wait(self, r):
        if r.status_code == 429:
            return _retry_after_seconds(r, default=30)
        return None

    def paginate(self, path, params=None, limit=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        params.setdefault("page", 1)
        results = []
        while True:
            try:
                r = self.get(path, params=params)
            except SystemExit:
                raise
            except Exception as e:
                print(f"  [warn] pagination stopped early: {e}")
                break
            if r is None:
                break
            try:
                data = r.json()
            except ValueError:
                break
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            if len(results) and len(results) % 1000 == 0:
                print(f"    ...{len(results)} items fetched")
            if limit and len(results) >= limit:
                return results[:limit]
            nxt = r.headers.get("X-Next-Page", "")
            if not nxt:
                break
            params["page"] = nxt
        return results

    # --- normalized interface ---

    def list_group_projects(self, group):
        gp = self.paginate(f"/groups/{enc(group)}/projects",
                           params={"include_subgroups": "true",
                                   "archived": "false"})
        return [p["id"] for p in gp]

    def project_info(self, project_id):
        pid = enc(project_id)
        r = self.get(f"/projects/{pid}", params={"statistics": "true"})
        if r is None:
            return None
        proj = r.json()
        stats = proj.get("statistics") or {}
        commits = stats.get("commit_count", "")
        if commits == "":
            rc = self.get(f"/projects/{pid}/repository/commits",
                          params={"per_page": 1})
            if rc is not None:
                total = rc.headers.get("X-Total")
                commits = int(total) if total and total.isdigit() else ""
        return {
            "id": pid,
            "name": proj.get("path_with_namespace", str(project_id)),
            "visibility": proj.get("visibility", ""),
            "created_at": (proj.get("created_at") or "")[:10],
            "last_activity": (proj.get("last_activity_at") or "")[:10],
            "default_branch": proj.get("default_branch") or "main",
            "commits": commits,
            "size_bytes": stats.get("repository_size", 0) or 0,
            "web_url": proj.get("web_url", ""),
        }

    def languages(self, info):
        lr = self.get(f"/projects/{info['id']}/languages")
        return lr.json() if lr else {}

    def contributors(self, info):
        contribs = self.paginate(
            f"/projects/{info['id']}/repository/contributors")
        return [{"name": c.get("name", ""), "id": c.get("email", "")}
                for c in contribs]

    def commit_log(self, info, limit):
        """Recent commits: [{author, email, message}] — for LLM detection."""
        commits = self.paginate(
            f"/projects/{info['id']}/repository/commits",
            params={"ref_name": info["default_branch"]},
            limit=limit or None)
        return [{"author": c.get("author_name", ""),
                 "email": c.get("author_email", ""),
                 "message": c.get("message") or c.get("title", ""),
                 "sha": c.get("id", "")}   # exact commit — for LLM location
                for c in commits]

    def merge_requests(self, info, limit):
        mrs = self.paginate(f"/projects/{info['id']}/merge_requests",
                            params={"state": "all", "order_by": "created_at",
                                    "sort": "desc"},
                            limit=limit or None)
        out = []
        for m in mrs:
            out.append({
                "number": m["iid"],
                "title": m.get("title", ""),
                "description": m.get("description") or "",
                "author": (m.get("author") or {}).get("username", ""),
                "merged": m.get("state") == "merged",
                "notes": m.get("user_notes_count", 0),
                "url": m.get("web_url", ""),   # exact MR location
            })
        return out

    def mr_analysis(self, info, mr):
        """Return: {notes, n_files, loc, touches_tests}"""
        n_files, loc, touches_tests = 0, 0, False
        dr = self.get(
            f"/projects/{info['id']}/merge_requests/{mr['number']}/diffs",
            params={"per_page": 100})
        if dr is not None:
            diffs = dr.json()
            n_files = len(diffs)
            for d in diffs:
                if is_test_path(d.get("new_path") or d.get("old_path") or ""):
                    touches_tests = True
                diff_text = d.get("diff", "") or ""
                loc += sum(1 for line in diff_text.splitlines()
                           if (line.startswith("+") or line.startswith("-"))
                           and not line.startswith(("+++", "---")))
        return {"notes": mr.get("notes", 0), "n_files": n_files,
                "loc": loc, "touches_tests": touches_tests}

    def tree(self, info):
        pid, ref = info["id"], info["default_branch"]
        tree = self.paginate(f"/projects/{pid}/repository/tree",
                             params={"recursive": "true", "ref": ref})
        if not tree:
            print("    [warn] tree came back empty, retrying with HEAD ref...")
            tree = self.paginate(f"/projects/{pid}/repository/tree",
                                 params={"recursive": "true"})
        return [t["path"] for t in tree if t.get("type") == "blob"]

    def file_content(self, info, path):
        fr = self.get(
            f"/projects/{info['id']}/repository/files/{quote(path, safe='')}/raw",
            params={"ref": info["default_branch"]})
        return fr.text if fr is not None else None

    def loc_estimate(self, info, code_paths):
        # rough estimate ~35 bytes/line (git objects are compressed)
        b = info.get("size_bytes", 0)
        return int(b / 35) if b else ""

    def ci_stats(self, info):
        """GitLab pipelines: total, recent success rate, duration, coverage."""
        pid = info["id"]
        out = {"pipelines_total": "", "success_rate": "",
               "avg_duration_min": "", "coverage_pct": ""}
        r = self.get(f"/projects/{pid}/pipelines", params={"per_page": 100})
        if r is None:
            return out
        pipelines = r.json()
        total = r.headers.get("X-Total")
        out["pipelines_total"] = (int(total) if total and total.isdigit()
                                  else len(pipelines))
        finished = [p for p in pipelines
                    if p.get("status") in ("success", "failed")]
        if finished:
            succ = len([p for p in finished if p["status"] == "success"])
            out["success_rate"] = round(100 * succ / len(finished), 1)
        # Duration + coverage from the latest successful pipelines
        durations = []
        for p in pipelines[:10]:
            if p.get("status") != "success":
                continue
            pd_ = self.get(f"/projects/{pid}/pipelines/{p['id']}")
            if pd_ is None:
                continue
            det = pd_.json()
            if det.get("duration"):
                durations.append(det["duration"])
            cov = det.get("coverage")
            if cov and out["coverage_pct"] == "":
                try:
                    out["coverage_pct"] = round(float(cov), 1)
                except (TypeError, ValueError):
                    pass
            if durations and out["coverage_pct"] != "":
                break
        if durations:
            out["avg_duration_min"] = round(sum(durations) / len(durations) / 60, 1)
        return out


# ---------------------------------------------------------------------------
# GitHub provider
# ---------------------------------------------------------------------------

class GitHubProvider(HttpBase):
    kind = "github"
    mr_word = "PRs"

    def __init__(self, base_url="https://api.github.com", token=None, workers=8):
        headers = {"User-Agent": "codebase-analyzer",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(base_url, headers, workers)

    def _rate_limit_wait(self, r):
        if r.status_code in (403, 429):
            if r.headers.get("Retry-After"):
                return _retry_after_seconds(r, default=60)
            if r.headers.get("X-RateLimit-Remaining") == "0":
                try:
                    reset = int(r.headers.get("X-RateLimit-Reset", 0))
                except (TypeError, ValueError):
                    reset = 0
                wait = max(5, reset - int(time.time()) + 2)
                return min(wait, 3600)
        return None

    def paginate(self, path, params=None, limit=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        params.setdefault("page", 1)
        results = []
        while True:
            try:
                r = self.get(path, params=params)
            except SystemExit:
                raise
            except Exception as e:
                print(f"  [warn] pagination stopped early: {e}")
                break
            if r is None:
                break
            try:
                data = r.json()
            except ValueError:
                break
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            if len(results) and len(results) % 1000 == 0:
                print(f"    ...{len(results)} items fetched")
            if limit and len(results) >= limit:
                return results[:limit]
            if len(data) < int(params["per_page"]):
                break
            params["page"] = int(params["page"]) + 1
        return results

    # --- normalized interface ---

    def list_group_projects(self, org):
        repos = self.paginate(f"/orgs/{org}/repos", params={"type": "all"})
        if not repos:  # maybe it's a user account, not an org
            repos = self.paginate(f"/users/{org}/repos")
        return [r["full_name"] for r in repos if not r.get("archived")]

    def project_info(self, project_id):
        r = self.get(f"/repos/{project_id}")
        if r is None:
            return None
        proj = r.json()
        full = proj.get("full_name", str(project_id))
        # Commit count: per_page=1, then read the last page number from the Link header
        commits = ""
        rc = self.get(f"/repos/{full}/commits", params={"per_page": 1})
        if rc is not None:
            link = rc.headers.get("Link", "")
            m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
            if m:
                commits = int(m.group(1))
            elif rc.json():
                commits = 1
        return {
            "id": full,
            "name": full,
            "visibility": "private" if proj.get("private") else "public",
            "created_at": (proj.get("created_at") or "")[:10],
            "last_activity": (proj.get("pushed_at")
                              or proj.get("updated_at") or "")[:10],
            "default_branch": proj.get("default_branch") or "main",
            "commits": commits,
            "size_bytes": (proj.get("size", 0) or 0) * 1024,  # size is in KB
            "license_hint": ((proj.get("license") or {}).get("spdx_id")
                             or "").replace("NOASSERTION", ""),
            "web_url": proj.get("html_url", ""),
        }

    def languages(self, info):
        lr = self.get(f"/repos/{info['id']}/languages")
        return lr.json() if lr else {}

    def contributors(self, info):
        contribs = self.paginate(f"/repos/{info['id']}/contributors",
                                 params={"anon": "false"})
        return [{"name": c.get("login", ""), "id": c.get("login", "")}
                for c in contribs]

    def commit_log(self, info, limit):
        """Recent commits: [{author, email, message}] — for LLM detection."""
        commits = self.paginate(f"/repos/{info['id']}/commits",
                                limit=limit or None)
        out = []
        for c in commits:
            cc = c.get("commit") or {}
            author = ((c.get("author") or {}).get("login")
                      or (cc.get("author") or {}).get("name") or "")
            out.append({"author": author,
                        "email": (cc.get("author") or {}).get("email", ""),
                        "message": cc.get("message", ""),
                        "sha": c.get("sha", "")})   # exact commit
        return out

    def merge_requests(self, info, limit):
        prs = self.paginate(f"/repos/{info['id']}/pulls",
                            params={"state": "all", "sort": "created",
                                    "direction": "desc"},
                            limit=limit or None)
        out = []
        for p in prs:
            out.append({
                "number": p["number"],
                "title": p.get("title", ""),
                "description": p.get("body") or "",
                "author": (p.get("user") or {}).get("login", ""),
                "merged": bool(p.get("merged_at")),
                "notes": None,   # not in the list endpoint; only in detail
                "url": p.get("html_url", ""),   # exact PR location
            })
        return out

    def mr_analysis(self, info, mr):
        notes, n_files, loc, touches_tests = 0, 0, 0, False
        # the /files endpoint returns filenames plus additions/deletions
        fr = self.get(f"/repos/{info['id']}/pulls/{mr['number']}/files",
                      params={"per_page": 100})
        if fr is not None:
            files = fr.json()
            n_files = len(files)
            for f in files:
                if is_test_path(f.get("filename", "")):
                    touches_tests = True
                loc += (f.get("additions", 0) or 0) + (f.get("deletions", 0) or 0)
        # comment count comes from the PR detail endpoint
        dr = self.get(f"/repos/{info['id']}/pulls/{mr['number']}")
        if dr is not None:
            det = dr.json()
            notes = ((det.get("comments", 0) or 0)
                     + (det.get("review_comments", 0) or 0))
        return {"notes": notes, "n_files": n_files,
                "loc": loc, "touches_tests": touches_tests}

    def tree(self, info):
        r = self.get(f"/repos/{info['id']}/git/trees/"
                     f"{quote(info['default_branch'], safe='')}",
                     params={"recursive": "1"})
        if r is None:
            return []
        data = r.json()
        if data.get("truncated"):
            print("    [warn] repo is very large — got a truncated file tree")
        return [t["path"] for t in data.get("tree", [])
                if t.get("type") == "blob"]

    def file_content(self, info, path):
        url = (f"{self.api}/repos/{info['id']}/contents/{quote(path)}"
               f"?ref={quote(info['default_branch'], safe='')}")
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=60,
                                     headers={"Accept": "application/vnd.github.raw+json"})
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            wait = self._rate_limit_wait(r)
            if wait is not None:
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return None
            return r.text
        return None

    def loc_estimate(self, info, code_paths):
        b = info.get("size_bytes", 0)
        return int(b / 35) if b else ""

    def ci_stats(self, info):
        """GitHub Actions: workflow runs total, success rate, duration."""
        out = {"pipelines_total": "", "success_rate": "",
               "avg_duration_min": "", "coverage_pct": ""}
        r = self.get(f"/repos/{info['id']}/actions/runs",
                     params={"per_page": 100})
        if r is None:
            return out
        data = r.json()
        out["pipelines_total"] = data.get("total_count", "")
        runs = data.get("workflow_runs", [])
        finished = [x for x in runs
                    if x.get("conclusion") in ("success", "failure")]
        if finished:
            succ = len([x for x in finished if x["conclusion"] == "success"])
            out["success_rate"] = round(100 * succ / len(finished), 1)
        durations = []
        for x in runs[:20]:
            try:
                s = datetime.fromisoformat(
                    x["run_started_at"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(
                    x["updated_at"].replace("Z", "+00:00"))
                d = (e - s).total_seconds()
                if 0 < d < 6 * 3600:
                    durations.append(d)
            except (KeyError, TypeError, ValueError):
                continue
        if durations:
            out["avg_duration_min"] = round(sum(durations) / len(durations) / 60, 1)
        # GitHub doesn't natively expose coverage — will stay blank
        return out


# ---------------------------------------------------------------------------
# Local / Offline provider (uses git commands — no internet required)
# ---------------------------------------------------------------------------

class LocalProvider:
    kind = "local"
    mr_word = "MRs/PRs (from git history)"

    def __init__(self, workers=8, mr_mode="auto"):
        self.workers = workers
        self.mr_mode = mr_mode   # auto | strict | merges | off

    def _git(self, repo, *args, check=True):
        try:
            r = subprocess.run(["git", "-C", repo] + list(args),
                               capture_output=True, text=True, timeout=300,
                               errors="replace")
            if check and r.returncode != 0:
                return None
            return r.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def list_group_projects(self, group):
        # in local mode a "group" = a folder containing multiple repos
        out = []
        for entry in sorted(os.listdir(group)):
            p = os.path.join(group, entry)
            if os.path.isdir(os.path.join(p, ".git")):
                out.append(p)
        return out

    def project_info(self, path):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            print(f"  folder '{path}' not found")
            return None
        has_git = os.path.isdir(os.path.join(path, ".git"))

        if not has_git:
            # ---- PLAIN FOLDER MODE (no git history) ----
            # Code from a zip extract / copy-paste — git-based fields
            # (commits, contributors, MRs) will stay blank, everything else works.
            # LLM detection will use tool configs + code comments.
            print("  [note] '.git' not found — plain-folder mode. "
                  "Commits/contributors/MRs will stay blank; "
                  "LLM detection will use code comments + tool configs.")
            mtimes = []
            for i, p in enumerate(self._walk_files(path)):
                if i >= 5000:
                    break
                try:
                    mtimes.append(os.path.getmtime(os.path.join(path, p)))
                except OSError:
                    pass
            fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d")
            return {
                "id": path, "name": os.path.basename(path),
                "visibility": "local (no git)",
                "created_at": fmt(min(mtimes)) if mtimes else "",
                "last_activity": fmt(max(mtimes)) if mtimes else "",
                "default_branch": "", "commits": "", "size_bytes": 0,
                "no_git": True,
            }

        branch = (self._git(path, "rev-parse", "--abbrev-ref", "HEAD")
                  or "HEAD").strip()
        commits_out = self._git(path, "rev-list", "--count", "HEAD")
        commits = int(commits_out.strip()) if commits_out else ""
        first = self._git(path, "log", "--max-parents=0", "--format=%as")
        created = min(first.split()) if first and first.strip() else ""
        last = (self._git(path, "log", "-1", "--format=%as") or "").strip()
        # Repo name from remote, otherwise folder name
        remote = (self._git(path, "remote", "get-url", "origin",
                            check=False) or "").strip()
        name = os.path.basename(path)
        m = re.search(r"[:/]([^/:]+/[^/]+?)(\.git)?$", remote)
        if m:
            name = m.group(1)
        return {
            "id": path, "name": name, "visibility": "local",
            "created_at": created, "last_activity": last,
            "default_branch": branch, "commits": commits, "size_bytes": 0,
        }

    @staticmethod
    def _walk_files(root, cap=50000):
        """All files in a plain folder (relative paths) — skips vendor/hidden."""
        skip_dirs = {".git", "node_modules", "vendor", "dist", "build",
                     "__pycache__", ".venv", "venv", ".idea", ".vscode",
                     "bower_components", ".tox", ".mypy_cache", "target"}
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                out.append(rel.replace("\\", "/"))
                if len(out) >= cap:
                    return out
        return out

    def languages(self, info):
        langs = Counter()
        for p in self.tree(info):
            pl = p.lower()
            if is_vendor_path(p):
                continue
            # Minified/compiled assets skew the language stats
            # (GitLab's linguist excludes them as well)
            if pl.endswith((".min.js", ".min.css", ".map",
                            ".bundle.js", ".chunk.js")):
                continue
            if pl.endswith(".blade.php"):
                lang = "Blade"
            else:
                lang = EXT_LANG.get(os.path.splitext(pl)[1])
            if lang:
                fp = os.path.join(info["id"], p)
                try:
                    langs[lang] += os.path.getsize(fp)
                except OSError:
                    pass
        return dict(langs)

    def contributors(self, info):
        if info.get("no_git"):
            return []
        out = self._git(info["id"], "log", "--format=%an%x01%ae") or ""
        seen = {}
        for line in out.splitlines():
            parts = line.split("\x01")
            if len(parts) == 2:
                name, email = parts
                seen.setdefault(email.lower(), name)
        return [{"name": n, "id": e} for e, n in seen.items()]

    def commit_log(self, info, limit):
        """Recent commits: [{author, email, message, sha}] — FULL message
        (including trailers/Co-Authored-By) + full commit hash (`%H`), so the
        exact commit containing an LLM signature can be pinpointed
        (`git show <sha>` jumps straight to that commit)."""
        if info.get("no_git"):
            return []
        args = ["log", "--format=%H%x01%an%x01%ae%x01%B%x02"]
        if limit:
            args.insert(1, f"-{limit}")
        out = self._git(info["id"], *args) or ""
        commits = []
        for chunk in out.split("\x02"):
            chunk = chunk.strip("\n")
            if not chunk.strip():
                continue
            parts = chunk.split("\x01", 3)
            if len(parts) == 4:
                commits.append({"sha": parts[0], "author": parts[1],
                                "email": parts[2], "message": parts[3]})
        return commits

    # --- MR/PR detection from git history ---
    # THE TRUTH IS: a local clone does NOT contain the real MR/PR database —
    # only their traces in commit messages. Therefore:
    #
    #   EXPLICIT references (100% certainly was an MR/PR):
    #     - GitHub merge:  "Merge pull request #123 from ..."
    #     - GitHub squash: subject "... (#123)"
    #     - GitLab merge:  body has "See merge request group/proj!123"
    #                      (GitLab's merge button writes this trailer itself)
    #     - GitLab squash: subject "... (!123)"
    #
    #   PLAIN merge commits ("Merge branch 'x'"):
    #     could be an MR, a dev's local merge, or a release merge —
    #     impossible to say for sure. This was the real cause of over-counting.
    #
    # Modes (--local-mrs):
    #   auto  (default): if explicit refs exist count ONLY those
    #                    (drop plain merges) — otherwise fall back to plain merges
    #   strict         : only explicit refs (most accurate, may under-count)
    #   merges         : explicit + plain merges (the old loose behaviour)
    #   off            : MR fields blank
    # Sync/pull merges are excluded in every mode. Unique numbers deduped.

    _PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+)", re.IGNORECASE)
    _SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
    _GITLAB_MR_RE = re.compile(r"See merge request [\w./ -]*!(\d+)",
                               re.IGNORECASE)
    _GITLAB_SQUASH_RE = re.compile(r"\(!(\d+)\)\s*$")
    _SYNC_MERGE_RE = re.compile(
        r"^Merge (remote-tracking branch|tag )"
        r"|^Merge branch '[^']+' (of |into (?!'?(master|main)\b))",
        re.IGNORECASE)

    def _extract_ref(self, subject, body, is_merge):
        m = self._PR_MERGE_RE.search(subject)
        if m:
            return "PR#" + m.group(1)
        g = self._GITLAB_MR_RE.search(subject + "\n" + body)
        if g:
            return "MR!" + g.group(1)
        if not is_merge:
            m = self._SQUASH_PR_RE.search(subject)
            if m:
                return "PR#" + m.group(1)
            g = self._GITLAB_SQUASH_RE.search(subject)
            if g:
                return "MR!" + g.group(1)
        return None

    _MR_REF_PATTERNS = [
        "refs/merge-requests/*/head",
        "refs/remotes/origin/merge-requests/*/head",
        "refs/remotes/origin/merge-requests/*",
        "refs/pull/*/head",
        "refs/remotes/origin/pull/*/head",
        "refs/remotes/origin/pr/*",
    ]

    def _mrs_from_refs(self, info, limit):
        """EXACT MR/PR list — if the user fetched the server's MR refs.
        GitLab keeps each MR head at 'refs/merge-requests/N/head', GitHub
        at 'refs/pull/N/head'. Once fetched, the full MR history becomes
        available offline. Returns None if no refs were found."""
        fmt = ("%(refname)%01%(objectname)%01%(authorname)%01"
               "%(subject)%01%(contents:body)%02")
        out = self._git(info["id"], "for-each-ref", f"--format={fmt}",
                        *self._MR_REF_PATTERNS, check=False)
        if not out or not out.strip():
            return None
        # Which MRs are merged: the ones whose heads are reachable from HEAD
        merged_out = self._git(info["id"], "for-each-ref", "--merged", "HEAD",
                               "--format=%(refname)",
                               *self._MR_REF_PATTERNS, check=False) or ""
        merged_refs = set(merged_out.split())

        mrs = []
        for rec in out.split("\x02"):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            parts = rec.split("\x01")
            if len(parts) < 4:
                continue
            refname, sha, author, subject = (parts[0].strip(), parts[1],
                                             parts[2], parts[3])
            body = parts[4] if len(parts) > 4 else ""
            if refname.endswith("/merge"):   # skip GitHub's test-merge refs
                continue
            m = re.search(r"(?:merge-requests|pull|pr)/(\d+)", refname)
            iid = int(m.group(1)) if m else 0
            mrs.append({"number": sha, "kind": "ref", "iid": iid,
                        "title": subject, "description": body,
                        "author": author,
                        "merged": refname in merged_refs, "notes": 0})
        if not mrs:
            return None
        mrs.sort(key=lambda x: x["iid"], reverse=True)   # newest first
        return mrs[:limit] if limit else mrs

    def merge_requests(self, info, limit):
        if self.mr_mode == "off" or info.get("no_git"):
            return []

        # Try the EXACT source first: fetched MR/PR refs
        ref_mrs = self._mrs_from_refs(info, limit)
        if ref_mrs:
            merged = len([m for m in ref_mrs if m["merged"]])
            print(f"    Found MR/PR refs — EXACT count: {len(ref_mrs)} "
                  f"({merged} merged/contained in HEAD). "
                  f"This is as accurate as the API.")
            return ref_mrs

        fmt = "%H%x01%P%x01%an%x01%s%x01%b%x02"
        out = self._git(info["id"], "log", "--first-parent",
                        f"--format={fmt}") or ""
        explicit, plain, seen = [], [], set()
        for rec in out.split("\x02"):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            parts = rec.split("\x01")
            if len(parts) < 4:
                continue
            sha, parents, author, subject = (parts[0].strip(), parts[1],
                                             parts[2], parts[3])
            body = parts[4] if len(parts) > 4 else ""
            is_merge = len(parents.split()) >= 2
            if is_merge and self._SYNC_MERGE_RE.search(subject):
                continue   # pull/sync/back-merge — not an MR
            num = self._extract_ref(subject, body, is_merge)
            mr = {"number": sha, "kind": "merge" if is_merge else "squash",
                  "title": subject, "description": body, "author": author,
                  "merged": True, "notes": 0}
            if num:
                if num in seen:
                    continue
                seen.add(num)
                explicit.append(mr)
            elif is_merge:
                plain.append(mr)
            # plain single-parent commit without a ref = direct push, skip

        # Choose according to the mode
        if self.mr_mode == "strict":
            mrs = explicit
        elif self.mr_mode == "merges":
            mrs = explicit + plain
        else:  # auto
            mrs = explicit if explicit else plain

        # Transparency: show the user what was counted and what was dropped
        print(f"    Detection: {len(explicit)} explicit MR/PR refs "
              f"(#N / !N / 'See merge request'), "
              f"{len(plain)} plain merge commits")
        print("    [note] Fast-forward/squash merged MRs leave no trace in "
              "git history — this count is a FLOOR, not exact.")
        print("    TIP: if you want exact MR history offline, run this once "
              "in the repo (with network access):")
        print("      GitLab: git fetch origin "
              "\"+refs/merge-requests/*/head:refs/remotes/origin/"
              "merge-requests/*/head\"")
        print("      GitHub: git fetch origin "
              "\"+refs/pull/*/head:refs/remotes/origin/pull/*/head\"")
        print("    — after that this script will analyze all MRs EXACTLY.")
        if self.mr_mode == "auto" and explicit and plain:
            print(f"    -> auto mode: only {len(explicit)} explicit refs "
                  f"counted; {len(plain)} plain merges DROPPED "
                  f"(to count everything use --local-mrs merges)")
        elif self.mr_mode == "auto" and not explicit and plain:
            print(f"    -> no explicit refs found; treated {len(plain)} plain "
                  f"merge commits as an MR proxy (approximate — "
                  f"for exact counts use --provider gitlab/github)")
        for s in [m["title"] for m in mrs[:3]]:
            print(f"       sample: {s[:70]}")

        if limit:
            mrs = mrs[:limit]
        return mrs

    def mr_analysis(self, info, mr):
        n_files, loc, touches_tests = 0, 0, False
        if mr.get("kind") == "ref":
            # Diff the MR head against its merge-base (with HEAD) — that
            # is exactly the change the MR proposed
            mb = self._git(info["id"], "merge-base", "HEAD", mr["number"],
                           check=False)
            base = mb.strip() if mb and mb.strip() else None
            out = (self._git(info["id"], "diff", "--numstat",
                             base, mr["number"], check=False)
                   if base else None)
        else:
            # merge commit: diff against first parent | squash: against its parent
            base = mr["number"] + ("^1" if mr.get("kind") == "merge" else "^")
            out = self._git(info["id"], "diff", "--numstat",
                            base, mr["number"], check=False)
        if out is None or out == "":
            # root commit / detached ref etc. — use the commit's own stat
            out = self._git(info["id"], "show", "--numstat", "--format=",
                            mr["number"], check=False)
        if out:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                add, dele, path = parts
                n_files += 1
                if is_test_path(path):
                    touches_tests = True
                try:
                    loc += int(add) + int(dele)
                except ValueError:
                    pass   # binary files show '-' here
        return {"notes": 0, "n_files": n_files, "loc": loc,
                "touches_tests": touches_tests}

    def tree(self, info):
        if not hasattr(self, "_tree_cache"):
            self._tree_cache = {}
        if info["id"] not in self._tree_cache:
            if info.get("no_git"):
                self._tree_cache[info["id"]] = self._walk_files(info["id"])
            else:
                out = self._git(info["id"], "ls-files") or ""
                paths = [p for p in out.splitlines() if p.strip()]
                # git exists but ls-files is empty (corrupt/bare?) -> walk fallback
                self._tree_cache[info["id"]] = (paths or
                                                self._walk_files(info["id"]))
        return self._tree_cache[info["id"]]

    # Read at most 5 MB per file — enough for every real source file, and it
    # keeps one giant generated/binary-ish file from blowing up memory.
    _MAX_READ_BYTES = 5_000_000

    def file_content(self, info, path):
        fp = os.path.join(info["id"], path)
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                return f.read(self._MAX_READ_BYTES)
        except OSError:
            return None

    def loc_estimate(self, info, code_paths):
        """ACTUAL line count for a local repo (not an estimate) — max 20k files."""
        total = 0
        for p in code_paths[:20000]:
            fp = os.path.join(info["id"], p)
            try:
                with open(fp, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        total += chunk.count(b"\n")
            except OSError:
                pass
        if len(code_paths) > 20000:
            total = int(total * len(code_paths) / 20000)
        return total

    def ci_stats(self, info):
        """Offline there is no pipeline history — we can only count CI config
        commits (a proxy for how actively it is maintained)."""
        out = {"pipelines_total": "", "success_rate": "",
               "avg_duration_min": "", "coverage_pct": ""}
        ci_paths = []
        for system, paths in detect_ci_configs(self.tree(info)).items():
            ci_paths.extend(paths)
        if ci_paths:
            log = self._git(info["id"], "log", "--oneline", "--",
                            *ci_paths[:20], check=False) or ""
            n = len([l for l in log.splitlines() if l.strip()])
            out["ci_commits"] = n
        return out


# ---------------------------------------------------------------------------
# Project analysis (provider-agnostic)
# ---------------------------------------------------------------------------

def analyze_project(prov, project_id, max_mrs, sample_mrs,
                    max_test_files, workers=8, max_commit_scan=0,
                    max_ai_file_scan=0, max_quality_scan=0):
    info = prov.project_info(project_id)
    if info is None:
        print(f"  Project not found: {project_id}")
        return None
    name = info["name"]
    print(f"\n=== {name} ===")

    result = {
        "repo": name,
        "provider": prov.kind,
        "visibility": info["visibility"],
        "created_at": info["created_at"],
        "last_activity": info["last_activity"],
        "default_branch": info["default_branch"],
        "commits": info["commits"],
    }

    # Languages
    langs = prov.languages(info)
    result["languages"] = langs
    result["primary_language"] = max(langs, key=langs.get) if langs else ""

    # Contributors
    print("  Contributors...")
    contribs = prov.contributors(info)
    result["contributors"] = len(contribs)
    result["human_contributors"] = len(
        [c for c in contribs
         if not is_bot((c.get("name") or "") + " " + (c.get("id") or ""))])

    # MRs / PRs
    print(f"  {prov.mr_word} fetch"
          + (f" (max {max_mrs})..." if max_mrs else " (ALL)..."))
    mrs = prov.merge_requests(info, max_mrs)

    if not mrs and prov.kind == "local":
        # An offline repo has no real MR/PR history — only their traces
        # can be detected from git history. Showing 0 when nothing was found
        # would be wrong — hence blank + a note.
        print("    [note] no MR/PR detected in git history — "
              "MR fields will stay blank. (To also count merge commits use "
              "--local-mrs merges; for exact counts use --provider "
              "gitlab/github)")
        result["total_mrs"] = ""
        result["merged_mrs"] = ""
        result["mrs_note"] = "not detectable from git history"
        for key in ("pct_simple", "pct_standard", "pct_rich",
                    "pct_automated", "pct_other", "avg_loc_per_mr"):
            result[key] = ""
        result["mr_sample_size"] = 0
    else:
        result["total_mrs"] = len(mrs)
        result["merged_mrs"] = len([m for m in mrs if m.get("merged")])
        if prov.kind == "local":
            result["mrs_note"] = ("estimated from git history — "
                                  "for an exact count use the GitLab/GitHub "
                                  "API provider")

    # MR categorization — sample_mrs=0 means ALL will be deep-analyzed
    if mrs:
        _categorize_mrs(prov, info, mrs, sample_mrs, workers, result)

    # Fetch the file tree only once (used by files/CI/tests and LLM detection)
    print("  Repository file tree...")
    all_paths = prov.tree(info)
    content_cache = {}   # avoid fetching a file twice (LLM + quality scan)

    _analyze_files_ci_tests(prov, info, all_paths, max_test_files,
                            workers, result)
    _analyze_llm_usage(prov, info, mrs, all_paths, result,
                       max_commit_scan, max_ai_file_scan, workers,
                       content_cache)
    _analyze_training_quality(prov, info, all_paths, result,
                              max_quality_scan, workers, content_cache)
    return result


def _categorize_mrs(prov, info, mrs, sample_mrs, workers, result):
    """Split MRs/PRs into simple/standard/rich/automated (writes into result)."""
    to_analyze = mrs if not sample_mrs else mrs[:sample_mrs]
    print(f"  Categorization ({len(to_analyze)} {prov.mr_word}, "
          f"{workers} parallel)...")
    cats = Counter()
    loc_list = []

    def classify_mr(mr):
        if is_bot(mr.get("author", "")):
            return "automated", 0
        title = mr.get("title", "")
        desc = (mr.get("description") or "") + " " + title
        linked = matches_any(desc, ISSUE_LINK_PATTERNS)

        a = prov.mr_analysis(info, mr)
        notes, n_files = a["notes"], a["n_files"]
        loc, touches_tests = a["loc"], a["touches_tests"]

        substantive = notes >= 3
        simple_title = matches_any(title, SIMPLE_FIX_TITLE_PATTERNS)

        if linked and substantive:
            cat = "rich"
        elif n_files <= 2 and notes == 0 and (simple_title or loc <= 50):
            cat = "simple"
        elif 3 <= n_files <= 10 and (touches_tests or linked):
            cat = "standard"
        elif n_files <= 2:
            cat = "simple"
        elif 3 <= n_files <= 10:
            cat = "standard"
        else:
            cat = "other"
        return cat, loc

    done = 0
    pool_workers = workers if prov.kind != "local" else min(workers, 4)
    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        futures = [pool.submit(classify_mr, mr) for mr in to_analyze]
        for fut in as_completed(futures):
            try:
                cat, loc = fut.result()
                cats[cat] += 1
                if loc:
                    loc_list.append(loc)
            except Exception as e:
                cats["other"] += 1
                print(f"    [warn] classify fail: {e}")
            done += 1
            if done % 100 == 0:
                print(f"    ...{done}/{len(to_analyze)} done")

    n = sum(cats.values()) or 1
    result["pct_simple"] = round(100 * cats["simple"] / n, 1)
    result["pct_standard"] = round(100 * cats["standard"] / n, 1)
    result["pct_rich"] = round(100 * cats["rich"] / n, 1)
    result["pct_automated"] = round(100 * cats["automated"] / n, 1)
    result["pct_other"] = round(100 * cats["other"] / n, 1)
    result["avg_loc_per_mr"] = (round(sum(loc_list) / len(loc_list), 1)
                                if loc_list else "")
    result["mr_sample_size"] = n


def _analyze_files_ci_tests(prov, info, all_paths, max_test_files,
                            workers, result):
    """File tree, CI/CD analysis, and test genuineness (writes into result)."""
    pool_workers = workers if prov.kind != "local" else min(workers, 4)

    # ---- Files: source + test detection ----
    all_code = [p for p in all_paths if is_code_file(p)]
    test_paths = [p for p in all_code if is_test_path(p)]
    source_paths = [p for p in all_code if not is_test_path(p)]
    result["source_files"] = len(source_paths)
    result["test_files"] = len(test_paths)
    result["total_loc_estimate"] = prov.loc_estimate(info, all_code)
    print(f"    {len(all_code)} code files: {len(source_paths)} source, "
          f"{len(test_paths)} test")

    # ---- CI/CD analysis (NEW) ----
    print("  CI/CD analysis...")
    ci_configs = detect_ci_configs(all_paths)
    result["ci_configured"] = "Yes" if ci_configs else "No"
    result["ci_systems"] = sorted(ci_configs.keys())
    result["ci_config_files"] = sum(len(v) for v in ci_configs.values())

    ci_jobs, ci_stages = 0, 0
    for system, paths in ci_configs.items():
        for cp in paths[:10]:          # read at most 10 configs per system
            content = prov.file_content(info, cp)
            if content:
                a = analyze_ci_config(system, content)
                ci_jobs += a["jobs"]
                ci_stages += a["stages"]
    result["ci_jobs_approx"] = ci_jobs if ci_configs else ""
    result["ci_stages_approx"] = ci_stages if ci_configs else ""

    stats = prov.ci_stats(info)
    result["ci_pipelines_total"] = stats.get("pipelines_total", "")
    result["ci_success_rate"] = stats.get("success_rate", "")
    result["ci_avg_duration_min"] = stats.get("avg_duration_min", "")
    result["coverage_pct"] = stats.get("coverage_pct", "")
    if "ci_commits" in stats:
        result["ci_commits"] = stats["ci_commits"]
    if ci_configs:
        print(f"    CI systems: {', '.join(result['ci_systems'])} "
              f"({result['ci_config_files']} config files, "
              f"~{ci_jobs} jobs)")
        if result["ci_success_rate"] != "":
            print(f"    Recent pipeline success rate: "
                  f"{result['ci_success_rate']}%")
    else:
        print("    No CI/CD config found")

    # ---- Test genuineness analysis (parallel content fetch) ----
    if max_test_files == 0:   # auto: ALL locally, 200 via API
        max_test_files = 10**9 if prov.kind == "local" else 200
    print(f"  Test genuineness check "
          f"({min(len(test_paths), max_test_files)} files)...")
    test_cases = 0
    assertions_total = 0
    genuine_files = 0
    suspicious_files = 0
    misnamed_files = 0
    suspicious_list = []

    def check_test_file(tp):
        content = prov.file_content(info, tp)
        if content is None:
            return tp, None
        return tp, analyze_test_content(content)

    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        futures = [pool.submit(check_test_file, tp)
                   for tp in test_paths[:max_test_files]]
        for fut in as_completed(futures):
            try:
                tp, tinfo = fut.result()
            except Exception:
                continue
            if tinfo is None:
                continue
            test_cases += tinfo["cases"]
            assertions_total += tinfo["assertions"]
            if tinfo["verdict"] == "genuine":
                genuine_files += 1
            elif tinfo["verdict"] == "suspicious":
                suspicious_files += 1
                suspicious_list.append(
                    {"file": tp, "cases": tinfo["cases"],
                     "assertions": tinfo["assertions"],
                     "trivial": tinfo["trivial"], "skipped": tinfo["skipped"]})
            else:
                misnamed_files += 1

    checked = min(len(test_paths), max_test_files)
    if len(test_paths) > max_test_files and checked:
        scale = len(test_paths) / checked
        test_cases = int(test_cases * scale)
        result["test_cases_note"] = "extrapolated"

    # ---- Content-based correction ----
    # A file living in a test folder that contains NOT A SINGLE test case
    # (misnamed) is really a source/support file. Adjust the counts so that
    # "Test files" only includes actual test files.
    if checked:
        scale = len(test_paths) / checked
        est_misnamed = int(round(misnamed_files * scale))
        result["test_files"] = max(0, len(test_paths) - est_misnamed)
        result["source_files"] = len(source_paths) + est_misnamed
        if est_misnamed:
            print(f"    [adjust] {est_misnamed} files were in test folders but "
                  f"contained no test cases — moved to source")

    result["test_cases"] = test_cases
    result["assertions"] = assertions_total
    result["genuine_test_files"] = genuine_files
    result["suspicious_test_files"] = suspicious_files
    result["misnamed_test_files"] = misnamed_files
    # % genuine = share of genuine among real test files (genuine+suspicious).
    # Misnamed files used to be in the denominator too — the % came out too low.
    real_tests_checked = genuine_files + suspicious_files
    result["pct_genuine_tests"] = (
        round(100 * genuine_files / real_tests_checked, 1)
        if real_tests_checked else "")
    result["suspicious_tests_detail"] = suspicious_list[:50]
    if suspicious_files:
        print(f"    [!] found {suspicious_files} SUSPICIOUS test files "
              f"(no real assertions / all skipped) — details in the JSON")
    return result


# ---------------------------------------------------------------------------
# AI / LLM usage analysis (NEW)
# ---------------------------------------------------------------------------

def _pct(num, den):
    """Percentage with adaptive precision — show 1/2201 as 0.05, not 0.0."""
    if not den:
        return ""
    p = 100 * num / den
    if num and round(p, 1) == 0.0:
        return max(round(p, 2), 0.01)
    return round(p, 1)


def _analyze_llm_usage(prov, info, mrs, all_paths, result,
                       max_commit_scan, max_ai_file_scan, workers,
                       content_cache=None):
    """Detect AI/LLM usage in the repo (writes into result).

    Sources:
      commits  -> trailers/messages/authors     (main basis for LLM usage %)
      MRs/PRs  -> descriptions/authors          (secondary %)
      tree     -> AI tool config files          (which tools are set up)
      code     -> attribution comments (sample) (extra evidence)
    """
    if content_cache is None:
        content_cache = {}
    print("  AI/LLM detection...")
    llm_commit_counts = Counter()   # LLM -> number of commits
    llm_mr_counts = Counter()       # LLM -> number of MRs
    evidence = []                   # samples for the detail JSON

    # ---- 1. Commits scan ----
    commits = []
    try:
        commits = prov.commit_log(info, max_commit_scan) or []
    except Exception as e:
        print(f"    [warn] commit scan fail: {e}")
    repo_web_url = (info.get("web_url") or "").rstrip("/")

    def commit_url(sha):
        """Link to the exact commit, if the web_url is known (GitLab/GitHub)."""
        if not sha or not repo_web_url:
            return ""
        if prov.kind == "gitlab":
            return f"{repo_web_url}/-/commit/{sha}"
        if prov.kind == "github":
            return f"{repo_web_url}/commit/{sha}"
        return ""

    ai_commits = 0
    for c in commits:
        hits = detect_llms_in_text(c.get("message", ""))
        bot = detect_llm_author((c.get("author", "") or "")
                                + " " + (c.get("email", "") or ""))
        if bot:
            hits.add(bot)
        if hits:
            ai_commits += 1
            for h in hits:
                llm_commit_counts[h] += 1
            if len(evidence) < 30:
                first_line = (c.get("message", "").splitlines() or [""])[0]
                sha = c.get("sha", "")
                # EXACT LOCATION: commit sha (+ web link when available)
                loc = sha[:12] if sha else ""
                url = commit_url(sha)
                if url:
                    loc = url
                evidence.append({"source": "commit",
                                 "llm": sorted(hits),
                                 "sample": first_line[:100],
                                 "location": loc})
    scanned = len(commits)
    result["llm_commits_scanned"] = scanned
    result["llm_ai_commits"] = ai_commits
    result["llm_pct_commits"] = _pct(ai_commits, scanned)

    # ---- 2. MRs/PRs scan (already fetched — no extra API calls) ----
    ai_mrs = 0
    for m in (mrs or []):
        text = (m.get("title", "") or "") + "\n" + (m.get("description", "") or "")
        hits = detect_llms_in_text(text)
        bot = detect_llm_author(m.get("author", ""))
        if bot:
            hits.add(bot)
        if hits:
            ai_mrs += 1
            for h in hits:
                llm_mr_counts[h] += 1
            if len(evidence) < 50:
                # EXACT LOCATION: MR/PR number + web link (if found)
                num = m.get("number", "")
                loc = m.get("url", "") or (f"#{num}" if num != "" else "")
                evidence.append({"source": "mr/pr",
                                 "llm": sorted(hits),
                                 "sample": (m.get("title", "") or "")[:100],
                                 "location": loc})
    n_mrs = len(mrs or [])
    result["llm_ai_mrs"] = ai_mrs if n_mrs else ""
    result["llm_pct_mrs"] = _pct(ai_mrs, n_mrs)

    # ---- 3. AI tool config files (from the tree — free) ----
    tool_configs = detect_ai_tool_configs(all_paths)
    result["ai_tool_configs"] = sorted(tool_configs.keys())
    result["ai_tool_config_files"] = sum(len(v) for v in tool_configs.values())
    for tool, paths in tool_configs.items():
        evidence.append({"source": "config-file", "llm": [tool],
                         "sample": "; ".join(paths[:3]),
                         "location": "; ".join(paths[:3])})

    # ---- 4. Code comments scan (sample of source files) ----
    if max_ai_file_scan == 0:   # auto
        if prov.kind == "local":
            # Reading from local disk is cheap — scan ALL code files
            max_ai_file_scan = 10**9
        else:
            max_ai_file_scan = 30
    code_paths = [p for p in all_paths if is_code_file(p)]
    llm_code_files = Counter()
    ai_code_files = 0
    files_scanned = 0
    if max_ai_file_scan > 0 and code_paths:
        # spread sample: take files from the start, middle and end
        step = max(1, len(code_paths) // max_ai_file_scan)
        sample = code_paths[::step][:max_ai_file_scan]
        pool_workers = workers if prov.kind != "local" else min(workers, 4)

        def scan_file(p):
            content = _fetch_cached(prov, info, p, content_cache)
            return p, (detect_llms_in_code(content) if content else None)

        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            futures = [pool.submit(scan_file, p) for p in sample]
            for fut in as_completed(futures):
                try:
                    p, hits = fut.result()
                except Exception:
                    continue
                if hits is None:
                    continue
                files_scanned += 1
                if hits:
                    ai_code_files += 1
                for h in hits:
                    llm_code_files[h] += 1   # file-level count (as before)
                if hits and len(evidence) < 80:
                    # EXACT LOCATION: file path + line number(s), e.g.
                    # "src/utils.py:3" — previously only the file name was
                    # available; now the exact line of the attribution shows too.
                    loc_parts = [f"{p}:{ln}" for name, lines in hits.items()
                                for ln in lines]
                    evidence.append({"source": "code-comment",
                                     "llm": sorted(hits),
                                     "sample": p[:100],
                                     "location": ", ".join(loc_parts[:5])})
    result["llm_code_files"] = ai_code_files
    result["llm_files_scanned"] = files_scanned

    # ---- Combine: which LLM, what % ----
    combined = Counter()
    for name, n in llm_commit_counts.items():
        combined[name] += n * 3        # commit trailer = strongest signal
    for name, n in llm_mr_counts.items():
        combined[name] += n * 2
    for name, n in llm_code_files.items():
        combined[name] += n
    for tool in tool_configs:
        combined[tool] += 1            # config file = tool was used (weak weight)

    result["llm_commit_breakdown"] = dict(llm_commit_counts)
    result["llm_mr_breakdown"] = dict(llm_mr_counts)
    result["llm_combined_score"] = dict(combined)
    result["llm_evidence"] = evidence[:80]

    detected = bool(combined)
    result["llm_detected"] = "Yes" if detected else "No"
    result["primary_llm"] = combined.most_common(1)[0][0] if detected else ""

    # Overall usage % = treat the commit-based % as primary; if no commits
    # use MRs; failing that too (e.g. no-git folder) use the code-file %
    if isinstance(result["llm_pct_commits"], (int, float)) and ai_commits:
        result["llm_usage_pct"] = result["llm_pct_commits"]
        result["llm_usage_basis"] = f"{ai_commits}/{scanned} commits"
    elif isinstance(result["llm_pct_mrs"], (int, float)) and ai_mrs:
        result["llm_usage_pct"] = result["llm_pct_mrs"]
        result["llm_usage_basis"] = f"{ai_mrs}/{n_mrs} MRs/PRs"
    elif ai_code_files and files_scanned:
        result["llm_usage_pct"] = _pct(ai_code_files, files_scanned)
        result["llm_usage_basis"] = (f"{ai_code_files}/{files_scanned} "
                                     f"source files (code comments)")
    elif detected:
        result["llm_usage_pct"] = ""   # only configs found — no % possible
        result["llm_usage_basis"] = "tool configs only"
    elif scanned or files_scanned:
        result["llm_usage_pct"] = 0.0
        basis = []
        if scanned:
            basis.append(f"0/{scanned} commits")
        if files_scanned:
            basis.append(f"0/{files_scanned} files")
        result["llm_usage_basis"] = ", ".join(basis)
    else:
        result["llm_usage_pct"] = ""
        result["llm_usage_basis"] = ""

    # ---- Console summary ----
    if detected:
        tot = sum(combined.values()) or 1
        breakdown = ", ".join(f"{n} {round(100*v/tot)}%"
                              for n, v in combined.most_common(4))
        print(f"    [AI] LLM detected: {result['primary_llm']} (primary)")
        print(f"         Breakdown: {breakdown}")
        if isinstance(result.get("llm_usage_pct"), (int, float)):
            print(f"         Usage: {result['llm_usage_pct']}% "
                  f"({result['llm_usage_basis']})")
        if tool_configs:
            print(f"         Tool configs: {', '.join(sorted(tool_configs))}")
        print("         NOTE: only explicit signatures are counted — "
              "real AI use may be higher (lower bound)")
    else:
        print(f"    No explicit AI/LLM signature found "
              f"({scanned} commits, {n_mrs} MRs, {files_scanned} files "
              f"scanned). Silent AI use cannot be detected.")


# ---------------------------------------------------------------------------
# Training-data quality analysis (NEW)
# ---------------------------------------------------------------------------

def _fetch_cached(prov, info, path, cache):
    if path not in cache:
        cache[path] = prov.file_content(info, path)
    return cache[path]


def _analyze_training_quality(prov, info, all_paths, result,
                              max_scan, workers, cache):
    """Post-training data-quality factors (writes into result):
    license, syntax validity, quality metrics, dedup, secrets/PII,
    eval contamination, composite score + suitability grade."""
    print("  Training-quality analysis...")

    # ---- 1. License detection ----
    license_name = info.get("license_hint") or ""
    lic_files = [p for p in all_paths
                 if LICENSE_FILE_RE.search(p) and p.count("/") == 0]
    if not license_name:
        for lp in lic_files[:2]:
            license_name = classify_license(_fetch_cached(prov, info, lp,
                                                          cache))
            if license_name:
                break
    if not license_name:
        # manifest fallback: package.json / pyproject / Cargo.toml
        for mf in ("package.json", "pyproject.toml", "Cargo.toml",
                   "composer.json", "setup.py"):
            if mf not in all_paths:
                continue
            c = _fetch_cached(prov, info, mf, cache) or ""
            m = re.search(r"""["']?license["']?\s*[:=]\s*["']([^"']{2,40})["']""",
                          c, re.IGNORECASE)
            if m:
                license_name = m.group(1).strip()
                break
    if license_name:
        risk = LICENSE_RISK.get(license_name, "unknown")
    elif lic_files:
        license_name, risk = "custom/unclassified", "unknown"
    else:
        license_name, risk = "NONE", "no-license (all rights reserved)"
    result["license"] = license_name
    result["license_risk"] = risk

    # ---- 2-6. In one pass: quality + dedup + secrets + contamination ----
    if max_scan == 0:   # auto
        # ALL code files locally; a sample via API (calls are costly)
        max_scan = 10**9 if prov.kind == "local" else 50
    code_paths = [p for p in all_paths if is_code_file(p)]
    step = max(1, len(code_paths) // max_scan) if code_paths else 1
    sample = code_paths[::step][:max_scan]
    pool_workers = workers if prov.kind != "local" else min(workers, 4)

    tot = Counter()
    sum_avg_len = 0.0
    hashes = Counter()
    secrets_found = []
    contam_files = []
    syntax_valid, syntax_checked = 0, 0
    files_scanned = 0

    def scan_one(p):
        content = _fetch_cached(prov, info, p, cache)
        if content is None:
            return None
        q = scan_file_quality(p, content)
        s, emails = scan_secrets_and_pii(content, p)
        return p, q, s, emails

    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        futures = [pool.submit(scan_one, p) for p in sample]
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception:
                continue
            if r is None:
                continue
            p, q, s, emails = r
            files_scanned += 1
            for k in ("lines", "code_lines", "long_lines", "comment_lines",
                      "funcs"):
                tot[k] += q[k]
            sum_avg_len += q["avg_len"]
            tot["docstrings"] += 1 if q["has_docstring"] else 0
            tot["py_files"] += 1 if p.endswith(".py") else 0
            tot["emails"] += emails
            if q["hash"]:
                hashes[q["hash"]] += 1
            if q["syntax_valid"] is not None:
                syntax_checked += 1
                syntax_valid += 1 if q["syntax_valid"] else 0
            if q["eval_contam"]:
                contam_files.append(p)
            secrets_found.extend(s)

    dup_files = sum(c - 1 for c in hashes.values() if c > 1)
    dup_pct = round(100 * dup_files / files_scanned, 1) if files_scanned else ""
    comment_ratio = (round(100 * tot["comment_lines"] / tot["lines"], 1)
                     if tot["lines"] else "")
    avg_line_len = (round(sum_avg_len / files_scanned, 1)
                    if files_scanned else "")
    pct_long = (round(100 * tot["long_lines"] / tot["lines"], 1)
                if tot["lines"] else "")
    avg_func_len = (round(tot["code_lines"] / tot["funcs"], 1)
                    if tot["funcs"] else "")
    syntax_pct = (round(100 * syntax_valid / syntax_checked, 1)
                  if syntax_checked else "")
    docstring_pct = (round(100 * tot["docstrings"] / tot["py_files"], 1)
                     if tot["py_files"] else "")

    result["quality_files_scanned"] = files_scanned
    result["syntax_valid_pct"] = syntax_pct
    result["avg_line_length"] = avg_line_len
    result["pct_long_lines"] = pct_long
    result["comment_ratio_pct"] = comment_ratio
    result["docstring_pct"] = docstring_pct
    result["avg_func_length"] = avg_func_len
    result["duplicate_files"] = dup_files
    result["duplicate_pct"] = dup_pct
    result["secrets_found"] = len(secrets_found)
    result["secrets_detail"] = secrets_found[:30]   # MASKED — never plain
    result["pii_emails"] = tot["emails"]
    result["eval_contamination_files"] = len(contam_files)
    result["eval_contamination_detail"] = contam_files[:20]

    # ---- 7. Composite score + Training suitability ----
    score = 100.0
    reasons = []
    if syntax_checked and syntax_valid < syntax_checked:
        bad = 100 * (1 - syntax_valid / syntax_checked)
        score -= min(40, bad * 0.5)
        reasons.append(f"{round(bad)}% Python files syntax-invalid")
    if isinstance(pct_long, (int, float)) and pct_long > 30:
        score -= 10
        reasons.append("very long lines (generated/obfuscated pattern)")
    if isinstance(comment_ratio, (int, float)):
        if comment_ratio < 2:
            score -= 10
            reasons.append("almost zero comments")
        elif comment_ratio > 60:
            score -= 5
            reasons.append("comment-heavy (auto-doc/generated pattern)")
    if isinstance(dup_pct, (int, float)) and dup_pct:
        score -= min(30, dup_pct)
        reasons.append(f"{dup_pct}% duplicate files")
    if secrets_found:
        score -= 15
        reasons.append(f"{len(secrets_found)} secrets/keys found")
    if isinstance(avg_func_len, (int, float)) and avg_func_len > 80:
        score -= 10
        reasons.append("very long functions (avg >80 lines)")
    if not result.get("test_files"):
        score -= 10
        reasons.append("no tests")
    elif (isinstance(result.get("pct_genuine_tests"), (int, float))
          and result["pct_genuine_tests"] < 50):
        score -= 10
        reasons.append("more than half the tests are suspicious")
    if contam_files:
        score -= 20
        reasons.append(f"eval-benchmark code in {len(contam_files)} files")
    score = max(0.0, round(score, 1))
    result["quality_score"] = score

    grade = ("A" if score >= 80 else "B" if score >= 60
             else "C" if score >= 40 else "D")
    # License cap: copyleft/no-license is risky for training use
    if risk.startswith("no-license"):
        if grade in ("A", "B"):
            grade = "C"
        reasons.append("no license — training use is risky")
    elif risk == "copyleft":
        if grade == "A":
            grade = "B"
        reasons.append(f"copyleft license ({license_name})")
    result["training_suitability"] = grade
    result["quality_reasons"] = reasons

    # ---- Console ----
    print(f"    License: {license_name} ({risk})  [heuristic — not legal "
          f"advice]")
    print(f"    Quality: score {score}/100 -> suitability {grade} "
          f"({files_scanned} files scanned)")
    if isinstance(syntax_pct, (int, float)):
        print(f"    Syntax valid (Python): {syntax_pct}%")
    if secrets_found:
        by_type = Counter(s["type"] for s in secrets_found)
        print("    [!] SECRETS: " + ", ".join(f"{t} x{c}"
                                               for t, c in by_type.items())
              + " (MASKED in the detail JSON)")
    if contam_files:
        print(f"    [!] EVAL CONTAMINATION: HumanEval/MBPP signatures in "
              f"{len(contam_files)} files")
    if reasons and not secrets_found and not contam_files:
        print(f"    Notes: {'; '.join(reasons[:3])}")


# ---------------------------------------------------------------------------
# Aggregation + CSV
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "Project/Group name", "Provider", "Established year", "Years active",
    "Last activity", "# of contributors", "Primary coding language",
    "Language breakdown", "Total LoC (est.)", "# of Repos", "# of MRs/PRs",
    "# of Merged", "Avg LoC per MR", "% Simple fixes",
    "% Standard feature work", "% Rich tasks", "Other %", "Automated %",
    "# of Commits",
    "CI/CD configured", "CI systems", "CI config files", "CI jobs (approx)",
    "# of Pipelines/Runs", "Pipeline success rate %",
    "Avg pipeline duration (min)", "Unit test coverage %",
    "Source files", "Test files", "Genuine test files",
    "Suspicious test files", "% Genuine tests", "Test cases (approx)",
    "Assertions",
    "AI/LLM detected", "Primary LLM", "LLM usage %", "LLM usage basis",
    "LLM breakdown", "AI commits", "Commits scanned (LLM)",
    "AI MRs/PRs", "AI tool configs",
    "License", "License risk", "Syntax valid % (py)", "Avg line length",
    "Long lines %", "Comment ratio %", "Docstring % (py)",
    "Avg function length", "Duplicate files %", "Secrets found",
    "PII emails", "Eval contamination files", "Quality score",
    "Training suitability",
    "Code availability",
]


def aggregate(results, name):
    agg = {"name": name, "repos": len(results)}
    agg["provider"] = "/".join(sorted({r.get("provider", "") for r in results}))
    for f in ("total_loc_estimate", "commits", "total_mrs", "merged_mrs",
              "contributors", "source_files", "test_files", "test_cases",
              "assertions", "genuine_test_files", "suspicious_test_files",
              "ci_config_files", "ci_jobs_approx", "ci_pipelines_total",
              "llm_commits_scanned", "llm_ai_commits", "llm_ai_mrs",
              "llm_code_files", "llm_files_scanned",
              "quality_files_scanned", "duplicate_files", "secrets_found",
              "pii_emails", "eval_contamination_files"):
        vals = [r[f] for r in results if isinstance(r.get(f), (int, float))]
        agg[f] = sum(vals) if vals else ""

    # ---- AI/LLM aggregate ----
    llm_score = Counter()
    for r in results:
        for llm_name, v in (r.get("llm_combined_score") or {}).items():
            llm_score[llm_name] += v
    agg["llm_detected"] = "Yes" if llm_score else "No"
    agg["primary_llm"] = llm_score.most_common(1)[0][0] if llm_score else ""
    tot_score = sum(llm_score.values()) or 1
    agg["llm_breakdown"] = "; ".join(
        f"{n} {round(100*v/tot_score)}%" for n, v in llm_score.most_common(5))
    scanned = agg.get("llm_commits_scanned")
    ai_c = agg.get("llm_ai_commits")
    if isinstance(scanned, int) and scanned and isinstance(ai_c, int):
        agg["llm_usage_pct"] = _pct(ai_c, scanned)
        agg["llm_usage_basis"] = f"{ai_c}/{scanned} commits"
    else:
        agg["llm_usage_pct"] = ""
        agg["llm_usage_basis"] = ""
    if (agg["llm_usage_pct"] in ("", 0, 0.0)) and llm_score:
        # nothing found in commits but something found elsewhere
        total_mrs_scanned = sum(r.get("mr_sample_size", 0) or 0
                                for r in results)
        ai_m = agg.get("llm_ai_mrs")
        ai_f = agg.get("llm_code_files")
        f_scanned = agg.get("llm_files_scanned")
        if isinstance(ai_m, int) and ai_m and total_mrs_scanned:
            agg["llm_usage_pct"] = round(100 * ai_m / total_mrs_scanned, 1)
            agg["llm_usage_basis"] = f"{ai_m}/{total_mrs_scanned} MRs/PRs"
        elif (isinstance(ai_f, int) and ai_f
              and isinstance(f_scanned, int) and f_scanned):
            agg["llm_usage_pct"] = round(100 * ai_f / f_scanned, 1)
            agg["llm_usage_basis"] = (f"{ai_f}/{f_scanned} source files "
                                      f"(code comments)")
        elif agg["llm_usage_pct"] in ("",):
            agg["llm_usage_basis"] = "tool configs only"
    elif agg["llm_usage_pct"] == "" and not llm_score:
        # Nothing detected — show 0% of what was scanned (not blank)
        f_scanned = agg.get("llm_files_scanned")
        if isinstance(f_scanned, int) and f_scanned:
            agg["llm_usage_pct"] = 0.0
            agg["llm_usage_basis"] = f"0/{f_scanned} source files"
    ai_tools = set()
    for r in results:
        ai_tools.update(r.get("ai_tool_configs") or [])
    agg["ai_tool_configs"] = "; ".join(sorted(ai_tools))

    checked = [(r.get("genuine_test_files", 0) or 0)
               + (r.get("suspicious_test_files", 0) or 0) for r in results]
    total_checked = sum(checked)
    agg["pct_genuine_tests"] = (
        round(100 * (agg["genuine_test_files"] or 0) / total_checked, 1)
        if total_checked else "")

    # CI/CD aggregate
    agg["ci_configured"] = ("Yes" if any(r.get("ci_configured") == "Yes"
                                         for r in results) else "No")
    systems = set()
    for r in results:
        systems.update(r.get("ci_systems") or [])
    agg["ci_systems"] = "; ".join(sorted(systems))
    rates = [r["ci_success_rate"] for r in results
             if isinstance(r.get("ci_success_rate"), (int, float))]
    agg["ci_success_rate"] = round(sum(rates) / len(rates), 1) if rates else ""
    durs = [r["ci_avg_duration_min"] for r in results
            if isinstance(r.get("ci_avg_duration_min"), (int, float))]
    agg["ci_avg_duration_min"] = round(sum(durs) / len(durs), 1) if durs else ""

    # ---- Training-quality aggregate ----
    licenses = sorted({r.get("license", "") for r in results
                       if r.get("license")})
    agg["license"] = "; ".join(licenses)
    risk_order = ["permissive", "weak-copyleft", "unknown", "copyleft",
                  "no-license (all rights reserved)"]
    risks = [r.get("license_risk", "") for r in results if r.get("license_risk")]
    agg["license_risk"] = (max(risks, key=lambda x: risk_order.index(x)
                               if x in risk_order else 2) if risks else "")
    def _wavg(key):
        pairs = [(r[key], r.get("quality_files_scanned", 0) or 0)
                 for r in results if isinstance(r.get(key), (int, float))]
        tw = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / tw, 1) if tw else ""
    agg["syntax_valid_pct"] = _wavg("syntax_valid_pct")
    agg["avg_line_length"] = _wavg("avg_line_length")
    agg["pct_long_lines"] = _wavg("pct_long_lines")
    agg["comment_ratio_pct"] = _wavg("comment_ratio_pct")
    agg["docstring_pct"] = _wavg("docstring_pct")
    agg["avg_func_length"] = _wavg("avg_func_length")
    agg["quality_score"] = _wavg("quality_score")
    dupf, dups = agg.get("duplicate_files"), agg.get("quality_files_scanned")
    agg["duplicate_pct"] = (round(100 * dupf / dups, 1)
                            if isinstance(dupf, int) and isinstance(dups, int)
                            and dups else "")
    grades = [r.get("training_suitability", "") for r in results
              if r.get("training_suitability")]
    agg["training_suitability"] = (max(grades) if grades else "")  # worst (A<B<C<D)

    lang_total = Counter()
    for r in results:
        for lang, v in (r.get("languages") or {}).items():
            lang_total[lang] += v
    agg["primary_language"] = lang_total.most_common(1)[0][0] if lang_total else ""
    tot = sum(lang_total.values()) or 1
    agg["lang_breakdown"] = "; ".join(
        f"{l} {round(100*v/tot)}%" for l, v in lang_total.most_common(5))

    total_sample = sum(r.get("mr_sample_size", 0) or 0 for r in results)
    if total_sample:
        for key in ("pct_simple", "pct_standard", "pct_rich",
                    "pct_automated", "pct_other"):
            w = sum((r.get(key) or 0) * (r.get("mr_sample_size") or 0)
                    for r in results if isinstance(r.get(key), (int, float)))
            agg[key] = round(w / total_sample, 1)
        locs = [r["avg_loc_per_mr"] for r in results
                if isinstance(r.get("avg_loc_per_mr"), (int, float))]
        agg["avg_loc_per_mr"] = round(sum(locs) / len(locs), 1) if locs else ""
    else:
        for key in ("pct_simple", "pct_standard", "pct_rich", "pct_automated",
                    "pct_other", "avg_loc_per_mr"):
            agg[key] = ""

    dates = [r["created_at"] for r in results if r.get("created_at")]
    lasts = [r["last_activity"] for r in results if r.get("last_activity")]
    agg["established"] = min(dates)[:4] if dates else ""
    agg["last_activity"] = max(lasts) if lasts else ""
    try:
        agg["years_active"] = (datetime.now().year - int(agg["established"])
                               if agg["established"] else "")
    except ValueError:
        agg["years_active"] = ""

    covs = [r["coverage_pct"] for r in results
            if isinstance(r.get("coverage_pct"), (int, float))]
    agg["coverage"] = round(sum(covs) / len(covs), 1) if covs else ""

    vis = {r.get("visibility", "") for r in results}
    prov = results[0].get("provider", "") if results else ""
    label = {"gitlab": "GitLab", "github": "GitHub", "local": "Local"}.get(
        prov, prov)
    if vis <= {"local"}:
        agg["availability"] = "Local/offline repo"
    elif vis <= {"public"}:
        agg["availability"] = f"Public ({label})"
    elif "private" in vis:
        agg["availability"] = "Private (access needed)"
    else:
        agg["availability"] = "/".join(sorted(v for v in vis if v)) or ""
    return agg


def to_row(a):
    return {
        "Project/Group name": a["name"],
        "Provider": a.get("provider", ""),
        "Established year": a["established"],
        "Years active": a["years_active"],
        "Last activity": a["last_activity"],
        "# of contributors": a["contributors"],
        "Primary coding language": a["primary_language"],
        "Language breakdown": a["lang_breakdown"],
        "Total LoC (est.)": a["total_loc_estimate"],
        "# of Repos": a["repos"],
        "# of MRs/PRs": a["total_mrs"],
        "# of Merged": a["merged_mrs"],
        "Avg LoC per MR": a["avg_loc_per_mr"],
        "% Simple fixes": a["pct_simple"],
        "% Standard feature work": a["pct_standard"],
        "% Rich tasks": a["pct_rich"],
        "Other %": a["pct_other"],
        "Automated %": a["pct_automated"],
        "# of Commits": a["commits"],
        "CI/CD configured": a["ci_configured"],
        "CI systems": a["ci_systems"],
        "CI config files": a["ci_config_files"],
        "CI jobs (approx)": a["ci_jobs_approx"],
        "# of Pipelines/Runs": a["ci_pipelines_total"],
        "Pipeline success rate %": a["ci_success_rate"],
        "Avg pipeline duration (min)": a["ci_avg_duration_min"],
        "Unit test coverage %": a["coverage"],
        "Source files": a["source_files"],
        "Test files": a["test_files"],
        "Genuine test files": a["genuine_test_files"],
        "Suspicious test files": a["suspicious_test_files"],
        "% Genuine tests": a["pct_genuine_tests"],
        "Test cases (approx)": a["test_cases"],
        "Assertions": a["assertions"],
        "AI/LLM detected": a.get("llm_detected", ""),
        "Primary LLM": a.get("primary_llm", ""),
        "LLM usage %": a.get("llm_usage_pct", ""),
        "LLM usage basis": a.get("llm_usage_basis", ""),
        "LLM breakdown": a.get("llm_breakdown", ""),
        "AI commits": a.get("llm_ai_commits", ""),
        "Commits scanned (LLM)": a.get("llm_commits_scanned", ""),
        "AI MRs/PRs": a.get("llm_ai_mrs", ""),
        "AI tool configs": a.get("ai_tool_configs", ""),
        "License": a.get("license", ""),
        "License risk": a.get("license_risk", ""),
        "Syntax valid % (py)": a.get("syntax_valid_pct", ""),
        "Avg line length": a.get("avg_line_length", ""),
        "Long lines %": a.get("pct_long_lines", ""),
        "Comment ratio %": a.get("comment_ratio_pct", ""),
        "Docstring % (py)": a.get("docstring_pct", ""),
        "Avg function length": a.get("avg_func_length", ""),
        "Duplicate files %": a.get("duplicate_pct", ""),
        "Secrets found": a.get("secrets_found", ""),
        "PII emails": a.get("pii_emails", ""),
        "Eval contamination files": a.get("eval_contamination_files", ""),
        "Quality score": a.get("quality_score", ""),
        "Training suitability": a.get("training_suitability", ""),
        "Code availability": a["availability"],
    }


# ---------------------------------------------------------------------------
# Safe file writing (so data isn't lost on Windows if the file is open in Excel)
# ---------------------------------------------------------------------------

def safe_open_for_write(path, encoding="utf-8"):
    # create the parent directory if the user gave a path that doesn't exist yet
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        pass
    try:
        return open(path, "w", newline="", encoding=encoding), path
    except OSError:
        base, ext = os.path.splitext(path)
        alt = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        print(f"  [warn] could not write '{path}' (maybe it's open in Excel) —")
        print(f"         saving to '{alt}' instead.")
        return open(alt, "w", newline="", encoding=encoding), alt


# ---------------------------------------------------------------------------
# Provider selection / auto-detect
# ---------------------------------------------------------------------------

def pick_provider(args):
    if args.provider != "auto":
        return args.provider
    if args.path:
        return "local"
    # if all given projects are existing directories, treat as local
    if args.project and all(os.path.isdir(p) for p in args.project):
        return "local"
    if args.org:
        return "github"
    if args.group:
        return "gitlab"
    tok = args.token or ""
    if tok.startswith(("ghp_", "gho_", "ghu_", "github_pat_")):
        return "github"
    if tok.startswith("glpat-"):
        return "gitlab"
    if args.github_url != "https://api.github.com":
        return "github"
    return "gitlab"   # backward compatible default


def main():
    # Never crash on characters the console can't encode (e.g. Windows with
    # output redirected to a cp1252 file) — degrade to '?' instead.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    ap = argparse.ArgumentParser(
        description="Universal codebase analyzer — GitLab, GitHub, or local repo")
    ap.add_argument("--provider", choices=["auto", "gitlab", "github", "local"],
                    default="auto",
                    help="Which source to use (default: auto-detect)")
    ap.add_argument("--project", action="append", default=[],
                    help="GitLab: group/repo or numeric ID | "
                         "GitHub: owner/repo | Local: repo folder path. "
                         "Can be given multiple times.")
    ap.add_argument("--path", action="append", default=[],
                    help="Path to a local repo (offline mode). "
                         "Can be given multiple times.")
    ap.add_argument("--group", help="GitLab group — all of its projects "
                                    "(in local mode: a folder containing repos)")
    ap.add_argument("--org", help="GitHub organization or user — all repos")
    ap.add_argument("--token",
                    default=os.environ.get("GITLAB_TOKEN")
                    or os.environ.get("GITHUB_TOKEN"),
                    help="Access token. Or set the GITLAB_TOKEN / "
                         "GITHUB_TOKEN env var. (Not needed in local mode)")
    ap.add_argument("--gitlab-url", default="https://gitlab.com",
                    help="Self-hosted GitLab URL (default: https://gitlab.com)")
    ap.add_argument("--github-url", default="https://api.github.com",
                    help="GitHub Enterprise API URL "
                         "(default: https://api.github.com)")
    ap.add_argument("--name", default="", help="Company/project name to use in the report")
    ap.add_argument("--max-mrs", type=int, default=0,
                    help="MR/PR fetch limit (0 = all, default)")
    ap.add_argument("--sample-mrs", type=int, default=0,
                    help="Deep-analysis limit (0 = all fetched, default)")
    ap.add_argument("--max-test-files", type=int, default=0,
                    help="How many test files to count test cases in "
                         "(default 0 = auto: ALL locally, 200 via API)")
    ap.add_argument("--max-commit-scan", type=int, default=0,
                    help="How many recent commits to scan for LLM signatures "
                         "(default 0 = ALL commits; on very large repos with "
                         "API providers set a limit to save time, "
                         "e.g. --max-commit-scan 5000)")
    ap.add_argument("--max-quality-scan", type=int, default=0,
                    help="How many source files to run the quality/secrets/"
                         "dedup scan on (0 = auto: local 1000, API 50)")
    ap.add_argument("--max-ai-file-scan", type=int, default=0,
                    help="How many source files to search for AI-attribution "
                         "comments in (0 = auto: local 300, API 30; "
                         "-1 = skip)")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel requests (default 8)")
    ap.add_argument("--local-mrs", choices=["auto", "strict", "merges", "off"],
                    default="auto",
                    help="MR/PR detection for local repos: auto = prefer "
                         "explicit #N/!N refs (default) | strict = only "
                         "explicit refs | merges = also all merge commits | "
                         "off = MR fields blank")
    ap.add_argument("--output", default="repo_report.csv")
    args = ap.parse_args()

    if not (args.project or args.group or args.org or args.path):
        ap.error("one of --project / --path / --group / --org is required")

    provider_name = pick_provider(args)
    print(f"Provider: {provider_name}")

    if provider_name == "gitlab":
        prov = GitLabProvider(args.gitlab_url, args.token,
                              workers=args.workers)
    elif provider_name == "github":
        prov = GitHubProvider(args.github_url, args.token,
                              workers=args.workers)
    else:
        prov = LocalProvider(workers=args.workers, mr_mode=args.local_mrs)

    projects = list(args.project) + list(args.path)
    group = args.group or (args.org if provider_name == "github" else None)
    if group:
        print(f"Listing projects of '{group}'...")
        gp = prov.list_group_projects(group)
        projects += gp
        print(f"  found {len(gp)} projects")

    results = []
    detail = os.path.splitext(args.output)[0] + "_detail.json"
    for p in projects:
        try:
            r = analyze_project(prov, p, args.max_mrs, args.sample_mrs,
                                args.max_test_files, workers=args.workers,
                                max_commit_scan=args.max_commit_scan,
                                max_ai_file_scan=args.max_ai_file_scan,
                                max_quality_scan=args.max_quality_scan)
            if r:
                results.append(r)
                # Save detail after every project — crash-safe
                try:
                    fh, detail = safe_open_for_write(detail)
                    with fh:
                        json.dump(results, fh, indent=2, default=str)
                except Exception as e:
                    print(f"  [warn] detail JSON save failed: {e}")
        except SystemExit:
            raise
        except KeyboardInterrupt:
            print("\n[interrupted] Stopping — writing the report for the "
                  f"{len(results)} project(s) finished so far...")
            break
        except Exception as e:
            print(f"  [error] {p}: {e}")

    if not results:
        print("No results found.")
        sys.exit(1)

    name = args.name or args.group or args.org or results[0]["repo"]
    agg = aggregate(results, name)

    # Console summary FIRST
    print("\n" + "=" * 62)
    print(f"SUMMARY — {name}")
    print("=" * 62)
    for k, v in to_row(agg).items():
        print(f"  {k:28s}: {v}")

    csv_path = args.output
    try:
        # utf-8-sig (BOM) so Excel renders non-ASCII correctly on Windows
        fh, csv_path = safe_open_for_write(args.output, encoding="utf-8-sig")
        with fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            w.writerow(to_row(agg))
            if len(results) > 1:
                for r in results:
                    w.writerow(to_row(aggregate([r], r["repo"])))
    except Exception as e:
        print(f"\n[error] could not save the CSV: {e}")
        print(f"The data is safe in the '{detail}' JSON — a CSV can be built from it.")
        csv_path = "(not saved)"

    print(f"\nCSV report : {csv_path}")
    print(f"Detail JSON: {detail}")


if __name__ == "__main__":
    main()
