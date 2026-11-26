import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid5

import httpx
import pytest
from alembic import command
from sqlalchemy import select

import refactra.repository_analysis_worker as analysis_worker
import refactra.repository_plan_worker as planner
import refactra.run_access as run_access
import refactra.worker as worker
from refactra.analysis_context import prepare_analysis_context, save_analysis_context
from refactra.analysis_request import BudgetExceeded
from refactra.model_reservations import settle_model_usage
from refactra.models import ModelReservation, RunCommand, RunEvent, RunPlanApproval
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.planning_request import PLAN_INSTRUCTIONS, load_planning_context
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    PlanContent,
    PlanFeedback,
    PlanProposal,
    PlanRevisionConflict,
    approve_refactoring_plan,
    evaluate_plan_readiness,
    load_refactoring_plan,
    queue_plan_feedback,
    save_refactoring_plan,
)
from refactra.repository_analysis_worker import (
    GENERATE_PLAN,
    WORKFLOW,
    enqueue_repository_analysis,
    process_repository_analysis,
    process_repository_plan_approval,
)
from refactra.repository_snapshot import SourceFile
from refactra.worker import claim_command

from .conftest import migration_config
from .test_analysis_context import prepare
from .test_openai_analysis import response


@pytest.fixture
def proposal():
    return {
        "summary": "Extract the cart calculation.",
        "changes": [{"path": "src/cart.py", "action": "MODIFY", "intent": "Extract calculation"}],
        "acceptance_criteria": [{"id": "AC-1", "description": "Cart totals stay the same."}],
        "validation_checks": [
            {
                "check_id": "python.pytest",
                "paths": ["tests/test_cart.py"],
                "required": True,
                "criteria_ids": ["AC-1"],
            }
        ],
        "preserve_behavior": ["Keep the same totals and errors."],
        "assumptions": [],
        "blocking_questions": [],
    }


def test_revisions_pin_exact_content_and_stale_saves_cannot_replace_them(session_factory, proposal):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        with pytest.raises(ValueError, match="completed analysis"):
            save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        enqueue_repository_analysis(session, run.id)

    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        first = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        assert first.revision == 1 and first.sha256 == sha256(first.document).hexdigest()

    with session_factory.begin() as session:
        assert save_refactoring_plan(session, run.id, proposal, expected_revision=0).id == first.id
        revised = dict(proposal, summary="Extract cart calculation and document the rounding.")
        with pytest.raises(ValueError, match="revision changed"):
            save_refactoring_plan(session, run.id, revised, expected_revision=0)
        second = save_refactoring_plan(session, run.id, revised, expected_revision=1)
        assert second.revision == 2
        assert second.id != first.id and second.sha256 != first.sha256

    with session_factory() as session:
        original = load_refactoring_plan(session, run.id, revision=1)
        assert original.id == first.id and original.document == first.document
        payload = json.loads(load_refactoring_plan(session, run.id).document)
        assert payload["summary"] == revised["summary"] and payload["run_id"] == str(run.id)
        assert payload["repository"] == {
            "repository_id": run.github_repository_id,
            "base_branch": run.base_branch,
            "base_sha": run.base_sha,
        }
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == "PLAN_SAVED").order_by(RunEvent.id)
        ).all()
        assert [event.payload for event in events] == [
            {"plan_id": str(plan.id), "revision": plan.revision, "sha256": plan.sha256}
            for plan in (first, second)
        ]


def test_revision_context_treats_feedback_as_current_and_initial_analysis_as_stale(
    session_factory, proposal
):
    run, _, context = prepare(session_factory, task_text="Add asynchronous browser search.")
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        feedback = PlanFeedback(
            expected_revision=1, notes="Keep the existing HTML form.", answers=[]
        )
        queue_plan_feedback(session, run.id, feedback)
        command_id = session.scalar(select(RunCommand.id).where(RunCommand.kind == GENERATE_PLAN))

        initial = json.loads(load_planning_context(session, run.id).document)
        assert initial["task_text"] == run.task_text and "saved_analysis" in initial
        revised = json.loads(load_planning_context(session, run.id, command_id).document)
        assert revised["original_request"] == run.task_text
        assert revised["reviewer_feedback"]["notes"] == feedback.notes
        assert "task_text" not in revised and "saved_analysis" not in revised


