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
from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn


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
    """One spinner row per org-lane (merged-pr-counts, pr-task-profile) plus
    one bar for the repo pool, all on the Console shared with logging (see
    module docstring for why that sharing matters)."""

    def __init__(self, console: Console, total_repos: int) -> None:
        self.console = console
        self.total_repos = total_repos
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}[/bold]"),
            BarColumn(bar_width=30),
            TextColumn("{task.fields[status]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self.repo_task = self.progress.add_task(
            "repos", total=total_repos, status=f"0/{total_repos}"
        )
        self._org_tasks: dict[str, int] = {}

    def __enter__(self) -> "RunProgress":
        self.progress.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.progress.stop()

    def start_org_phase(self, name: str) -> None:
        self._org_tasks[name] = self.progress.add_task(name, total=None, status="running")

    def finish_org_phase(self, name: str, ok: bool) -> None:
        task_id = self._org_tasks.get(name)
        if task_id is None:
            return
        self.progress.update(task_id, total=1, completed=1, status="ok" if ok else "failed")

    def advance_repo(self, done: int, ok: int, partial: int, failed: int) -> None:
        self.progress.update(
            self.repo_task,
            completed=done,
            status=f"{done}/{self.total_repos} ok={ok} partial={partial} failed={failed}",
        )
