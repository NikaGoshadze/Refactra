import json
from dataclasses import replace

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from refactra import analysis_context as contexts
from refactra.auth_sessions import GitHubIdentity
from refactra.models import Run, RunAnalysisContext, RunCommand, RunEvent
from refactra.repository_revision import RepositoryRevision
from refactra.repository_runs import create_repository_run
from refactra.repository_snapshot import SourceFile, SourceSnapshot

from .conftest import migration_config


def prepare(session_factory, *, base_sha="a" * 40):
    with session_factory.begin() as session:
        run = create_repository_run(
            session,
            identity=GitHubIdentity(123, "fixture"),
            installation_id=77,
            revision=RepositoryRevision(42, "fixture/repo", "main", "main", base_sha),
            task_text="Refactor cart",
        )
    snapshot = SourceSnapshot(
        42,
        base_sha,
        (
            SourceFile("src/cart.py", 'def cart():\n    return "café"\n'.encode()),
            SourceFile(".env", b"PASSWORD=hidden-marker"),
        ),
    )
    return run, snapshot, contexts.prepare_analysis_context(run, snapshot)


def test_save_retry_and_reload_through_new_connection_preserve_the_whole_context(
    session_factory, database
):
    run, _, prepared = prepare(session_factory)
    assert b"hidden-marker" not in prepared.document and "def cart" not in repr(prepared)
    for _ in range(2):
        with session_factory.begin() as session:
            contexts.save_analysis_context(session, prepared)
    database.dispose()
    with session_factory() as session:
        loaded = contexts.load_analysis_context(session, run.id)
        assert loaded == prepared
        payload = json.loads(loaded.document)
        assert payload["content"]["files"][0]["text"] == 'def cart():\n    return "café"\n'
        assert payload["python"]["files"][0]["symbols"][0]["name"] == "cart"
        assert (
            payload["selection"]["commit_sha"] == payload["inventory"]["commit_sha"] == run.base_sha
        )
        assert session.scalar(select(func.count()).select_from(RunAnalysisContext)) == 1
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == "ANALYSIS_CONTEXT_SAVED")
        ).all()
        assert len(events) == 1 and set(events[0].payload) == {"schema_version", "size_bytes"}
        assert session.scalar(select(func.count()).select_from(RunCommand)) == 0
        assert session.get(Run, run.id).status == "CREATED"


def test_caller_rollback_removes_context_and_event(session_factory):
    run, _, prepared = prepare(session_factory)
    with pytest.raises(RuntimeError):
        with session_factory.begin() as session:
            contexts.save_analysis_context(session, prepared)
            raise RuntimeError("abort")
    with session_factory() as session:
        assert contexts.load_analysis_context(session, run.id) is None
        assert (
            session.scalar(
                select(func.count()).where(RunEvent.event_type == "ANALYSIS_CONTEXT_SAVED")
            )
            == 0
        )


def test_conflicting_context_cannot_overwrite_saved_data(session_factory):
    run, _, prepared = prepare(session_factory)
    with session_factory.begin() as session:
        contexts.save_analysis_context(session, prepared)
    payload = json.loads(prepared.document)
    payload["content"]["files"][0]["text"] = "changed"
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="already saved"):
            contexts.save_analysis_context(
                session, replace(prepared, document=json.dumps(payload).encode())
            )
    with session_factory() as session:
        assert contexts.load_analysis_context(session, run.id) == prepared


def test_changed_run_revision_is_rejected_on_build_and_reload(session_factory):
    run, snapshot, prepared = prepare(session_factory)
    with pytest.raises(ValueError, match="persisted repository run"):
        contexts.prepare_analysis_context(run, replace(snapshot, commit_sha="b" * 40))
    with session_factory.begin() as session:
        contexts.save_analysis_context(session, prepared)
        session.get(Run, run.id).base_sha = "b" * 40
    with session_factory() as session:
        with pytest.raises(ValueError, match="persisted repository run"):
            contexts.load_analysis_context(session, run.id)


def test_context_byte_limit_is_checked_before_build_and_save(session_factory, monkeypatch):
    run, snapshot, prepared = prepare(session_factory)
    monkeypatch.setattr(contexts, "MAX_CONTEXT_BYTES", 1)
    with pytest.raises(ValueError, match="storage limit"):
        contexts.prepare_analysis_context(run, snapshot)
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="storage limit"):
            contexts.save_analysis_context(session, prepared)
        assert session.get(RunAnalysisContext, run.id) is None


def test_migration_rollback_keeps_existing_runs_and_format_constraint_is_enforced(
    session_factory, database
):
    run, _, _ = prepare(session_factory)
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add(RunAnalysisContext(run_id=run.id, payload={"schema_version": None}))
    with database.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "0002_auth_sessions")
        assert connection.scalar(text("SELECT to_regclass('run_analysis_contexts')")) is None
        assert connection.scalar(select(Run.id).where(Run.id == run.id)) == run.id
        command.upgrade(config, "head")