def test_plan_budget_leaves_capacity_for_candidate(session_factory, monkeypatch):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        usage = session.scalar(select(ModelReservation).where(ModelReservation.run_id == run.id))
        usage.actual_input_tokens = 26_000

    def model(request):
        assert request.url.path.endswith("/input_tokens"), "Planning spent the candidate budget"
        return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 3000})

    monkeypatch.setattr(planner, "authorize_plan", lambda run: None)
    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    with pytest.raises(BudgetExceeded, match="run_input_tokens"):
        planner.generate_initial_plan(session_factory, run.id, provider)
    with session_factory() as session:
        assert load_refactoring_plan(session, run.id) is None


def analyze_fixture_run(session_factory, *, queue_plan=False):
    def mock_model(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        return httpx.Response(200, json=response())

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(mock_model))
    command = claim_command(session_factory, "fixture", workflow=WORKFLOW)
    assert process_repository_analysis(
        session_factory,
        command,
        "fixture",
        provider,
        queue_plan=queue_plan,
        authorize=lambda _run: None,
    )


def test_tracked_template_requirement_blocks_an_omission(session_factory, proposal):
    run, snapshot, _ = prepare(
        session_factory,
        task_text="Change the cart behavior and update the example environment file if one exists.",
    )
    snapshot = replace(snapshot, files=(*snapshot.files, SourceFile(".env-example", b"KEY=\n")))
    with session_factory.begin() as session:
        save_analysis_context(session, prepare_analysis_context(run, snapshot))
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        first = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        readiness = evaluate_plan_readiness(PlanContent.model_validate_json(first.document))
        assert readiness.scope_errors == [
            "The request calls for MODIFY .env-example, but the plan does not include that action."
        ]
        with pytest.raises(PlanRevisionConflict, match="scope issues"):
            approve_refactoring_plan(
                session,
                run.id,
                plan_id=first.id,
                revision=1,
                sha256=first.sha256,
                actor_id=run.requested_by,
            )
        proposal["changes"].append(
            {"path": ".env-example", "action": "MODIFY", "intent": "Add a default."}
        )
        revised = save_refactoring_plan(session, run.id, proposal, expected_revision=1)
        assert (
            evaluate_plan_readiness(PlanContent.model_validate_json(revised.document)).outcome
            == "ready"
        )


def test_edits_to_files_withheld_from_analysis_block_approval(session_factory, proposal):
    run, snapshot, _ = prepare(session_factory)
    secret = b'ACCESS_TOKEN = "synthetic-plan-secret"\n\ndef cart():\n    return 2\n'
    snapshot = replace(snapshot, files=(SourceFile("src/cart.py", secret), *snapshot.files[1:]))
    context = prepare_analysis_context(run, snapshot)
    assert b"synthetic-plan-secret" not in context.document
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        first = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        readiness = evaluate_plan_readiness(PlanContent.model_validate_json(first.document))
        assert readiness.outcome == "unsupported"
        assert readiness.scope_errors == [
            "src/cart.py may contain a credential and cannot be sent to the model for editing."
        ]
        with pytest.raises(PlanRevisionConflict, match="scope issues"):
            approve_refactoring_plan(
                session,
                run.id,
                plan_id=first.id,
                revision=1,
                sha256=first.sha256,
                actor_id=run.requested_by,
            )
        proposal["changes"] = [
            {"path": "src/checkout.py", "action": "CREATE", "intent": "Add checkout totals."}
        ]
        revised = save_refactoring_plan(session, run.id, proposal, expected_revision=1)
        content = PlanContent.model_validate_json(revised.document)
        assert evaluate_plan_readiness(content).outcome == "ready"


