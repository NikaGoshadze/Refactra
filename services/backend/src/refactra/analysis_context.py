"""Internal structured input checkpoint. Callers authorize access before fetching source."""

import json
from dataclasses import asdict, dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from refactra.models import Run, RunAnalysisContext, RunEvent
from refactra.python_symbols import inspect_python_symbols
from refactra.repository_content import retrieve_selected_content
from refactra.repository_inventory import inventory_snapshot
from refactra.repository_selection import select_task_files
from refactra.repository_snapshot import SourceSnapshot

FORMAT = "analysis-context-v1"
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
COMPACT_SOURCE_CHARS = 2400


@dataclass(frozen=True)
class PreparedAnalysisContext:
    run_id: UUID
    document: bytes = field(repr=False)


def _encode(payload: dict) -> bytes:
    document = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )
    if len(document) > MAX_CONTEXT_BYTES:
        raise ValueError("Analysis context exceeds the 2 MiB storage limit.")
    return document


def _require_run(run: Run | None, run_id: UUID, payload: dict) -> None:
    if (
        run is None
        or not isinstance(run_id, UUID)
        or not isinstance(payload, dict)
        or run.workflow_version != "repository-v0.1"
        or payload.get("schema_version") != FORMAT
        or payload.get("run_id") != str(run_id)
        or payload.get("repository_id") != run.github_repository_id
        or payload.get("base_sha") != run.base_sha
        or payload.get("task_text") != run.task_text
    ):
        raise ValueError("Analysis context must match the persisted repository run.")


def prepare_analysis_context(run: Run, snapshot: SourceSnapshot) -> PreparedAnalysisContext:
    """Compute outside the write transaction; source stays in bounded, policy-checked data."""
    identity = {
        "schema_version": FORMAT,
        "run_id": str(run.id),
        "repository_id": snapshot.repository_id,
        "base_sha": snapshot.commit_sha,
        "task_text": run.task_text,
    }
    _require_run(run, run.id, identity)
    inventory = inventory_snapshot(snapshot)
    selection = select_task_files(snapshot, inventory, run.task_text)
    content = retrieve_selected_content(snapshot, selection)
    payload = identity | {
        "inventory": asdict(inventory),
        "selection": asdict(selection),
        "content": asdict(content),
        "python": asdict(inspect_python_symbols(content)),
    }
    return PreparedAnalysisContext(run.id, _encode(payload))


def save_analysis_context(session: Session, context: PreparedAnalysisContext) -> None:
    """Caller commits/rolls back context and event together; identical retries add no event."""
    if len(context.document) > MAX_CONTEXT_BYTES:
        raise ValueError("Analysis context exceeds the 2 MiB storage limit.")
    payload = json.loads(context.document)
    run = session.scalar(select(Run).where(Run.id == context.run_id).with_for_update())
    _require_run(run, context.run_id, payload)
    existing = session.get(RunAnalysisContext, context.run_id)
    if existing is not None:
        if existing.payload != payload:
            raise ValueError("A different analysis context is already saved for this run.")
        return
    session.add(RunAnalysisContext(run_id=context.run_id, payload=payload))
    session.add(
        RunEvent(
            run_id=context.run_id,
            event_type="ANALYSIS_CONTEXT_SAVED",
            payload={"schema_version": FORMAT, "size_bytes": len(context.document)},
        )
    )
    session.flush()


def load_analysis_context(session: Session, run_id: UUID) -> PreparedAnalysisContext | None:
    """Resume from saved data without fetching GitHub or recomputing inspection."""
    stored = session.get(RunAnalysisContext, run_id)
    if stored is None:
        return None
    _require_run(session.get(Run, run_id), run_id, stored.payload)
    return PreparedAnalysisContext(run_id, _encode(stored.payload))


def compact_read_only_context(context: PreparedAnalysisContext) -> PreparedAnalysisContext:
    """Create a deterministic fallback without dropping requests or pinned file identities."""
    payload = json.loads(context.document)
    for source in payload["content"]["files"]:
        original = source["text"]
        if len(original) <= COMPACT_SOURCE_CHARS:
            continue
        prefix = original[:COMPACT_SOURCE_CHARS]
        last_break = prefix.rfind("\n")
        if last_break > 0:
            prefix = prefix[: last_break + 1]
        source.update(
            text=prefix,
            end_line=source["start_line"] + len(prefix.splitlines()) - 1,
            size_bytes=len(prefix.encode("utf-8")),
            excerpt=True,
        )
    payload["content"]["total_bytes"] = sum(f["size_bytes"] for f in payload["content"]["files"])
    if len(payload.get("saved_analysis", "")) > COMPACT_SOURCE_CHARS:
        payload["saved_analysis"] = payload["saved_analysis"][:COMPACT_SOURCE_CHARS] + "\n[Excerpt]"
    return PreparedAnalysisContext(context.run_id, _encode(payload))
