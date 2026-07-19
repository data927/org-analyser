#!/usr/bin/env python3
"""
unwrap.py — read sealed repo-quality-score bundles and print human-readable summaries.

Usage:
    python unwrap.py <sealed.json|dir|results-root> [--csv PATH] [--json PATH]

Examples:
    python unwrap.py results/your-org/repos/
    python unwrap.py results/your-org/org.sealed.json
    python unwrap.py results/ --csv summary.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# Allow importing sealing from the skill's scripts/ dir
SKILL_SCRIPTS = Path(__file__).resolve().parent / "scripts"
if SKILL_SCRIPTS.exists():
    sys.path.insert(0, str(SKILL_SCRIPTS))

try:
    import sealing
except ImportError:
    sealing = None


def load_bundle(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    if sealing and sealing.is_sealed(data):
        return data
    if isinstance(data, dict) and "result" in data:
        return data
    raise ValueError(f"not a sealed bundle: {path}")


def unwrap_result(bundle: dict) -> dict:
    if sealing:
        return sealing.unwrap_result(bundle)
    return bundle.get("result", bundle)


def is_org_bundle(bundle: dict) -> bool:
    r = unwrap_result(bundle)
    return "org_score" in r or "repos" in r


def collect_sealed_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    files = sorted(path.rglob("*.sealed.json"))
    if not files:
        files = sorted(path.glob("*.json"))
    return files


def repo_row(bundle: dict, source: str) -> dict:
    r = unwrap_result(bundle)
    if "overall_score" not in r:
        return {}
    cls = r.get("classes") or []
    primary = cls[0]["name"] if cls else ""
    return {
        "source_file": source,
        "repo_name": r.get("repo_name", ""),
        "overall_score": r.get("overall_score"),
        "grade": r.get("overall_grade") or r.get("grade", ""),
        "total_loc": r.get("total_loc"),
        "primary_class": primary,
        "is_monorepo": r.get("is_monorepo", False),
        "capacity": r.get("capacity"),
        "mining_rank": r.get("mining_rank"),
        "head_sha": _head_sha(bundle),
        "integrity_digest": (bundle.get("integrity") or {}).get("digest", ""),
    }


def _head_sha(bundle: dict) -> str:
    prov = bundle.get("provenance") or []
    if prov:
        return prov[0].get("head_sha") or ""
    for obj in (bundle.get("evidence") or {}).values():
        if isinstance(obj, dict) and "repo_stats" in obj:
            return (obj.get("repo_stats") or {}).get("head_sha") or ""
    return ""


def org_row(bundle: dict, source: str) -> dict:
    r = unwrap_result(bundle)
    if "org_score" not in r:
        return {}
    return {
        "source_file": source,
        "org_name": r.get("org_name", ""),
        "org_score": r.get("org_score"),
        "org_grade": r.get("org_grade", ""),
        "repo_count": r.get("repo_count"),
        "total_loc": r.get("total_loc"),
        "score_min": (r.get("distribution") or {}).get("min"),
        "score_max": (r.get("distribution") or {}).get("max"),
        "score_median": (r.get("distribution") or {}).get("median"),
        "integrity_digest": (bundle.get("integrity") or {}).get("digest", ""),
    }


def print_repo_table(rows: list[dict]) -> None:
    if not rows:
        print("No repo results found.")
        return
    rows = sorted(rows, key=lambda x: (x.get("overall_score") or 0), reverse=True)
    print(f"\n{'Repo':<30} {'Score':>6} {'Grade':<5} {'LOC':>8} {'Class':<15} {'Mining':>8}")
    print("-" * 80)
    for r in rows:
        mining = r.get("mining_rank")
        mining_s = f"{mining:.1f}" if mining is not None else "-"
        loc = r.get("total_loc") or 0
        print(
            f"{r['repo_name']:<30} {r['overall_score']:>6.1f} {r['grade']:<5} "
            f"{loc:>8} {r['primary_class']:<15} {mining_s:>8}"
        )


def print_org_summary(row: dict) -> None:
    if not row:
        return
    print(f"\nOrg: {row.get('org_name')}  Score: {row.get('org_score')} ({row.get('org_grade')})")
    print(f"  Repos: {row.get('repo_count')}  LOC: {row.get('total_loc')}")
    print(f"  Distribution min/median/max: {row.get('score_min')} / {row.get('score_median')} / {row.get('score_max')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Unwrap sealed quality-score bundles.")
    parser.add_argument("path", help="Sealed file, directory, or results root")
    parser.add_argument("--csv", help="Write repo summary CSV to this path")
    parser.add_argument("--json", help="Write flat JSON array to this path")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    files = collect_sealed_files(root)

    repo_rows: list[dict] = []
    org_rows: list[dict] = []

    for f in files:
        try:
            bundle = load_bundle(f)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"skip {f}: {e}", file=sys.stderr)
            continue
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)
        if is_org_bundle(bundle):
            row = org_row(bundle, rel)
            if row:
                org_rows.append(row)
        else:
            row = repo_row(bundle, rel)
            if row:
                repo_rows.append(row)

    print_repo_table(repo_rows)
    for o in org_rows:
        print_org_summary(o)

    if args.csv and repo_rows:
        fields = list(repo_rows[0].keys())
        Path(args.csv).write_text("")
        with Path(args.csv).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(repo_rows)
        print(f"\nWrote CSV: {args.csv}")

    if args.json:
        out = {"repos": repo_rows, "orgs": org_rows}
        Path(args.json).write_text(json.dumps(out, indent=2))
        print(f"Wrote JSON: {args.json}")

    print(f"\nTotal: {len(repo_rows)} repo bundle(s), {len(org_rows)} org bundle(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
