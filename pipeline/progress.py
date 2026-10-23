"""Interactive terminal UI for a run in progress.

Only active when stdout is a real TTY and --quiet wasn't passed: CI and any
redirected/piped output always get PipelineLogger's plain log lines, never
ANSI escape codes (see should_use_rich).

When active, this owns the terminal. Rich's Live rendering (what the
progress bars use under the hood) only coexists cleanly with other output
that goes through the *same* Console instance -- a second writer hitting
stdout directly (PipelineLogger's plain StreamHandler) would tear the
display apart mid-repaint. rich_console_handler swaps that handler for a
RichHandler bound to the same Console for the duration of the run, then
restores it; the file handler (full detail) is never touched either way.
"""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

# One row per link in cli.py's REPO_PHASE_CHAIN. Kept here (rather than
# imported from cli.py) to avoid a cli.py <-> pipeline.progress import
# cycle; cli.py's process_repo is the source of truth for what actually
# runs and must keep calling update_phase with exactly these names.
REPO_PHASES = ("clone", "redact", "codebase-profiler", "repo-analyzer", "eval-kit", "repo-quality-score")


def should_use_rich(quiet: bool) -> bool:
    return sys.stdout.isatty() and not quiet


@contextmanager
def rich_console_handler(logger: logging.Logger, console: Console) -> Iterator[None]:
    """Swap `logger`'s plain console StreamHandler for a RichHandler on
    `console` for the duration of the block, then restore it."""
    plain_handlers = [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    for h in plain_handlers:
        logger.removeHandler(h)

    rich_handler = RichHandler(
        console=console, show_time=True, show_path=False, markup=False, rich_tracebacks=False
    )
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(rich_handler)
    try:
        yield
    finally:
        logger.removeHandler(rich_handler)
        for h in plain_handlers:
            logger.addHandler(h)


class RunProgress:
    """Spinner rows for the org-lanes (merged-pr-counts, pr-task-profile),
    plus one bar per repo-phase (clone/redact/codebase-profiler/...) and an
    overall repo bar, all sharing the Console logging is bound to (see
    module docstring for why that sharing matters).

    Rich's `Progress.add_task`/`.update` take their own internal lock, so
    calling them from many repo-pool worker threads at once is fine; the
    plain dict this class uses to derive running/done/failed *counts* is
    not thread-safe on its own, hence `_lock`.
    """

    def __init__(self, console: Console, total_repos: int, phase_names: tuple[str, ...] = REPO_PHASES) -> None:
        self.console = console
        self.total_repos = total_repos
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}[/bold]"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[status]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self.repo_task = self.progress.add_task(
            "repos", total=total_repos, status="ok=0 partial=0 failed=0"
        )
        self._org_tasks: dict[str, int] = {}

        self._lock = threading.Lock()
        self._phase_counts: dict[str, dict[str, int]] = {
            name: {"running": 0, "done": 0, "failed": 0} for name in phase_names
        }
        self._phase_running: dict[str, set[str]] = {name: set() for name in phase_names}
        self._phase_tasks: dict[str, int] = {
            name: self.progress.add_task(name, total=total_repos, status="pending")
            for name in phase_names
        }

    def __enter__(self) -> "RunProgress":
        self.progress.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.progress.stop()

    def queue_org_phase(self, name: str, note: str = "queued") -> None:
        """Pre-create an org-lane row so a phase that deliberately runs late
        (pr-task-profile runs last) is visible from the start instead of
        appearing out of nowhere near the end. start=False keeps the elapsed
        clock at -:--:-- until start_org_phase actually starts the work."""
        self._org_tasks[name] = self.progress.add_task(
            name, total=None, start=False, status=note
        )

    def start_org_phase(self, name: str) -> None:
        task_id = self._org_tasks.get(name)
        if task_id is None:
            self._org_tasks[name] = self.progress.add_task(name, total=None, status="running")
            return
        self.progress.start_task(task_id)
        self.progress.update(task_id, status="running")

    def note_org_phase(self, name: str, status: str) -> None:
        """Update a running org-lane row's status text without finishing it --
        e.g. live Batch API stage read from llm.batch's state sidecars."""
        task_id = self._org_tasks.get(name)
        if task_id is None:
            return
        self.progress.update(task_id, status=status)

    def finish_org_phase(self, name: str, ok: bool, status: str | None = None) -> None:
        task_id = self._org_tasks.get(name)
        if task_id is None:
            return
        # start_task is a no-op on an already-started task; needed so a
        # queued-but-skipped row still gets a frozen (zero) elapsed time.
        self.progress.start_task(task_id)
        self.progress.update(
            task_id, total=1, completed=1, status=status or ("ok" if ok else "failed")
        )

    def advance_repo(self, done: int, ok: int, partial: int, failed: int) -> None:
        self.progress.update(
            self.repo_task,
            completed=done,
            status=f"ok={ok} partial={partial} failed={failed}",
        )

    def update_phase(self, phase: str, event: str, repo: str | None = None) -> None:
        """event: one of "start" (phase kicked off for a repo), "ok"/"failed"
        (phase finished for a repo), "skip" (resume: already ok, never ran
        this generation -- counts straight as done, no running increment).

        `repo` (full name) keeps a running-set per phase so watchers can ask
        which repos a phase is currently executing for (phase_running)."""
        counts = self._phase_counts.get(phase)
        task_id = self._phase_tasks.get(phase)
        if counts is None or task_id is None:
            return
        with self._lock:
            running_set = self._phase_running[phase]
            if event == "start":
                counts["running"] += 1
                if repo:
                    running_set.add(repo)
            elif event == "ok":
                counts["running"] = max(0, counts["running"] - 1)
                counts["done"] += 1
                running_set.discard(repo or "")
            elif event == "failed":
                counts["running"] = max(0, counts["running"] - 1)
                counts["failed"] += 1
                running_set.discard(repo or "")
            elif event == "skip":
                counts["done"] += 1
            running, done, failed = counts["running"], counts["done"], counts["failed"]
        status = "pending" if not (running or done or failed) else f"running={running} failed={failed}"
        self.progress.update(task_id, completed=done + failed, status=status)

    def phase_running(self, phase: str) -> set[str]:
        """Snapshot of repo full-names this phase is currently running for."""
        with self._lock:
            return set(self._phase_running.get(phase, ()))

    def note_phase_progress(self, phase: str, running_fraction: float, detail: str) -> None:
        """Fractional in-flight progress for a phase's bar, derived by a
        watcher from the streamed subprocess logs (e.g. eval-kit: PRs scanned
        vs the repo's merged-PR total). Bar position = finished repos +
        in-flight fractions, so a 1-repo run no longer sits at 0% for the
        whole phase. No-op once nothing is running -- the exact count from
        update_phase always wins in the end."""
        counts = self._phase_counts.get(phase)
        task_id = self._phase_tasks.get(phase)
        if counts is None or task_id is None:
            return
        with self._lock:
            running, done, failed = counts["running"], counts["done"], counts["failed"]
        if not running:
            return
        self.progress.update(
            task_id,
            completed=done + failed + min(running_fraction, running * 0.99),
            status=f"running={running} failed={failed} {detail}",
        )
