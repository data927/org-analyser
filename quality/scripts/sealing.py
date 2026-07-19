#!/usr/bin/env python3
"""
sealing.py — shared sealing / integrity primitives for the repo-quality-score skill.

This is a LIBRARY module (no CLI). It is imported by score.py and aggregate_org.py
(which seal their output by default), by seal.py (manual / re-sealing CLI), and by
verify.py (so the canonicalization and digest are byte-for-byte identical on both
sides). Standard library only; reads no env vars and no secrets.

A "sealed bundle" wraps a scored result with everything needed to RE-DERIVE it on
trusted infrastructure:
  - generated_at   : ISO-8601 timestamp (so time-derived signals are reproducible)
  - tool_version   : SHA-256 fingerprint of the skill's scripts
  - provenance     : repo identity + git head_sha + commit counts/dates
  - evidence       : the raw repo_stats / git_stats signals (or nested per-repo
                     bundles, for an org rollup)
  - result         : the scored output
  - integrity      : a SHA-256 over the canonical payload — CHANGE-DETECTION ONLY,
                     not a trust anchor (a self-contained client can recompute it).
Trust comes from re-derivation (verify.py), not from this digest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "repo-quality-score/sealed/1"


def canonical(obj) -> str:
    """Stable serialization for digesting: sorted keys, compact, ASCII."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_version() -> dict:
    """Fingerprint the skill's scripts so a verifier can tell whether the client ran
    unmodified code. Best-effort (a self-contained client could fake it); its real
    value is cross-checked against the verifier's own known-good copy."""
    scripts_dir = Path(__file__).resolve().parent
    files: dict[str, str] = {}
    for p in sorted(scripts_dir.glob("*.py")):
        try:
            files[p.name] = sha256_hex(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return {"algo": "sha256", "scripts_digest": sha256_hex(canonical(files)), "files": files}


def is_sealed(obj) -> bool:
    return isinstance(obj, dict) and obj.get("schema") == SCHEMA


def unwrap_result(obj):
    """Return the scored result whether `obj` is a sealed bundle or a raw result."""
    return obj["result"] if is_sealed(obj) else obj


def _looks_like_git_stats(obj) -> bool:
    return isinstance(obj, dict) and "repo_stats" in obj and "confirmed_candidate_count" in obj


def provenance_from_evidence(evidence: dict) -> list:
    """Pull the facts needed to re-clone and re-run (repo identity + HEAD). Looks one
    level into nested sealed bundles, so an org rollup inherits per-repo provenance."""
    prov = []
    for name, obj in evidence.items():
        if _looks_like_git_stats(obj):
            rs = obj["repo_stats"]
            prov.append({
                "source": name,
                "repo_path": obj.get("repo_path"),
                "head_sha": rs.get("head_sha"),
                "total_commits": rs.get("total_commits"),
                "first_commit": rs.get("first_commit"),
                "last_commit": rs.get("last_commit"),
            })
        elif is_sealed(obj):
            prov.extend(obj.get("provenance", []))
    return prov


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_bundle(result: dict, evidence: dict, generated_at: str | None = None) -> dict:
    """Wrap `result` + `evidence` into a sealed bundle with provenance and integrity."""
    payload = {
        "schema": SCHEMA,
        "generated_at": generated_at or now_iso(),
        "tool_version": tool_version(),
        "provenance": provenance_from_evidence(evidence),
        "evidence": evidence,
        "result": result,
    }
    digest = sha256_hex(canonical(payload))
    payload["integrity"] = {
        "algo": "sha256",
        "canonical": "json sort_keys compact ascii, over payload excluding this block",
        "digest": digest,
        "note": "change-detection only; trust comes from re-derivation (see verify.py)",
    }
    return payload


def recompute_digest(bundle: dict) -> str:
    """Digest of a bundle's payload excluding its own integrity block."""
    payload = {k: v for k, v in bundle.items() if k != "integrity"}
    return sha256_hex(canonical(payload))


def load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
