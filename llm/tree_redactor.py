"""Working-tree secret redaction: run once per repo clone, right after clone,
before any analysis phase reads the tree.

Complements the LLM-boundary redaction in credential_redactor.py (which
scrubs text at the moment it is sent to a model) -- this pass scrubs the
checkout itself, so every phase that reads files directly (profiler,
repo-analyzer, eval-kit) sees an already-clean tree instead of each phase
reimplementing its own scrubbing.

Does not touch git history (.git/ objects): rewriting history is slow and
destructive, and nothing downstream reads objects directly. .git/ is also
never part of the zipped run bundle (see clone isolation in cli.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm.credential_redactor import redact_secrets

# Private-key material: dropped whole (content truncated) rather than
# pattern-scrubbed. Some of these are binary (pfx/p12/jks) where regex
# redaction can't work reliably; for the text-based ones (pem/key) truncating
# is simpler and stronger than trusting a pattern to catch every key format.
KEY_FILE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk"}
KEY_FILENAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}

# VCS internals and dependency/build trees: nothing here is original source,
# and .git/ especially must never be pattern-scanned (huge, and irrelevant --
# it never leaves the machine, see module docstring).
SKIP_DIR_NAMES = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv", "dist", "build"}

# Past this size, treat the file as non-text/generated and leave it alone --
# reading+decoding multi-MB files on every repo adds up across a big org run.
MAX_FILE_BYTES = 5 * 1024 * 1024


def _is_key_file(path: Path) -> bool:
    return path.suffix.lower() in KEY_FILE_SUFFIXES or path.name in KEY_FILENAMES


def redact_working_tree(root: Path) -> dict[str, Any]:
    """Redact secrets from every text file under `root`, in place.

    Returns a report: files_scanned, files_modified, files_dropped (key
    material truncated), secrets_by_type (counts only, values never
    recorded), errors.
    """
    root = Path(root)
    report: dict[str, Any] = {
        "files_scanned": 0,
        "files_modified": 0,
        "files_dropped": 0,
        "secrets_by_type": {},
        "errors": [],
    }

    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel_parts = path.relative_to(root).parts[:-1]
        if any(part in SKIP_DIR_NAMES for part in rel_parts):
            continue

        if _is_key_file(path):
            try:
                if path.stat().st_size > 0:
                    path.write_bytes(b"")
                    report["files_dropped"] += 1
            except OSError as exc:
                report["errors"].append(f"{path}: truncate failed: {exc}")
            continue

        try:
            size = path.stat().st_size
        except OSError as exc:
            report["errors"].append(f"{path}: stat failed: {exc}")
            continue
        if size == 0 or size > MAX_FILE_BYTES:
            continue

        try:
            raw = path.read_bytes()
        except OSError as exc:
            report["errors"].append(f"{path}: read failed: {exc}")
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary file -- nothing pattern-based can safely do here

        report["files_scanned"] += 1
        redacted, redactions = redact_secrets(text)
        if not redactions:
            continue
        try:
            path.write_text(redacted, encoding="utf-8")
        except OSError as exc:
            report["errors"].append(f"{path}: write failed: {exc}")
            continue
        report["files_modified"] += 1
        for name, count in redactions:
            report["secrets_by_type"][name] = report["secrets_by_type"].get(name, 0) + count

    return report


def write_redaction_report(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
