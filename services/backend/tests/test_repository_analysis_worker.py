from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from refactra.analysis_context import save_analysis_context
from refactra.github_app import GitHubAppError
from refactra.models import ModelReservation, Run, RunAnalysisResult, RunCommand, RunEvent
from refactra.openai_analysis import (
    AnalysisProviderError,
    OpenAIAnalysisProvider,
    execute_read_only_analysis,
)
from refactra.repository_analysis_worker import (
    ACCESS_RETRY_ATTEMPTS,
    ANALYSIS_ACCESS_RETRY,
    WORKFLOW,
    enqueue_repository_analysis,
    process_repository_analysis,
)
from refactra.run_access import RunAccessDenied, authorize_run_repository
from refactra.worker import claim_command

from .test_analysis_context import prepare
from .test_openai_analysis import response


@pytest.fixture
def queued(session_factory):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="saved analysis context"):
            enqueue_repository_analysis(session, run.id)
        save_analysis_context(session, context)
        command_id = enqueue_repository_analysis(session, run.id)
        assert enqueue_repository_analysis(session, run.id) == command_id
    return run.id, command_id


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("complete", "analysis_ready"),
        ("incomplete", "analysis_provider_failed"),
        ("timeout", "analysis_outcome_unknown"),
    ],
)
def test_worker_persists_analysis_or_safe_failure(session_factory, queued, outcome, code):
    run_id, command_id = queued
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        with session_factory() as session:
            assert session.get(ModelReservation, command_id).settled_at is None
            assert session.get(Run, run_id).status == "ANALYZING"
        if outcome == "timeout":
            raise httpx.ReadTimeout("private upstream error", request=request)
        payload = response()
        if outcome == "incomplete":
            payload["status"] = "incomplete"
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("test-key", httpx.MockTransport(handle))
    assert claim_command(session_factory, "mock") is None
    assert claim_command(session_factory, "worker", workflow=WORKFLOW) == command_id
    assert process_repository_analysis(
        session_factory, command_id, "worker", provider, authorize=lambda _run: None
    )
    assert claim_command(session_factory, "worker", workflow=WORKFLOW) is None
    assert calls == ["/v1/responses/input_tokens", "/v1/responses"]
    with session_factory() as session:
        result = session.get(RunAnalysisResult, run_id)
        assert (session.get(ModelReservation, command_id).settled_at is None) == (
            outcome == "timeout"
        )
        run = session.get(Run, run_id)
        command = session.get(RunCommand, command_id)
        if outcome == "complete":
            assert (run.status, command.status) == ("NEEDS_HUMAN_REVIEW", "DONE")
            assert result.analysis_text == "src/cart.py:1 needs review"
            assert result.reservation_id == command_id
        else:
            assert (run.status, command.status) == ("FAILED", "FAILED")
            assert result is None
        event = session.scalar(select(RunEvent).where(RunEvent.event_type == "RUN_STOPPED"))
        assert event.payload == {"code": code}


@pytest.mark.parametrize(
    "change", [{"requested_by": 7}, {"github_repository_id": 43}, {"github_installation_id": 78}]
)
def test_local_policy_denial_is_distinct_and_needs_no_credentials(monkeypatch, change):
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", "123")
    monkeypatch.setenv("REFACTRA_GITHUB_REPOSITORY_ID", "42")
    monkeypatch.setenv("REFACTRA_GITHUB_INSTALLATION_ID", "77")
    monkeypatch.delenv("REFACTRA_GITHUB_PRIVATE_KEY_PATH", raising=False)
    owner = {"requested_by": 123, "github_repository_id": 42, "github_installation_id": 77}
    with pytest.raises(RunAccessDenied):
        authorize_run_repository(Run(**(owner | change)))


def _recording_provider(calls):
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        return httpx.Response(200, json=response())

    return OpenAIAnalysisProvider("test-key", httpx.MockTransport(handle))