@pytest.mark.parametrize(
    "task_text, missing",
    [
        ("Delete src/legacy-cart.py.", True),
        (
            "Delete `src/legacy-cart.py`. If that exact file is absent from the selected branch, "
            "tell me it is absent and stop. Do not delete a similarly named file or propose "
            "unrelated changes.",
            True,
        ),
        ("Delete src/cart.py.", False),
        ("Delete src/legacy-cart.py and update src/cart.py.", False),
    ],
)
def test_exact_missing_delete_saves_an_unapprovable_plan_without_a_model_call(
    session_factory, proposal, monkeypatch, task_text, missing
):
    run, _, context = prepare(session_factory, task_text=task_text)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory, queue_plan=True)
    monkeypatch.setattr(planner, "authorize_plan", lambda _: None)
    command_id = claim_command(session_factory, "fixture", workflow=WORKFLOW, kind=GENERATE_PLAN)

    def model(request):
        assert not missing, "A missing deletion target needs no planning request."
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        payload = response()
        payload["output"][0]["content"][0]["text"] = json.dumps(proposal)
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    assert planner.process_initial_plan(session_factory, command_id, "fixture", provider)
    with session_factory.begin() as session:
        plan = load_refactoring_plan(session, run.id)
        assert plan.revision == 1 and session.get(RunCommand, command_id).status == "DONE"
        attempt = session.get(
            ModelReservation, uuid5(run.id, "repository:initial-plan:structured-v1")
        )
        assert (attempt is None) == missing
        if missing:
            content = json.loads(plan.document)
            assert content["summary"] == (
                "src/legacy-cart.py is absent from the pinned source snapshot. "
                "There is nothing to delete."
            )
            assert content["changes"] == content["validation_checks"] == []
            with pytest.raises(PlanRevisionConflict, match="requires a plan with proposed changes"):
                approve_refactoring_plan(
                    session,
                    run.id,
                    plan_id=plan.id,
                    revision=1,
                    sha256=plan.sha256,
                    actor_id=run.requested_by,
                )
        else:
            assert json.loads(plan.document)["changes"] == proposal["changes"]


@pytest.mark.parametrize("outcome", ["fresh", "reclaimed", "denied"])
def test_workflow_worker_saves_initial_plan_without_a_manual_command(
    database, session_factory, proposal, monkeypatch, tmp_path, outcome
):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)

    calls = []

    def model(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        body = json.loads(request.content)
        payload = response()
        if body["instructions"] == PLAN_INSTRUCTIONS:
            payload["output"][0]["content"][0]["text"] = json.dumps(proposal)
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))

    def authorize(run_to_check):
        assert run_to_check.id == run.id
        if outcome == "denied":
            raise ValueError("Private authorization failure.")

    monkeypatch.setattr(planner, "authorize_plan", authorize)
    monkeypatch.setattr(analysis_worker, "authorize_run_repository", lambda _run: None)
    monkeypatch.setattr(worker, "make_engine", lambda _: database)
    monkeypatch.setattr(worker, "OpenAIAnalysisProvider", lambda _: provider)
    monkeypatch.setattr(worker, "WORKER_LOCK", run.id.int >> 65)
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setenv("REFACTRA_OPENAI_API_KEY", "fixture")
    monkeypatch.setattr(
        "sys.argv",
        [
            "worker",
            "--repository-workflow",
            "--once",
            "--candidate-source",
            str(tmp_path),
            "--candidate-artifacts",
            str(tmp_path),
        ],
    )
    worker.main()

    if outcome == "reclaimed":
        command_id = claim_command(session_factory, "old", workflow=WORKFLOW, kind=GENERATE_PLAN)
        planner.generate_initial_plan(session_factory, run.id, provider)
        with session_factory.begin() as session:
            session.get(RunCommand, command_id).lease_until = datetime.now(UTC) - timedelta(
                minutes=1
            )

    worker.main()
    worker.main()
    with session_factory() as session:
        job = session.scalar(select(RunCommand).where(RunCommand.kind == GENERATE_PLAN))
        plan = load_refactoring_plan(session, run.id)
        if outcome == "denied":
            assert job.status == "FAILED" and plan is None
            event = session.scalar(
                select(RunEvent).where(RunEvent.event_type == "PLAN_GENERATION_FAILED")
            )
            assert event.payload == {"code": "plan_generation_failed", "command_id": str(job.id)}
        else:
            assert job.status == "DONE" and plan.revision == 1
        assert session.get(RunPlanApproval, run.id) is None
    assert calls == ["/v1/responses/input_tokens", "/v1/responses"] * (
        1 if outcome == "denied" else 2
    )


