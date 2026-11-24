import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import refactra.snapshot_preparation as preparation
from refactra.analysis_context import load_analysis_context, save_analysis_context
from refactra.github_app import GitHubAppError
from refactra.models import Run, RunCommand, RunEvent
from refactra.repository_analysis_worker import WORKFLOW
from refactra.run_access import RunAccessDenied
from refactra.snapshot_preparation import (
    PREPARE_SNAPSHOT,
    SNAPSHOT_RETRY,
    SNAPSHOT_RETRY_ATTEMPTS,
    enqueue_snapshot_preparation,
    process_snapshot_preparation,
)
from refactra.worker import claim_command

from .test_analysis_context import prepare


def allowed(_run):
    return SimpleNamespace(transport=None), object()


@pytest.fixture
def pending(session_factory):
    run, snapshot, context = prepare(session_factory)
    with session_factory.begin() as session:
        command_id = enqueue_snapshot_preparation(session, run.id)
        assert enqueue_snapshot_preparation(session, run.id) == command_id
    return run, snapshot, context, command_id


def process(session_factory, authorize=allowed):
    command = claim_command(session_factory, "worker", workflow=WORKFLOW, kind=PREPARE_SNAPSHOT)
    assert command is not None
    return process_snapshot_preparation(session_factory, command, "worker", authorize=authorize)


def commands(session):
    return sorted(session.execute(select(RunCommand.kind, RunCommand.status)).all())


def stop_code(session):
    event = session.scalar(select(RunEvent).where(RunEvent.event_type == "RUN_STOPPED"))
    return event.payload["code"] if event else None


def test_worker_saves_the_pinned_context_and_queues_analysis_once(
    session_factory, pending, monkeypatch
):
    run, snapshot, _, _ = pending
    fetched = []

    def fetch(persisted, _token, *, transport):
        fetched.append(persisted.base_sha)
        return snapshot

    monkeypatch.setattr(preparation, "fetch_source_snapshot", fetch)
    assert process(session_factory)
    assert fetched == [run.base_sha]
    assert claim_command(session_factory, "w", workflow=WORKFLOW, kind=PREPARE_SNAPSHOT) is None
    with session_factory() as session:
        saved = json.loads(load_analysis_context(session, run.id).document)
        assert saved["base_sha"] == run.base_sha
        assert commands(session) == [(PREPARE_SNAPSHOT, "DONE"), ("START", "PENDING")]
        assert session.get(Run, run.id).status == "CREATED"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (RunAccessDenied("Removed from the allowlist."), "analysis_access_revoked"),
        (HTTPException(403, "Write access was removed."), "analysis_access_revoked"),
        (KeyError("REFACTRA_GITHUB_INSTALLATION_ID"), "analysis_access_unconfigured"),
    ],
)
def test_lost_access_stops_before_any_download(
    session_factory, pending, monkeypatch, failure, code
):
    run, _, _, _ = pending

    def deny(_run):
        raise failure

    monkeypatch.setattr(
        preparation, "fetch_source_snapshot", lambda *_, **__: pytest.fail("Downloaded source")
    )
    assert process(session_factory, deny)
    with session_factory() as session:
        assert session.get(Run, run.id).status == "FAILED"
        assert commands(session) == [(PREPARE_SNAPSHOT, "FAILED")]
        assert load_analysis_context(session, run.id) is None
        assert stop_code(session) == code


@pytest.mark.parametrize("recovers", [True, False])
def test_failed_downloads_retry_then_recover_or_stop(
    session_factory, pending, monkeypatch, recovers
):
    run, snapshot, _, _ = pending
    attempts = []

    def fetch(_run, _token, *, transport):
        attempts.append(1)
        if recovers and len(attempts) == 2:
            return snapshot
        raise GitHubAppError("Repository source snapshot could not be retrieved safely.")

    monkeypatch.setattr(preparation, "fetch_source_snapshot", fetch)
    for _ in range(SNAPSHOT_RETRY_ATTEMPTS):
        assert process(session_factory)
        with session_factory.begin() as session:
            command = session.scalar(select(RunCommand).where(RunCommand.kind == PREPARE_SNAPSHOT))
            if command.status != "PENDING":
                break
            assert session.get(Run, run.id).status == "CREATED"
            now = session.scalar(select(func.now()))
            assert command.available_at > now
            command.available_at = now

    with session_factory() as session:
        retries = session.scalars(
            select(RunEvent.payload).where(RunEvent.event_type == SNAPSHOT_RETRY)
        ).all()
        if recovers:
            assert len(attempts) == 2 and len(retries) == 1
            assert commands(session) == [(PREPARE_SNAPSHOT, "DONE"), ("START", "PENDING")]
        else:
            assert len(attempts) == SNAPSHOT_RETRY_ATTEMPTS
            assert len(retries) == SNAPSHOT_RETRY_ATTEMPTS - 1
            assert session.get(Run, run.id).status == "FAILED"
            assert stop_code(session) == "snapshot_unavailable"


def test_oversized_context_stops_without_retrying(session_factory, pending, monkeypatch):
    run, snapshot, _, _ = pending

    def too_large(_run, _snapshot):
        raise ValueError("Analysis context exceeds the 2 MiB storage limit.")

    monkeypatch.setattr(preparation, "fetch_source_snapshot", lambda *_, **__: snapshot)
    monkeypatch.setattr(preparation, "prepare_analysis_context", too_large)
    assert process(session_factory)
    with session_factory() as session:
        assert session.get(Run, run.id).status == "FAILED"
        assert commands(session) == [(PREPARE_SNAPSHOT, "FAILED")]
        assert stop_code(session) == "snapshot_unsupported"


def test_a_saved_context_only_queues_analysis(session_factory, pending):
    run, _, context, _ = pending
    with session_factory.begin() as session:
        save_analysis_context(session, context)
    assert process(session_factory, lambda _run: pytest.fail("Rechecked access needlessly"))
    with session_factory() as session:
        assert commands(session) == [(PREPARE_SNAPSHOT, "DONE"), ("START", "PENDING")]
