"""SQLite-backed run state: durable record of every phase, for resume and traceability.

One state.db per run directory. Every phase transition (org-level or per-repo)
is written through to disk *before* the phase starts and again when it ends,
so a crash mid-phase leaves an accurate "running" row behind rather than
silence. Resume reads this DB to decide what to skip (status="ok") and what
to redo (anything else).

Thread-safety: sqlite3 connections are not safe for concurrent writers, and
the pipeline's per-repo phases run from a ThreadPoolExecutor. A single
connection is opened with check_same_thread=False and every write goes
through one process-wide lock; reads (status queries, `status`/`trace`
subcommands) don't need the lock since SQLite WAL mode allows concurrent
readers even while a writer holds the file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# Phase status values, shared by org_phases and repo_phases.
PENDING = "pending"
RUNNING = "running"
OK = "ok"
FAILED = "failed"
INTERRUPTED = "interrupted"
SKIPPED = "skipped"

# Terminal statuses that resume treats as "done, do not redo".
DONE_STATUSES = (OK, SKIPPED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    run_dir TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,
    platform TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS org_phases (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    phase TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    log_path TEXT,
    UNIQUE(run_id, phase)
);

CREATE TABLE IF NOT EXISTS repo_phases (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    repo TEXT NOT NULL,
    platform TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    log_path TEXT,
    clone_path TEXT,
    UNIQUE(run_id, repo, phase)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    generation INTEGER NOT NULL,
    scope TEXT NOT NULL,
    repo TEXT,
    phase TEXT NOT NULL,
    event TEXT NOT NULL,
    attempt INTEGER,
    duration_ms INTEGER,
    error_class TEXT,
    error_tail TEXT,
    log_path TEXT
);

CREATE TABLE IF NOT EXISTS llm_batches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    phase TEXT NOT NULL,
    repo TEXT,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    batch_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    request_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    input_file TEXT,
    output_file TEXT,
    error_file TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, phase, repo, chunk_index)
);

CREATE TABLE IF NOT EXISTS llm_requests (
    id INTEGER PRIMARY KEY,
    batch_row_id INTEGER NOT NULL REFERENCES llm_batches(id),
    custom_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_class TEXT,
    error_detail TEXT,
    UNIQUE(batch_row_id, custom_id)
);

CREATE INDEX IF NOT EXISTS idx_repo_phases_run ON repo_phases(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_requests_batch ON llm_requests(batch_row_id);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@dataclass
class PhaseRecord:
    phase: str
    status: str
    attempts: int
    error: str | None
    log_path: str | None
    repo: str | None = None


class StateStore:
    """One instance per run; wraps a single sqlite3 connection.

    Safe to share across ThreadPoolExecutor workers within one process --
    every write path acquires `self._lock`. Not safe to share the same
    state.db file across processes for writing (not needed: one pipeline
    process owns a run at a time).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # -- run lifecycle ----------------------------------------------------

    def init_run(self, run_dir: Path, target: str, platform: str, config: dict[str, Any]) -> int:
        """Create (or reuse, for a re-entrant same-process call) the run row."""
        now = _now()
        with self._write() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO runs (run_dir, target, platform, config_json, "
                "status, generation, created_at, updated_at) VALUES (?, ?, ?, ?, 'running', 1, ?, ?)",
                (str(run_dir), target, platform, json.dumps(config), now, now),
            )
            cur.execute("SELECT id FROM runs WHERE run_dir = ?", (str(run_dir),))
            row = cur.fetchone()
        return int(row["id"])

    def load_run(self, run_dir: Path) -> sqlite3.Row | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM runs WHERE run_dir = ?", (str(run_dir),))
        return cur.fetchone()

    def resume_run(self, run_id: int) -> int:
        """Bump generation, flip any stuck 'running' rows to 'interrupted', return new generation."""
        now = _now()
        with self._write() as cur:
            cur.execute(
                "UPDATE org_phases SET status = ? WHERE run_id = ? AND status = ?",
                (INTERRUPTED, run_id, RUNNING),
            )
            cur.execute(
                "UPDATE repo_phases SET status = ? WHERE run_id = ? AND status = ?",
                (INTERRUPTED, run_id, RUNNING),
            )
            cur.execute(
                "UPDATE runs SET generation = generation + 1, status = 'running', "
                "updated_at = ?, finished_at = NULL WHERE id = ?",
                (now, run_id),
            )
            cur.execute("SELECT generation FROM runs WHERE id = ?", (run_id,))
            row = cur.fetchone()
        return int(row["generation"])

    def finish_run(self, run_id: int, status: str) -> None:
        now = _now()
        with self._write() as cur:
            cur.execute(
                "UPDATE runs SET status = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (status, now, now, run_id),
            )

    def get_generation(self, run_id: int) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT generation FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        return int(row["generation"]) if row else 1

    # -- org-level phases ---------------------------------------------------

    def org_phase_status(self, run_id: int, phase: str) -> str | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT status FROM org_phases WHERE run_id = ? AND phase = ?", (run_id, phase)
        )
        row = cur.fetchone()
        return row["status"] if row else None

    def start_org_phase(self, run_id: int, phase: str, generation: int) -> int:
        now = _now()
        with self._write() as cur:
            cur.execute(
                "INSERT INTO org_phases (run_id, phase, status, attempts, started_at) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(run_id, phase) DO UPDATE SET "
                "status = ?, attempts = attempts + 1, started_at = ?, finished_at = NULL, error = NULL",
                (run_id, phase, RUNNING, now, RUNNING, now),
            )
            cur.execute(
                "SELECT attempts FROM org_phases WHERE run_id = ? AND phase = ?", (run_id, phase)
            )
            row = cur.fetchone()
        attempt = int(row["attempts"])
        self.record_event(run_id, generation, "org", None, phase, "started", attempt=attempt)
        return attempt

    def finish_org_phase(
        self,
        run_id: int,
        phase: str,
        generation: int,
        ok: bool,
        error: str = "",
        log_path: str = "",
        duration_ms: int = 0,
        attempt: int = 0,
    ) -> None:
        now = _now()
        status = OK if ok else FAILED
        with self._write() as cur:
            cur.execute(
                "UPDATE org_phases SET status = ?, finished_at = ?, error = ?, log_path = ? "
                "WHERE run_id = ? AND phase = ?",
                (status, now, error[-2000:] if error else None, log_path, run_id, phase),
            )
        self.record_event(
            run_id,
            generation,
            "org",
            None,
            phase,
            "ok" if ok else "failed",
            attempt=attempt,
            duration_ms=duration_ms,
            error_tail=error[-500:] if error else None,
            log_path=log_path,
        )

    def skip_org_phase(self, run_id: int, phase: str, generation: int) -> None:
        now = _now()
        with self._write() as cur:
            cur.execute(
                "INSERT INTO org_phases (run_id, phase, status, attempts, started_at, finished_at) "
                "VALUES (?, ?, 'ok', 0, ?, ?) "
                "ON CONFLICT(run_id, phase) DO NOTHING",
                (run_id, phase, now, now),
            )
        self.record_event(run_id, generation, "org", None, phase, "skipped-resume")

    # -- per-repo phases ------------------------------------------------------

    def repo_phase_status(self, run_id: int, repo: str, phase: str) -> str | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT status FROM repo_phases WHERE run_id = ? AND repo = ? AND phase = ?",
            (run_id, repo, phase),
        )
        row = cur.fetchone()
        return row["status"] if row else None

    def repo_phases_for(self, run_id: int, repo: str) -> list[PhaseRecord]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT phase, status, attempts, error, log_path FROM repo_phases "
            "WHERE run_id = ? AND repo = ? ORDER BY id",
            (run_id, repo),
        )
        return [
            PhaseRecord(r["phase"], r["status"], r["attempts"], r["error"], r["log_path"], repo)
            for r in cur.fetchall()
        ]

    def start_repo_phase(
        self, run_id: int, repo: str, platform: str, phase: str, generation: int
    ) -> int:
        now = _now()
        with self._write() as cur:
            cur.execute(
                "INSERT INTO repo_phases (run_id, repo, platform, phase, status, attempts, started_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(run_id, repo, phase) DO UPDATE SET "
                "status = ?, attempts = attempts + 1, started_at = ?, finished_at = NULL, error = NULL",
                (run_id, repo, platform, phase, RUNNING, now, RUNNING, now),
            )
            cur.execute(
                "SELECT attempts FROM repo_phases WHERE run_id = ? AND repo = ? AND phase = ?",
                (run_id, repo, phase),
            )
            row = cur.fetchone()
        attempt = int(row["attempts"])
        self.record_event(run_id, generation, "repo", repo, phase, "started", attempt=attempt)
        return attempt

    def finish_repo_phase(
        self,
        run_id: int,
        repo: str,
        phase: str,
        generation: int,
        ok: bool,
        error: str = "",
        log_path: str = "",
        clone_path: str = "",
        duration_ms: int = 0,
        attempt: int = 0,
    ) -> None:
        now = _now()
        status = OK if ok else FAILED
        with self._write() as cur:
            cur.execute(
                "UPDATE repo_phases SET status = ?, finished_at = ?, error = ?, log_path = ?, "
                "clone_path = COALESCE(?, clone_path) WHERE run_id = ? AND repo = ? AND phase = ?",
                (
                    status,
                    now,
                    error[-2000:] if error else None,
                    log_path,
                    clone_path or None,
                    run_id,
                    repo,
                    phase,
                ),
            )
        self.record_event(
            run_id,
            generation,
            "repo",
            repo,
            phase,
            "ok" if ok else "failed",
            attempt=attempt,
            duration_ms=duration_ms,
            error_tail=error[-500:] if error else None,
            log_path=log_path,
        )

    def repo_clone_path(self, run_id: int, repo: str) -> str | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT clone_path FROM repo_phases WHERE run_id = ? AND repo = ? "
            "AND clone_path IS NOT NULL LIMIT 1",
            (run_id, repo),
        )
        row = cur.fetchone()
        return row["clone_path"] if row else None

    # -- retry / invalidation ---------------------------------------------

    def reset_phase(
        self, run_id: int, phase: str, repo: str | None = None, force: bool = False
    ) -> int:
        """Flip matching row(s) back to pending so the next run redoes them.
        repo=None means the org-level phase of that name.

        force=False (default, plain `retry`) only touches rows that are not
        already 'ok' -- retrying must never discard completed work. Pass
        force=True for `retry --force` (redo even if ok) and for invalidating
        a downstream phase whose upstream just got redone from scratch (that
        must apply regardless of the downstream phase's current status).
        """
        status_filter = "" if force else " AND status != 'ok'"
        with self._write() as cur:
            if repo is None:
                cur.execute(
                    "UPDATE org_phases SET status = 'pending' WHERE run_id = ? "
                    f"AND phase = ?{status_filter}",
                    (run_id, phase),
                )
            else:
                cur.execute(
                    "UPDATE repo_phases SET status = 'pending' WHERE run_id = ? AND repo = ? "
                    f"AND phase = ?{status_filter}",
                    (run_id, repo, phase),
                )
            return cur.rowcount

    def reset_repo_all_failed(self, run_id: int) -> int:
        with self._write() as cur:
            cur.execute(
                "UPDATE repo_phases SET status = 'pending' WHERE run_id = ? "
                "AND status IN ('failed', 'interrupted', 'running')",
                (run_id,),
            )
            n1 = cur.rowcount
            cur.execute(
                "UPDATE org_phases SET status = 'pending' WHERE run_id = ? "
                "AND status IN ('failed', 'interrupted', 'running')",
                (run_id,),
            )
            n2 = cur.rowcount
        return n1 + n2

    def reset_phase_all_repos(self, run_id: int, phase: str, force: bool = False) -> int:
        status_filter = "" if force else " AND status != 'ok'"
        with self._write() as cur:
            cur.execute(
                "UPDATE repo_phases SET status = 'pending' WHERE run_id = ? AND phase = ?"
                f"{status_filter}",
                (run_id, phase),
            )
            return cur.rowcount

    # -- events / tracing ---------------------------------------------------

    def record_event(
        self,
        run_id: int,
        generation: int,
        scope: str,
        repo: str | None,
        phase: str,
        event: str,
        attempt: int | None = None,
        duration_ms: int | None = None,
        error_class: str | None = None,
        error_tail: str | None = None,
        log_path: str | None = None,
    ) -> None:
        with self._write() as cur:
            cur.execute(
                "INSERT INTO events (run_id, ts, generation, scope, repo, phase, event, "
                "attempt, duration_ms, error_class, error_tail, log_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    _now(),
                    generation,
                    scope,
                    repo,
                    phase,
                    event,
                    attempt,
                    duration_ms,
                    error_class,
                    error_tail[-500:] if error_tail else None,
                    log_path,
                ),
            )

    def trace(self, run_id: int, repo: str | None = None) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        if repo:
            cur.execute(
                "SELECT * FROM events WHERE run_id = ? AND repo = ? ORDER BY id", (run_id, repo)
            )
        else:
            cur.execute("SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,))
        return cur.fetchall()

    def failures(self, run_id: int) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 'org' AS scope, NULL AS repo, phase, status, attempts, error, log_path "
            "FROM org_phases WHERE run_id = ? AND status IN ('failed', 'interrupted') "
            "UNION ALL "
            "SELECT 'repo' AS scope, repo, phase, status, attempts, error, log_path "
            "FROM repo_phases WHERE run_id = ? AND status IN ('failed', 'interrupted')",
            (run_id, run_id),
        )
        return cur.fetchall()

    def status_summary(self, run_id: int) -> dict[str, Any]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT phase, status, attempts, error FROM org_phases WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        org = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT repo, phase, status, attempts FROM repo_phases WHERE run_id = ? ORDER BY repo, id",
            (run_id,),
        )
        repo_rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT DISTINCT repo FROM repo_phases WHERE run_id = ?",
            (run_id,),
        )
        repos = [r["repo"] for r in cur.fetchall()]
        counts = {"ok": 0, "failed": 0, "partial": 0, "pending": 0, "running": 0}
        for repo in repos:
            statuses = [r["status"] for r in repo_rows if r["repo"] == repo]
            if all(s == OK for s in statuses):
                counts["ok"] += 1
            elif any(s in (FAILED, INTERRUPTED) for s in statuses):
                counts["failed"] += 1
            elif any(s == RUNNING for s in statuses):
                counts["running"] += 1
            elif any(s == OK for s in statuses):
                counts["partial"] += 1
            else:
                counts["pending"] += 1
        return {"org_phases": org, "repo_count": len(repos), "repo_counts": counts, "repo_phases": repo_rows}

    # -- llm batch tracking ---------------------------------------------------

    def upsert_batch(
        self,
        run_id: int,
        phase: str,
        repo: str | None,
        chunk_index: int,
        batch_id: str | None = None,
        status: str | None = None,
        request_count: int | None = None,
        completed_count: int | None = None,
        failed_count: int | None = None,
        input_file: str | None = None,
        output_file: str | None = None,
        error_file: str | None = None,
    ) -> int:
        now = _now()
        with self._write() as cur:
            cur.execute(
                "INSERT INTO llm_batches (run_id, phase, repo, chunk_index, batch_id, status, "
                "request_count, completed_count, failed_count, input_file, output_file, error_file, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, COALESCE(?, 'pending'), "
                "COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0), ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, phase, repo, chunk_index) DO UPDATE SET "
                "batch_id = COALESCE(?, batch_id), "
                "status = COALESCE(?, status), "
                "request_count = COALESCE(?, request_count), "
                "completed_count = COALESCE(?, completed_count), "
                "failed_count = COALESCE(?, failed_count), "
                "input_file = COALESCE(?, input_file), "
                "output_file = COALESCE(?, output_file), "
                "error_file = COALESCE(?, error_file), "
                "updated_at = ?",
                (
                    run_id, phase, repo or "", chunk_index, batch_id, status,
                    request_count, completed_count, failed_count, input_file, output_file, error_file,
                    now, now,
                    batch_id, status, request_count, completed_count, failed_count,
                    input_file, output_file, error_file, now,
                ),
            )
            cur.execute(
                "SELECT id FROM llm_batches WHERE run_id = ? AND phase = ? AND repo = ? AND chunk_index = ?",
                (run_id, phase, repo or "", chunk_index),
            )
            row = cur.fetchone()
        return int(row["id"])

    def get_batch(self, run_id: int, phase: str, repo: str | None, chunk_index: int) -> sqlite3.Row | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM llm_batches WHERE run_id = ? AND phase = ? AND repo = ? AND chunk_index = ?",
            (run_id, phase, repo or "", chunk_index),
        )
        return cur.fetchone()

    def batches_for_phase(self, run_id: int, phase: str, repo: str | None = None) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        if repo is None:
            cur.execute(
                "SELECT * FROM llm_batches WHERE run_id = ? AND phase = ? ORDER BY chunk_index",
                (run_id, phase),
            )
        else:
            cur.execute(
                "SELECT * FROM llm_batches WHERE run_id = ? AND phase = ? AND repo = ? ORDER BY chunk_index",
                (run_id, phase, repo),
            )
        return cur.fetchall()
