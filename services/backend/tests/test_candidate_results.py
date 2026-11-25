import json
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from refactra.analysis_context import save_analysis_context
from refactra.candidate_patch import CandidatePatch
from refactra.candidate_results import CandidateResultError, save_validated_candidate
from refactra.models import RunCandidateResult, RunEvent, RunPublicationTarget
from refactra.publication_target import (
    PublicationTargetError,
    approve_publication_target,
    waive_publication_check,
)
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


def _save_target(session, result):
    document = json.dumps({"result_id": str(result.id)}).encode()
    target = RunPublicationTarget(
        result_id=result.id, sha256=sha256(document).hexdigest(), document=document
    )
    session.add(target)
    session.flush()
    return target


def test_unavailable_required_checks_block_publication_until_waived(session_factory, proposal):
    run, _, context = prepare(session_factory)
    with session_factory.begin() as session:
        save_analysis_context(session, context)
        enqueue_repository_analysis(session, run.id)
    analyze_fixture_run(session_factory)
    proposal["validation_checks"].append(
        {
            "check_id": "typescript.tsc",
            "paths": ["tsconfig.json"],
            "required": False,
            "criteria_ids": ["AC-1"],
        }
    )
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

    def candidate(value):
        return CandidatePatch(
            base_sha=run.base_sha,
            plan_sha256=plan.sha256,
            edits=[{"path": "src/cart.py", "action": "MODIFY", "content": f"{value}\n"}],
        )

    def unavailable(check_id, path, required):
        return ValidationResult(
            check_id, [path], required, "UNAVAILABLE", None, 0, "", "Image unavailable.", "x"
        )

    checks = [
        unavailable("python.pytest", "tests/test_cart.py", True),
        unavailable("typescript.tsc", "tsconfig.json", False),
        passing_patch("src/cart.py"),
    ]
    owner = run.requested_by

    def waive(session, result, index, reason="Lock image needs network; tested manually."):
        return waive_publication_check(
            session,
            run.id,
            result_id=result.id,
            result_sha256=result.sha256,
            check_index=index,
            reason=reason,
            actor_id=owner,
        )

    def approve(session, result, target):
        return approve_publication_target(
            session, run.id, result_id=result.id, target_sha256=target.sha256, actor_id=owner
        )

    with session_factory.begin() as session:
        first = save_validated_candidate(session, run.id, candidate("one"), b"diff one", checks)
        first_target = _save_target(session, first)
        with pytest.raises(PublicationTargetError, match="Waive each required check"):
            approve(session, first, first_target)
        for index in (1, 2):  # optional or passing checks cannot be waived
            with pytest.raises(PublicationTargetError, match="Only required checks"):
                waive(session, first, index)
        with pytest.raises(PublicationTargetError, match="cannot accept"):
            waive_publication_check(
                session,
                run.id,
                result_id=first.id,
                result_sha256=first.sha256,
                check_index=0,
                reason="Not the owner.",
                actor_id=owner + 1,
            )
        waiver = waive(session, first, 0, reason="  Lock image needs network; tested manually.  ")
        assert waiver.reason == "Lock image needs network; tested manually."
        assert (waiver.check_id, waiver.result_sha256) == ("python.pytest", first.sha256)
        assert waive(session, first, 0) == waiver
        with pytest.raises(PublicationTargetError, match="already waived"):
            waive(session, first, 0, reason="A different reason.")

    with session_factory.begin() as session:
        second = save_validated_candidate(session, run.id, candidate("two"), b"diff two", checks)
        second_target = _save_target(session, second)
        with pytest.raises(PublicationTargetError, match="result changed"):
            waive(session, first, 0)
        with pytest.raises(PublicationTargetError, match="Waive each required check"):
            approve(session, second, second_target)
        waive(session, second, 0)
        approval = approve(session, second, second_target)
        assert approval.result_revision == second.revision == 2
        with pytest.raises(PublicationTargetError, match="already approved"):
            waive(session, second, 0, reason="Late change.")


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
