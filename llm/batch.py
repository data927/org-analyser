"""OpenAI Batch API broker: shared by every phase that scores many PRs with
the same prompt shape (pr_task_profile, eval-kit's PR-rubrics, the
cybersecurity PR scanner, task taxonomy) so a 7,000-PR org run is one batch
instead of 7,000 live chat.completions.create calls.

Batch processing requires the whole request set up front -- there is no
per-item streaming API, unlike chat.completions.create. Every call site is
expected to finish its own "collect" step (fetch every PR, render every
prompt) before calling into this module; run_batch/run_batch_or_sync take a
fully-materialized list[BatchItem], never a generator, precisely because
half-collected input can't be submitted.

Resumability here is file-based, not tied to the main pipeline's state.db --
pr_task_profile.py and eval-kit run as their own subprocess, separate from
the process that owns the pipeline's SQLite state. The moment a batch is
accepted by OpenAI it is real, billable, in-flight work; the state.json
sidecar records the batch_id first, so a crash after submit resumes by
polling that same batch instead of submitting a duplicate.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from llm.credential_redactor import redact_secrets

logger = logging.getLogger(__name__)

# OpenAI Batch API limits (as of writing): 50,000 requests or 200MB per input
# file, whichever comes first. Staying a bit under the byte limit leaves
# headroom for JSONL formatting overhead this estimate doesn't account for.
MAX_REQUESTS_PER_CHUNK = 50_000
MAX_BYTES_PER_CHUNK = 190 * 1024 * 1024
# Shared routing policy: a handful of requests returns quickly over live chat;
# larger workloads use the OpenAI Batch API.
DEFAULT_BATCH_THRESHOLD = 50
# Shared sync-path fallback pool size, used by every run_batch_or_sync call site.
DEFAULT_MAX_WORKERS = 8

TERMINAL_BATCH_STATUSES = ("completed", "failed", "expired", "cancelled")


def _batch_endpoint() -> str:
    """Batch input `url` / `batches.create` endpoint. Azure OpenAI serves batch
    under the deployment-relative "/chat/completions"; OpenAI under "/v1/...".
    Mirrors llm_safety.safe_openai's Azure switch (AZURE_OPENAI_ENDPOINT set)."""
    if os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip():
        return "/chat/completions"
    return "/v1/chat/completions"


def _batch_model(model: str) -> str:
    """Azure Batch indexes by deployment name, not model name -- swap it in
    here (same mapping safe_openai applies to live calls via kwargs)."""
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
    if deployment and os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip():
        return deployment
    return model


@dataclass
class BatchItem:
    custom_id: str
    messages: list[dict[str, str]]
    model: str
    temperature: float = 0.0
    response_format: dict[str, str] | None = None
    # Caller-only bookkeeping (e.g. repo/PR number) -- never sent to the API,
    # carried through untouched so results can be re-associated with input.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchItemResult:
    custom_id: str
    ok: bool
    content: str | None
    error: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


def _redact_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    redacted = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            content, _found = redact_secrets(content)
        redacted.append({**m, "content": content})
    return redacted


def _to_jsonl_line(item: BatchItem) -> str:
    body: dict[str, Any] = {
        "model": _batch_model(item.model),
        "messages": _redact_messages(item.messages),
        "temperature": item.temperature,
    }
    if item.response_format:
        body["response_format"] = item.response_format
    return json.dumps(
        {"custom_id": item.custom_id, "method": "POST", "url": _batch_endpoint(), "body": body}
    )


def _chunk_items(items: list[BatchItem]) -> list[list[BatchItem]]:
    chunks: list[list[BatchItem]] = []
    current: list[BatchItem] = []
    current_bytes = 0
    for item in items:
        line_bytes = len(_to_jsonl_line(item).encode("utf-8")) + 1
        if current and (
            len(current) >= MAX_REQUESTS_PER_CHUNK
            or current_bytes + line_bytes > MAX_BYTES_PER_CHUNK
        ):
            chunks.append(current)
            current, current_bytes = [], 0
        current.append(item)
        current_bytes += line_bytes
    if current:
        chunks.append(current)
    return chunks


