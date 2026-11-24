import json
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from refactra.analysis_context import save_analysis_context
from refactra.candidate_patch import CandidatePatch
from refactra.candidate_results import CandidateResultError, save_validated_candidate
from refactra.models import RunCandidateResult, RunEvent
from refactra.refactoring_plan import approve_refactoring_plan, save_refactoring_plan
from refactra.repository_analysis_worker import enqueue_repository_analysis
from refactra.repository_validation import ValidationResult

from .test_analysis_context import prepare
from .test_refactoring_plan import analyze_fixture_run
from .test_refactoring_plan import proposal as proposal


def passing_patch(*paths):
    return ValidationResult(
        "repository.patch", list(paths), True, "PASSED", 0, 0, "Patch verified.", None, "builtin"
    )


def test_saved_results_need_evidence_for_each_approved_go_check(session_factory, proposal):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    proposal["changes"] = [
        {"path": "svc/api/handler.go", "action": "MODIFY", "intent": "Rename a helper."}
    ]
    proposal["validation_checks"] = [
        {
            "check_id": "go.test",
            "paths": ["svc/report/report_test.go"],
            "required": True,
            "criteria_ids": ["AC-1"],
        }
    ]
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        approve_refactoring_plan(
            session,
            run.id,
            plan_id=plan.id,
            revision=plan.revision,
            sha256=plan.sha256,
            actor_id=run.requested_by,
        )

    candidate = CandidatePatch(
        base_sha=run.base_sha,
        plan_sha256=plan.sha256,
        edits=[{"path": "svc/api/handler.go", "action": "MODIFY", "content": "package api\n"}],
    )

    def go_result(path):
        return ValidationResult("go.test", [path], True, "PASSED", 0, 10, "ok", None, "fixture")

    approved = go_result("svc/report/report_test.go")
    inferred = go_result("svc/api/handler.go")
    patch_check = passing_patch("svc/api/handler.go")
    diff = (
        b"--- a/svc/api/handler.go\n+++ b/svc/api/handler.go\n@@ -1 +1 @@\n"
        b"-package old\n+package api\n"
    )
    with session_factory.begin() as session:
        for missing in ([inferred, patch_check], [approved, patch_check]):
            with pytest.raises(CandidateResultError, match="required checks"):
                save_validated_candidate(session, run.id, candidate, diff, missing)
        saved = save_validated_candidate(
            session, run.id, candidate, diff, [approved, inferred, patch_check]
        )
        evidence = json.loads(saved.document)["validation"]
        assert [(item["check_id"], item["paths"]) for item in evidence] == [
            ("go.test", ["svc/report/report_test.go"]),
            ("go.test", ["svc/api/handler.go"]),
            ("repository.patch", ["svc/api/handler.go"]),
        ]


def test_candidate_results_bind_passing_checks_to_immutable_revisions(session_factory, proposal):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    with session_factory.begin() as session:
        plan = save_refactoring_plan(session, run.id, proposal, expected_revision=0)
        approve_refactoring_plan(
            session,
            run.id,
            plan_id=plan.id,
            revision=plan.revision,
            sha256=plan.sha256,
            actor_id=run.requested_by,
        )

    candidate = CandidatePatch(
        base_sha=run.base_sha,
        plan_sha256=plan.sha256,
        edits=[{"path": "src/cart.py", "action": "MODIFY", "content": "def cart(): return 2\n"}],
    )
    check = ValidationResult(
        "python.pytest", ["tests/test_cart.py"], True, "PASSED", 0, 50, "1 passed", None, "fixture"
    )
    patch_check = passing_patch("src/cart.py")
    diff = (
        b"--- a/src/cart.py\n+++ b/src/cart.py\n@@ -1 +1 @@\n"
        b"-def cart(): return 1\n+def cart(): return 2\n"
    )
    with session_factory.begin() as session:
        with pytest.raises(CandidateResultError, match="approved commit and plan"):
            stale = candidate.model_copy(update={"plan_sha256": "b" * 64})
            save_validated_candidate(session, run.id, stale, diff, [check])
        for invalid in (
            [],
            [check],
            [replace(check, exit_code=1), patch_check],
            [replace(check, paths=["test_other.py"]), patch_check],
            [check, replace(patch_check, status="UNAVAILABLE", exit_code=None, reason="Missing")],
        ):
            with pytest.raises(CandidateResultError, match="required checks"):
                save_validated_candidate(session, run.id, candidate, diff, invalid)
        assert session.scalar(select(func.count()).select_from(RunCandidateResult)) == 0
        first = save_validated_candidate(session, run.id, candidate, diff, [check, patch_check])

    with session_factory.begin() as session:
        retry = save_validated_candidate(
            session, run.id, candidate, diff, [replace(check, duration_ms=99), patch_check]
        )
        assert retry.id == first.id and retry.document == first.document
        with pytest.raises(CandidateResultError, match="different diff"):
            save_validated_candidate(session, run.id, candidate, diff + b"\n", [check, patch_check])
        edit = candidate.edits[0].model_copy(update={"content": "def cart(): return 3\n"})
        updated = candidate.model_copy(update={"edits": [edit]})
        second = save_validated_candidate(
            session, run.id, updated, diff.replace(b"2\n", b"3\n"), [check, patch_check]
        )

    with session_factory() as session:
        saved = session.get(RunCandidateResult, first.id)
        artifact = json.loads(saved.document)
        assert saved.revision == 1 and second.revision == 2 and saved.sha256 != second.sha256
        assert saved.sha256 == sha256(saved.document).hexdigest()
        assert artifact["approved_plan"]["sha256"] == plan.sha256
        assert artifact["candidate"] == candidate.model_dump(mode="json")
        assert artifact["diff"] == diff.decode() and artifact["validation"][0]["exit_code"] == 0
        events = session.scalars(
            select(RunEvent).where(RunEvent.event_type == "CANDIDATE_RESULT_SAVED")
        )
        assert {item.payload["result_id"] for item in events} == {str(first.id), str(second.id)}
