import json
from hashlib import sha256
from uuid import uuid5

import httpx
import pytest
from sqlalchemy import select

import refactra.repository_plan_worker as planner
from refactra.analysis_context import save_analysis_context
from refactra.models import ModelReservation, RunEvent
from refactra.openai_analysis import OpenAIAnalysisProvider
from refactra.planning_request import PLAN_INSTRUCTIONS
from refactra.refactoring_plan import PlanProposal, load_refactoring_plan, save_refactoring_plan
from refactra.repository_analysis_worker import (
    WORKFLOW,
    enqueue_repository_analysis,
    process_repository_analysis,
)
from refactra.worker import claim_command

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


def analyze_fixture_run(session_factory):
    def mock_model(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        return httpx.Response(200, json=response())

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(mock_model))
    command = claim_command(session_factory, "fixture", workflow=WORKFLOW)
    assert process_repository_analysis(session_factory, command, "fixture", provider)


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


@pytest.mark.parametrize("valid", [True, False])
def test_operator_plan_uses_saved_inputs_and_one_paid_receipt(
    session_factory, proposal, monkeypatch, tmp_path, valid
):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    key = tmp_path / "app.pem"
    key.write_bytes(b"fixture")
    for name, value in {
        "REFACTRA_GITHUB_REPOSITORY_ID": "42",
        "REFACTRA_GITHUB_INSTALLATION_ID": "77",
        "REFACTRA_GITHUB_CLIENT_ID": "fixture-client",
        "REFACTRA_GITHUB_PRIVATE_KEY_PATH": str(key),
        "REFACTRA_ALLOWED_GITHUB_USER_IDS": "999",
    }.items():
        monkeypatch.setenv(name, value)

    def verify_permission(app, installation, repository, user):
        assert (installation, repository, user.user_id) == (77, 42, run.requested_by)

    monkeypatch.setattr(planner, "require_github_write_access", verify_permission)
    attempt_id = uuid5(run.id, "repository:initial-plan")
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["instructions"] == PLAN_INSTRUCTIONS
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
        payload["output"][0]["content"][0]["text"] = json.dumps(proposal) if valid else "{}"
        return httpx.Response(200, json=payload)

    provider = OpenAIAnalysisProvider("fixture", httpx.MockTransport(model))
    with pytest.raises(ValueError, match="Repository access is not allowed"):
        planner.generate_initial_plan(session_factory, run.id, provider)
    assert requests == []
    monkeypatch.setenv("REFACTRA_ALLOWED_GITHUB_USER_IDS", str(run.requested_by))
    if valid:
        plan = planner.generate_initial_plan(session_factory, run.id, provider)
        assert plan.revision == 1
        assert planner.generate_initial_plan(session_factory, run.id, provider).id == plan.id
    else:
        with pytest.raises(planner.PlanGenerationError, match="proposal schema"):
            planner.generate_initial_plan(session_factory, run.id, provider)
        with pytest.raises(planner.PlanGenerationError, match="cannot be resent"):
            planner.generate_initial_plan(session_factory, run.id, provider)
    assert len(requests) == 2  # Count and generation, even when the command is repeated.
    with session_factory() as session:
        assert session.get(ModelReservation, attempt_id).settled_at is not None
        assert (load_refactoring_plan(session, run.id) is not None) == valid
