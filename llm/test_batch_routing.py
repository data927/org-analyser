"""Regression checks for the shared Batch-versus-chat routing policy.

Run from the repository root with:
    python -m llm.test_batch_routing
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import cli
import llm.batch as batch


def _items(count: int) -> list[batch.BatchItem]:
    return [
        batch.BatchItem(
            custom_id=str(index),
            messages=[{"role": "user", "content": "classify"}],
            model="test-model",
        )
        for index in range(count)
    ]


def test_small_request_sets_use_live_chat():
    seen_sync: list[str] = []
    original_run_batch = batch.run_batch

    def unexpected_batch(*args, **kwargs):
        raise AssertionError("small request set should not use the Batch API")

    try:
        batch.run_batch = unexpected_batch
        results = batch.run_batch_or_sync(
            object(),
            _items(2),
            Path("unused"),
            "small",
            sync_fn=lambda item: (
                seen_sync.append(item.custom_id)
                or batch.BatchItemResult(item.custom_id, True, "{}", None, item.metadata)
            ),
            threshold=3,
            max_workers=1,
        )
    finally:
        batch.run_batch = original_run_batch

    assert seen_sync == ["0", "1"]
    assert [result.custom_id for result in results] == ["0", "1"]


def test_large_request_sets_use_openai_batch():
    seen_batches: list[list[str]] = []
    seen_sync: list[str] = []
    original_run_batch = batch.run_batch

    def fake_run_batch(client, items, work_dir, tag, **kwargs):
        seen_batches.append([item.custom_id for item in items])
        return [
            batch.BatchItemResult(item.custom_id, True, "{}", None, item.metadata)
            for item in items
        ]

    try:
        batch.run_batch = fake_run_batch
        results = batch.run_batch_or_sync(
            object(),
            _items(3),
            Path("unused"),
            "large",
            sync_fn=lambda item: (
                seen_sync.append(item.custom_id)
                or batch.BatchItemResult(item.custom_id, True, "{}", None, item.metadata)
            ),
            threshold=3,
            max_workers=1,
        )
    finally:
        batch.run_batch = original_run_batch

    assert seen_batches == [["0", "1", "2"]]
    assert seen_sync == []
    assert [result.custom_id for result in results] == ["0", "1", "2"]


def test_default_threshold_value_is_50():
    # Pins the literal, not just the routing behavior: every call site
    # listed in llm/batch.py's DEFAULT_BATCH_THRESHOLD comment now shares
    # this constant, so an accidental edit here silently changes routing
    # everywhere at once. The behavioral tests below read the constant
    # dynamically and would not catch that.
    assert batch.DEFAULT_BATCH_THRESHOLD == 50


def test_default_threshold_uses_batch_for_large_workloads():
    items = _items(batch.DEFAULT_BATCH_THRESHOLD)
    seen_sync: list[str] = []
    original_run_batch = batch.run_batch

    def fake_run_batch(client, batch_items, work_dir, tag, **kwargs):
        return [
            batch.BatchItemResult(item.custom_id, True, "{}", None, item.metadata)
            for item in batch_items
        ]

    try:
        batch.run_batch = fake_run_batch
        results = batch.run_batch_or_sync(
            object(),
            items,
            Path("unused"),
            "default-threshold",
            sync_fn=lambda item: (
                seen_sync.append(item.custom_id)
                or batch.BatchItemResult(item.custom_id, True, "{}", None, item.metadata)
            ),
            max_workers=1,
        )
    finally:
        batch.run_batch = original_run_batch

    assert seen_sync == []
    assert len(results) == batch.DEFAULT_BATCH_THRESHOLD


def test_pipeline_eval_kit_keeps_automatic_llm_routing():
    captured: dict[str, object] = {}
    original_run_module = cli.run_module

    def fake_run_module(module, args, **kwargs):
        captured["module"] = module
        captured["args"] = args
        captured["timeout"] = kwargs["timeout"]
        return 0, "", ""

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        entry = cli.RepoEntry(
            platform="local",
            full_name="local/example",
            org="local",
            batch_org="local",
            local_path=tmp_path,
        )
        ctx = SimpleNamespace(
            eval_kit_dir=tmp_path / "eval-kit",
            skip_f2p=True,
            local_only=False,
            pr_rubrics_provider="openai",
            repos_manifest={},
            repo_log_dir=lambda _entry: tmp_path / "logs",
        )
        try:
            cli.run_module = fake_run_module
            ok, _ = cli.run_eval_kit(entry, tmp_path, ctx)
        finally:
            cli.run_module = original_run_module

    assert ok is True
    args = captured["args"]
    assert args[args.index("--taxonomy-llm-mode") + 1] == "auto"
    assert args[args.index("--rubrics-llm-mode") + 1] == "auto"
    assert captured["timeout"] >= 3 * 24 * 60 * 60


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK: bulk LLM work uses Batch and small work uses live chat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())