class BatchStateFile:
    """JSON sidecar recording one chunk's batch_id/status, for resuming a
    submit-poll-join cycle across process restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_batch_chunk(
    client: Any,
    items: list[BatchItem],
    state_file: BatchStateFile,
    work_dir: Path,
    chunk_tag: str,
    poll_interval: float = 10.0,
    poll_timeout: float = 24 * 3600,
) -> list[BatchItemResult]:
    """Submit-or-resume a single chunk (<=50k requests) through the Batch API."""
    state = state_file.load()
    batch_id = state.get("batch_id")

    if not batch_id:
        work_dir.mkdir(parents=True, exist_ok=True)
        input_path = work_dir / f"{chunk_tag}.requests.jsonl"
        with input_path.open("w", encoding="utf-8") as fh:
            for item in items:
                fh.write(_to_jsonl_line(item) + "\n")
        with input_path.open("rb") as fh:
            upload = client.files.create(file=fh, purpose="batch")
        batch = client.batches.create(
            input_file_id=upload.id, endpoint=_batch_endpoint(), completion_window="24h"
        )
        batch_id = batch.id
        state = {
            "batch_id": batch_id,
            "status": batch.status,
            "input_file": str(input_path),
            "request_count": len(items),
        }
        state_file.save(state)
        logger.info(
            "Batch %s submitted: %d requests (chunk=%s, window=24h)",
            batch_id, len(items), chunk_tag,
        )
    else:
        logger.info(
            "Resuming batch %s (chunk=%s, last status=%s)",
            batch_id, chunk_tag, state.get("status", "unknown"),
        )

    # Persist and log every observed stage transition (validating ->
    # in_progress -> finalizing -> ...) and completed-count change, not just
    # the terminal state: the state sidecar is the only window another
    # process (the pipeline's progress UI, a human with cat) has into a wait
    # that can legitimately last hours, and it doubles as the resume record.
    last_seen: tuple[Any, ...] | None = None

    def note_progress(b: Any) -> None:
        nonlocal last_seen
        counts = getattr(b, "request_counts", None)
        completed = int(getattr(counts, "completed", 0) or 0)
        failed = int(getattr(counts, "failed", 0) or 0)
        total = int(getattr(counts, "total", 0) or 0)
        seen = (b.status, completed, failed)
        if seen == last_seen:
            return
        last_seen = seen
        state["status"] = b.status
        state["request_counts"] = {"completed": completed, "failed": failed, "total": total}
        state_file.save(state)
        logger.info(
            "Batch %s: %s [%d/%d done, %d failed]",
            batch_id, b.status, completed + failed, total, failed,
        )

    deadline = time.monotonic() + poll_timeout
    batch = client.batches.retrieve(batch_id)
    note_progress(batch)
    while batch.status not in TERMINAL_BATCH_STATUSES:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"batch {batch_id} did not reach a terminal state within "
                f"{poll_timeout}s (status={batch.status})"
            )
        time.sleep(poll_interval)
        batch = client.batches.retrieve(batch_id)
        note_progress(batch)

    state["status"] = batch.status
    state["output_file_id"] = batch.output_file_id
    state["error_file_id"] = batch.error_file_id
    state_file.save(state)

    results: dict[str, BatchItemResult] = {}
    if batch.output_file_id:
        content = client.files.content(batch.output_file_id).text
        for line in content.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cid = row["custom_id"]
            body = (row.get("response") or {}).get("body") or {}
            choices = body.get("choices") or []
            text = choices[0]["message"]["content"] if choices else None
            err = row.get("error")
            results[cid] = BatchItemResult(
                cid, err is None and text is not None, text, json.dumps(err) if err else None
            )
    if batch.error_file_id:
        content = client.files.content(batch.error_file_id).text
        for line in content.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cid = row.get("custom_id")
            if cid:
                results[cid] = BatchItemResult(cid, False, None, json.dumps(row.get("error") or row))

    out: list[BatchItemResult] = []
    for item in items:
        r = results.get(item.custom_id)
        if r is None:
            out.append(
                BatchItemResult(item.custom_id, False, None, "missing from batch output", item.metadata)
            )
        else:
            r.metadata = item.metadata
            out.append(r)
    return out


def run_batch(
    client: Any,
    items: list[BatchItem],
    work_dir: Path,
    tag: str,
    **kwargs: Any,
) -> list[BatchItemResult]:
    """Chunk `items` (already fully collected) and run each chunk through
    run_batch_chunk. A chunk whose state file already has a batch_id resumes
    that batch instead of resubmitting."""
    chunks = _chunk_items(items)
    all_results: list[BatchItemResult] = []
    for idx, chunk in enumerate(chunks):
        chunk_tag = f"{tag}-{idx:03d}"
        state_file = BatchStateFile(work_dir / f"{chunk_tag}.state.json")
        all_results.extend(run_batch_chunk(client, chunk, state_file, work_dir, chunk_tag, **kwargs))
    return all_results


def run_batch_or_sync(
    client: Any,
    items: list[BatchItem],
    work_dir: Path,
    tag: str,
    sync_fn: Callable[[BatchItem], BatchItemResult],
    mode: str = "auto",
    threshold: int = DEFAULT_BATCH_THRESHOLD,
    max_workers: int = DEFAULT_MAX_WORKERS,
    **batch_kwargs: Any,
) -> list[BatchItemResult]:
    """Single entry point call sites should use instead of a raw per-item
    chat.completions.create loop.

    mode="auto" (default) batches once `items` reaches `threshold`; below
    that it runs `sync_fn` over a thread pool -- today's per-request
    behaviour -- since a handful of requests shouldn't wait on a queue that
    can take up to 24h. mode="batch"/"sync" force one path; "auto" also
    degrades to sync if the Batch API call itself fails (e.g. an Azure
    deployment without batch support configured).
    """
    if not items:
        return []

    use_batch = mode == "batch" or (mode == "auto" and len(items) >= threshold)
    if use_batch:
        try:
            return run_batch(client, items, work_dir, tag, **batch_kwargs)
        except Exception as exc:
            if mode == "batch":
                raise
            # auto mode only: fall through to the sync path below.
            logger.warning(
                "Batch API path failed for tag=%s (%s: %s); falling back to "
                "%d sync requests", tag, type(exc).__name__, exc, len(items),
            )

    results: list[BatchItemResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(sync_fn, item): item for item in items}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(BatchItemResult(item.custom_id, False, None, str(exc), item.metadata))
    return results


def _demo() -> None:
    """Self-check: the JSONL request must switch URL/model between OpenAI and
    Azure so batch works on both providers."""
    item = BatchItem(custom_id="c0", messages=[{"role": "user", "content": "hi"}], model="gpt-4o")

    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"):
        os.environ.pop(var, None)
    row = json.loads(_to_jsonl_line(item))
    assert row["url"] == "/v1/chat/completions", row["url"]
    assert row["body"]["model"] == "gpt-4o", row["body"]["model"]

    os.environ["AZURE_OPENAI_ENDPOINT"] = "https://x.openai.azure.com/"
    os.environ["AZURE_OPENAI_DEPLOYMENT"] = "gpt4o-batch"
    row = json.loads(_to_jsonl_line(item))
    assert row["url"] == "/chat/completions", row["url"]
    assert row["body"]["model"] == "gpt4o-batch", row["body"]["model"]

    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"):
        os.environ.pop(var, None)
    print("ok")


if __name__ == "__main__":
    _demo()
