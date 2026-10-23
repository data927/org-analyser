"""Regression tests for restored + hardened pr-task-profile.

Run from the repository root with:
    python -m analysis.test_pr_task_profile
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import analysis.pr_task_profile as ptp
import cli
import llm.batch as batch


def test_rate_limit_sleep_is_capped():
    far_future = str(int(time.time()) + 99_999)

    class Resp:
        headers = {"X-RateLimit-Reset": far_future}

    assert ptp._wait_for_rate_limit(Resp(), 1) == ptp.MAX_RATE_LIMIT_SLEEP_SECONDS
    assert ptp.MAX_RATE_LIMIT_SLEEP_SECONDS <= 120


def test_graphql_default_retries_are_bounded():
    assert ptp.DEFAULT_GRAPHQL_RETRIES <= 6


def test_clear_fetch_checkpoint_removes_sidecars():
    with TemporaryDirectory() as tmp:
        checkpoint_dir = Path(tmp)
        slug = ptp._fetch_checkpoint_slug("github", "acme/api")
        jsonl = checkpoint_dir / f"{slug}_prs.jsonl"
        state = checkpoint_dir / f"{slug}_fetch_state.json"
        jsonl.write_text("{}\n", encoding="utf-8")
        state.write_text("{}", encoding="utf-8")
        ptp.clear_fetch_checkpoint(checkpoint_dir, "github", "acme/api")
        assert not jsonl.exists()
        assert not state.exists()


def test_find_prior_repo_report_picks_newest_scan():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        older = root / "scan_1" / "repos"
        newer = root / "scan_2" / "repos"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        (older / "acme_api.json").write_text("{}", encoding="utf-8")
        time.sleep(0.05)
        (newer / "acme_api.json").write_text('{"summary":{}}', encoding="utf-8")
        found = ptp.find_prior_repo_report(root, "acme_api")
        assert found is not None
        assert found.parent == newer


def test_process_prs_batch_path_refuses_sync_stampede():
    """Large N + Batch timeout must NOT fall back to thousands of sync calls."""
    prs = [
        {
            "number": i,
            "title": f"PR {i}",
            "bodyText": "fix a bug",
            "url": f"https://example/{i}",
            "author": {"login": "human", "isBot": False, "__typename": "User"},
            "files": {"nodes": [{"path": "a.py"}]},
            "comments": {"nodes": []},
            "reviews": {"nodes": []},
            "reviewThreads": {"nodes": []},
            "closingIssuesReferences": {"nodes": []},
            "commits": {"nodes": []},
            "additions": 1,
            "deletions": 1,
            "changedFiles": 1,
            "createdAt": "2024-01-01T00:00:00Z",
            "mergedAt": "2024-01-02T00:00:00Z",
        }
        for i in range(5)
    ]

    original_run_batch = batch.run_batch
    sync_calls: list[str] = []

    def boom(*_a, **_k):
        raise TimeoutError("batch poll timed out")

    client = SimpleNamespace()

    # Skip LLM preflight (would need a real client).
    with mock.patch.object(ptp, "_llm_preflight", return_value=None):
        with mock.patch.object(ptp, "safe_openai", return_value=client):
            with TemporaryDirectory() as tmp:
                try:
                    batch.run_batch = boom  # type: ignore[assignment]
                    try:
                        ptp.process_prs(
                            prs,
                            model="test-model",
                            max_workers=2,
                            repo="acme/api",
                            client=client,
                            batch_work_dir=Path(tmp),
                            llm_mode="auto",
                            llm_batch_threshold=3,
                            batch_poll_timeout=1.0,
                        )
                        raise AssertionError("expected TimeoutError from Batch path")
                    except TimeoutError:
                        pass
                finally:
                    batch.run_batch = original_run_batch

    assert sync_calls == []


def test_process_prs_small_n_uses_sync():
    pr = {
        "number": 1,
        "title": "tiny",
        "bodyText": "typo",
        "url": "https://example/1",
        "author": {"login": "human", "isBot": False, "__typename": "User"},
        "files": {"nodes": [{"path": "README.md"}]},
        "comments": {"nodes": []},
        "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "closingIssuesReferences": {"nodes": []},
        "commits": {"nodes": []},
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "createdAt": "2024-01-01T00:00:00Z",
        "mergedAt": "2024-01-02T00:00:00Z",
    }

    sync_seen: list[str] = []

    def fake_sync(client, model, item, max_retries=3):
        sync_seen.append(item.custom_id)
        return batch.BatchItemResult(
            item.custom_id,
            True,
            json.dumps(
                {
                    "category": "simple_fix",
                    "confidence": "high",
                    "reason": "tiny change",
                }
            ),
            None,
            item.metadata,
        )

    with mock.patch.object(ptp, "_llm_preflight", return_value=None):
        with mock.patch.object(ptp, "_sync_classify_item", side_effect=fake_sync):
            with mock.patch.object(batch, "run_batch", side_effect=AssertionError("no batch")):
                with TemporaryDirectory() as tmp:
                    rows = ptp.process_prs(
                        [pr],
                        model="test-model",
                        max_workers=1,
                        repo="acme/api",
                        client=object(),
                        batch_work_dir=Path(tmp),
                        llm_mode="auto",
                        llm_batch_threshold=50,
                    )

    assert sync_seen == ["0"]
    assert rows[0]["llm_category"] == "simple_fix"


def test_cli_parent_timeout_is_five_hours_not_twenty_four():
    """run_pr_task_profile must pass the hardened 5h parent ceiling."""
    captured: dict[str, object] = {}

    def fake_run_module(module, args, **kwargs):
        captured["module"] = module
        captured["timeout"] = kwargs.get("timeout")
        captured["args"] = args
        return 0, "ok", ""

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ctx = SimpleNamespace(
            task_profile_dir=tmp_path / "pr-task-profile",
            logs_dir=tmp_path / "logs",
            workers=4,
            gitlab_host="gitlab.com",
            platform="github",
            target="acme",
            github_repos=[],
            gitlab_projects=[],
            tokens={"github-data-token": "ghs_test"},
            github_token_name="github-data-token",
        )
        ctx.logs_dir.mkdir()
        log = SimpleNamespace(
            info=lambda *_a, **_k: None,
            error=lambda *_a, **_k: None,
            phase_log=lambda *_a, **_k: None,
        )
        original = cli.run_module
        try:
            cli.run_module = fake_run_module  # type: ignore[assignment]
            result = cli.run_pr_task_profile(ctx, log, [])
        finally:
            cli.run_module = original

    assert captured["module"] == "analysis.pr_task_profile"
    assert captured["timeout"] == 5 * 3600
    assert result.get("ok") is True


def test_org_analyser_pr_task_profile_subcommand_forwards_argv():
    """`org-analyser pr-task-profile --org X` must hit the standalone module."""
    seen: list[list[str]] = []

    def fake_cli(argv=None):
        seen.append(list(argv or []))
        return 0

    original = cli.run_pr_task_profile_cli
    saved = sys.argv[:]
    try:
        cli.run_pr_task_profile_cli = fake_cli  # type: ignore[assignment]
        sys.argv = ["org-analyser", "pr-task-profile", "--org", "acme", "--max-prs", "10"]
        code = cli.main()
    finally:
        cli.run_pr_task_profile_cli = original
        sys.argv = saved

    assert code == 0
    assert seen == [["--org", "acme", "--max-prs", "10"]]


def test_standalone_help_exposes_harden_flags():
    import io
    from contextlib import redirect_stderr, redirect_stdout

    buf_out, buf_err = io.StringIO(), io.StringIO()
    saved = sys.argv[:]
    try:
        sys.argv = ["pr-task-profile", "--help"]
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            try:
                ptp.main()
            except SystemExit as exc:
                assert exc.code in (0, None)
    finally:
        sys.argv = saved

    text = buf_out.getvalue() + buf_err.getvalue()
    for flag in ("--repo-timeout", "--org-timeout", "--batch-poll-timeout", "--max-prs"):
        assert flag in text, f"missing {flag} in help"


def test_batch_state_summary_reads_sidecars():
    with TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "chunk.state.json").write_text(
            json.dumps(
                {
                    "status": "in_progress",
                    "request_counts": {"completed": 2, "failed": 0, "total": 5},
                }
            ),
            encoding="utf-8",
        )
        summary = cli.batch_state_summary(d)
        assert summary is not None
        assert "in_progress" in summary
        assert "2/5" in summary


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK: pr-task-profile hardenings + dual CLI wiring")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