@pytest.mark.parametrize("scenario", ["new", "legacy", "mismatch"])
def test_approval_worker_recovers_and_checks_the_saved_plan(
    database, session_factory, proposal, scenario
):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        approval = approve_refactoring_plan(
            session,
            run.id,
            plan_id=plan.id,
            revision=1,
            sha256=plan.sha256,
            actor_id=run.requested_by,
        )
        if scenario == "mismatch":
            approval.sha256 = "a" * 64
    if scenario == "legacy":
        with database.begin() as connection:
            config = migration_config(connection)
            command.downgrade(config, "0009_plan_approvals")
            command.upgrade(config, "head")
    assert claim_command(session_factory, "analysis", workflow=WORKFLOW) is None
    command_id = claim_command(session_factory, "old", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL)
    assert command_id is not None
    with session_factory.begin() as session:
        session.get(RunCommand, command_id).lease_until = datetime.now(UTC) - timedelta(minutes=1)
    assert (
        claim_command(session_factory, "new", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL)
        == command_id
    )
    assert not process_repository_plan_approval(session_factory, command_id, "old")
    assert process_repository_plan_approval(session_factory, command_id, "new")
    assert not process_repository_plan_approval(session_factory, command_id, "new")
    assert (
        claim_command(session_factory, "new", workflow=WORKFLOW, kind=APPLY_PLAN_APPROVAL) is None
    )
    with session_factory() as session:
        event = session.scalar(
            select(RunEvent).where(RunEvent.payload["command_id"].astext == str(command_id))
        )
        expected_status, expected_event = (
            ("FAILED", "PLAN_APPROVAL_REJECTED")
            if scenario == "mismatch"
            else ("DONE", "PLAN_APPROVAL_VERIFIED")
        )
        assert session.get(RunPlanApproval, run.id).approved_at == approval.approved_at
        assert session.get(RunCommand, command_id).status == expected_status
        assert event.event_type == expected_event
        if scenario != "mismatch":
            assert event.payload == {
                "plan_id": str(plan.id),
                "revision": 1,
                "sha256": plan.sha256,
                "command_id": str(command_id),
            }


@pytest.mark.parametrize("invalid", ["traversal", "unknown_criterion", "supplied_identity"])
def test_untrusted_plan_validation_rejects_invalid_paths_and_references(proposal, invalid):
    if invalid == "traversal":
        proposal["changes"][0]["path"] = "src/../outside.py"
    elif invalid == "unknown_criterion":
        proposal["validation_checks"][0]["criteria_ids"] = ["AC-99"]
    else:
        proposal["repository"] = {"base_sha": "a" * 40}
    with pytest.raises(ValueError):
        PlanProposal.model_validate(proposal)


def test_plan_readiness_reports_unknown_checks_without_blocking_approval(proposal):
    check = proposal["validation_checks"][0]
    proposal["validation_checks"] = [
        dict(check, check_id="VC2"),
        dict(check, required=False),
        dict(check, check_id="VC1", required=False),
        dict(check, check_id="VC2", required=False),
    ]
    readiness = evaluate_plan_readiness(PlanProposal.model_validate(proposal))
    assert readiness.unsupported_check_ids == ["VC1", "VC2"]
    assert readiness.has_required_supported_check is True
    assert readiness.outcome == "ready"
    assert readiness.blocking_question_count == 0