def _stop_code(session):
    event = session.scalar(select(RunEvent).where(RunEvent.event_type == "RUN_STOPPED"))
    return event.payload["code"]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (RunAccessDenied("Removed from the allowlist."), "analysis_access_revoked"),
        (HTTPException(403, "Write access was removed."), "analysis_access_revoked"),
        (KeyError("REFACTRA_GITHUB_INSTALLATION_ID"), "analysis_access_unconfigured"),
        (OSError("Private key is missing."), "analysis_access_unconfigured"),
    ],
)
def test_lost_or_unusable_access_stops_before_any_provider_call(
    session_factory, queued, failure, code
):
    run_id, command_id = queued
    calls = []

    def authorize(run):
        assert run.id == run_id
        raise failure

    assert claim_command(session_factory, "worker", workflow=WORKFLOW) == command_id
    assert process_repository_analysis(
        session_factory, command_id, "worker", _recording_provider(calls), authorize=authorize
    )
    assert calls == []
    with session_factory() as session:
        assert session.get(ModelReservation, command_id) is None
        assert session.get(Run, run_id).status == "FAILED"
        assert session.get(RunCommand, command_id).status == "FAILED"
        assert _stop_code(session) == code


@pytest.mark.parametrize("recovers", [True, False])
def test_temporary_github_failure_retries_without_provider_calls(session_factory, queued, recovers):
    run_id, command_id = queued
    calls = []
    provider = _recording_provider(calls)

    def unavailable(_run):
        raise GitHubAppError("GitHub repository permissions could not be verified.")

    for attempt in range(1, ACCESS_RETRY_ATTEMPTS + 1):
        assert claim_command(session_factory, "worker", workflow=WORKFLOW) == command_id
        authorize = (lambda _run: None) if recovers and attempt == 2 else unavailable
        assert process_repository_analysis(
            session_factory, command_id, "worker", provider, authorize=authorize
        )
        with session_factory.begin() as session:
            command = session.get(RunCommand, command_id)
            if command.status != "PENDING":
                break
            assert calls == [] and session.get(Run, run_id).status == "ANALYZING"
            assert command.available_at > session.scalar(select(func.now()))
            assert claim_command(session_factory, "early", workflow=WORKFLOW) is None
            command.available_at = session.scalar(select(func.now()))

    with session_factory() as session:
        retries = session.scalars(
            select(RunEvent.payload).where(RunEvent.event_type == ANALYSIS_ACCESS_RETRY)
        ).all()
        if recovers:
            assert retries == [{"code": "analysis_access_unavailable", "attempt": 1}]
            assert calls == ["/v1/responses/input_tokens", "/v1/responses"]
            assert session.get(Run, run_id).status == "NEEDS_HUMAN_REVIEW"
            assert _stop_code(session) == "analysis_ready"
        else:
            assert len(retries) == ACCESS_RETRY_ATTEMPTS - 1
            assert calls == []
            assert session.get(ModelReservation, command_id) is None
            assert session.get(Run, run_id).status == "FAILED"
            assert _stop_code(session) == "analysis_access_unavailable"


@pytest.mark.parametrize("settled", [True, False])
def test_restart_does_not_resend_when_receipt_has_no_saved_result(session_factory, queued, settled):
    run_id, command_id = queued

    def handle(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        if not settled:
            raise httpx.ReadTimeout("no provider response", request=request)
        return httpx.Response(200, json=response())

    assert claim_command(session_factory, "old", workflow=WORKFLOW) == command_id
    provider = OpenAIAnalysisProvider("test", httpx.MockTransport(handle))
    if settled:
        execute_read_only_analysis(session_factory, run_id, command_id, provider)
    else:
        with pytest.raises(AnalysisProviderError):
            execute_read_only_analysis(session_factory, run_id, command_id, provider)
    with session_factory.begin() as session:
        session.get(RunCommand, command_id).lease_until = session.scalar(
            select(func.now())
        ) - timedelta(seconds=1)
    assert claim_command(session_factory, "new", workflow=WORKFLOW) == command_id
    assert not process_repository_analysis(session_factory, command_id, "old", None)
    assert process_repository_analysis(session_factory, command_id, "new", None)
    with session_factory() as session:
        assert session.get(Run, run_id).status == "FAILED"
        assert session.get(RunAnalysisResult, run_id) is None
        event = session.scalar(select(RunEvent).where(RunEvent.event_type == "RUN_STOPPED"))
        code = "analysis_result_missing" if settled else "analysis_outcome_unknown"
        assert event.payload == {"code": code}
        assert (session.get(ModelReservation, command_id).settled_at is not None) == settled