@pytest.mark.parametrize(
    ("valid", "legacy_state"),
    [(True, None), (False, None), (True, "settled"), (True, "held")],
)
def test_operator_plan_uses_saved_inputs_and_one_paid_receipt(
    session_factory, proposal, monkeypatch, tmp_path, valid, legacy_state
):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    key = tmp_path / "app.pem"
    key.write_bytes(b"fixture")
    for name, value in {
        "REFACTRA_GITHUB_CLIENT_ID": "fixture-client",
        "REFACTRA_GITHUB_PRIVATE_KEY_PATH": str(key),
        "REFACTRA_ALLOWED_GITHUB_USER_IDS": "999",
    }.items():
        monkeypatch.setenv(name, value)

    def verify_permission(app, installation, repository, user):
        assert (installation, repository, user.user_id) == (77, 42, run.requested_by)

    def installation(app, repository):
        assert repository == 42
        return 77

    monkeypatch.setattr(run_access, "require_github_write_access", verify_permission)
    monkeypatch.setattr(run_access.GitHubAppClient, "repository_installation", installation)
    legacy_id = uuid5(run.id, "repository:initial-plan")
    attempt_id = uuid5(run.id, "repository:initial-plan:structured-v1")
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["instructions"] == PLAN_INSTRUCTIONS
        assert "python.pytest" in body["instructions"]
        assert body["text"]["format"] == {
            "type": "json_schema",
            "name": "refactoring_plan",
            "strict": True,
            "schema": PlanProposal.model_json_schema(),
        }
        context = json.loads(body["input"])
        assert context["saved_analysis"] == "src/cart.py:1 needs review"
        assert context["base_sha"] == run.base_sha
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 500}
            )
        with session_factory() as session:
            assert session.get(ModelReservation, attempt_id).settled_at is None
        assert body["input"] == requests[0]["input"] and body["tools"] == []
        payload = response()
        if not valid:
            # This shape can satisfy the provider schema but still violates a local rule.
            proposal["validation_checks"][0]["criteria_ids"] = ["AC-unknown"]
        payload["output"][0]["content"][0]["text"] = json.dumps(proposal)
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    with pytest.raises(ValueError, match="Repository access is not allowed"):
        planner.generate_initial_plan(session_factory, run.id, provider)
    assert requests == []
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", str(run.requested_by))
    if legacy_state is not None:
        with session_factory.begin() as session:
            analysis = session.scalar(
                select(ModelReservation).where(ModelReservation.run_id == run.id)
            )
            session.add(
                ModelReservation(
                    id=legacy_id,
                    run_id=run.id,
                    budget_month=analysis.budget_month,
                    request_hash="a" * 64,
                    model=analysis.model,
                    input_tokens=100,
                    output_tokens=3000,
                    maximum_cost_usd=Decimal("0.02"),
                    pricing=analysis.pricing,
                )
            )
            session.flush()
            if legacy_state == "settled":
                settle_model_usage(session, legacy_id, input_tokens=100, output_tokens=20)
    if legacy_state == "held":
        with pytest.raises(planner.PlanGenerationError, match="unknown outcome"):
            planner.generate_initial_plan(session_factory, run.id, provider)
        assert requests == []
        return
    if valid:
        plan = planner.generate_initial_plan(session_factory, run.id, provider)
        assert plan.revision == 1
        replay = planner.generate_initial_plan(session_factory, run.id, provider)
        assert replay.id == plan.id and replay.document == plan.document
    else:
        with pytest.raises(planner.PlanGenerationError, match=r"proposal schema \(value_error\)"):
            planner.generate_initial_plan(session_factory, run.id, provider)
        with pytest.raises(planner.PlanGenerationError, match="cannot be resent"):
            planner.generate_initial_plan(session_factory, run.id, provider)
    assert len(requests) == 2  # Count and generation, even when the command is repeated.
    with session_factory() as session:
        assert session.get(ModelReservation, attempt_id).settled_at is not None
        assert (load_refactoring_plan(session, run.id) is not None) == valid
