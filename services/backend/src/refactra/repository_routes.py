"""Protected repository metadata and saved workflow artifacts."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from refactra.analysis_context import prepare_analysis_context, save_analysis_context
from refactra.auth_sessions import resolve_session
from refactra.diff_review import DiffReview
from refactra.github_app import GitHubAppClient, GitHubAppError
from refactra.github_auth import GitHubOAuthConfig
from refactra.github_repository_access import require_github_write_access
from refactra.models import (
    Run,
    RunAnalysisContext,
    RunCandidateResult,
    RunCommand,
    RunEvent,
    RunPlan,
    RunPlanApproval,
    RunPublicationTarget,
)
from refactra.publication_target import (
    MAX_WAIVER_REASON,
    PUBLISH_CANDIDATE,
    PublicationApproval,
    PublicationBranchRead,
    PublicationDraftRead,
    PublicationTargetError,
    PublicationWaiver,
    approve_publication_target,
    load_publication_approval,
    load_publication_progress,
    load_publication_waivers,
    waive_publication_check,
)
from refactra.refactoring_plan import (
    APPLY_PLAN_APPROVAL,
    PlanContent,
    PlanFeedback,
    PlanProposal,
    PlanReadiness,
    PlanRepository,
    PlanRevisionConflict,
    approve_refactoring_plan,
    evaluate_plan_readiness,
    load_refactoring_plan,
    plan_feedback_status,
    queue_plan_feedback,
    save_refactoring_plan,
)
from refactra.repository_access import RepositoryAccessPolicy
from refactra.repository_analysis_worker import (
    GENERATE_CANDIDATE,
    GENERATE_PLAN,
    WORKFLOW,
    enqueue_repository_analysis,
)
from refactra.repository_candidate import VALIDATE_CANDIDATE
from refactra.repository_revision import resolve_revision
from refactra.repository_runs import create_repository_run
from refactra.repository_snapshot import fetch_source_snapshot
from refactra.repository_validation import ValidationResult
from refactra.schemas import (
    RepositoryRunCreate,
    RepositoryRunRead,
    RepositoryRunReceipt,
    StopReason,
)
from refactra.session_routes import is_trusted_mutation

ANALYSIS_FAILURE_MESSAGES = {
    "analysis_access_revoked": "The run owner no longer has access to this repository.",
    "analysis_access_unavailable": "GitHub access could not be verified. Submit the run again.",
    "analysis_access_unconfigured": "The worker's repository access settings are unavailable.",
    "analysis_budget_exceeded": "The run reached an analysis budget limit.",
    "analysis_input_invalid": "The saved input could not be analyzed.",
    "analysis_outcome_unknown": "The model request outcome could not be confirmed.",
    "analysis_provider_failed": "The model did not return a complete analysis.",
    "analysis_result_missing": "The model request was recorded, but the analysis was not saved.",
}
RUN_NOT_FOUND = {"code": "not_found", "message": "Repository run not found."}


def _analysis_stop_reason(session, run_id: UUID) -> StopReason:
    payload = session.scalar(
        select(RunEvent.payload)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == "RUN_STOPPED")
        .order_by(RunEvent.id.desc())
        .limit(1)
    )
    code = payload.get("code") if isinstance(payload, dict) else None
    if isinstance(code, str) and code in ANALYSIS_FAILURE_MESSAGES:
        return StopReason(code=code, message=ANALYSIS_FAILURE_MESSAGES[code])
    return StopReason(code="analysis_failed", message="Analysis could not be completed.")


class RepositoryRead(BaseModel):
    repository_id: int
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_branch: str = Field(min_length=1)
    access: Literal["read-only"] = "read-only"


class ArtifactReference(BaseModel):
    id: UUID
    revision: int
    sha256: str


class PlanArtifact(ArtifactReference):
    run_id: UUID
    kind: Literal["PLAN"] = "PLAN"
    size: int
    created_at: datetime


class PublicationDestination(PlanRepository):
    head_branch: str


class PublicationTargetRead(BaseModel):
    sha256: str
    commit_sha: str
    destination: PublicationDestination


class PublicationApprovalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: UUID
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationWaiverCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: UUID
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    check_index: int = Field(ge=0, le=63)
    reason: str = Field(min_length=1, max_length=MAX_WAIVER_REASON)


class CandidateResultRead(ArtifactReference):
    run_id: UUID
    created_at: datetime
    repository: PlanRepository
    approved_plan: ArtifactReference
    diff: str
    diff_sha256: str
    validation: list[ValidationResult]
    review: DiffReview | None = None
    publication_target: PublicationTargetRead | None
    publication_approval: PublicationApproval | None
    publication_waivers: list[PublicationWaiver] = []
    publication: PublicationBranchRead | PublicationDraftRead | None


class CandidateProgressRead(BaseModel):
    status: Literal[
        "idle", "queued", "generating", "validating", "preparing", "publishing", "failed"
    ]
    message: str | None = None
    result: CandidateResultRead | None


CANDIDATE_STAGES = {
    APPLY_PLAN_APPROVAL: ("queued", "The worker could not verify the approved plan."),
    GENERATE_CANDIDATE: ("generating", "The worker could not generate the candidate patch."),
    VALIDATE_CANDIDATE: ("validating", "Candidate validation failed."),
    PUBLISH_CANDIDATE: ("publishing", "The worker could not finish publishing the draft PR."),
}


def candidate_failure_message(session, run_id: UUID) -> str | None:
    """Explain a policy stop using only the recorded path, never file contents."""
    payload = session.scalar(
        select(RunEvent.payload)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == "CANDIDATE_GENERATION_FAILED")
        .order_by(RunEvent.id.desc())
        .limit(1)
    )
    if not isinstance(payload, dict) or payload.get("code") != "candidate_source_withheld":
        return None
    path = payload.get("path")
    if not isinstance(path, str):
        return None
    return (
        f"{path} may contain a credential, so it was not sent to the model. "
        "Remove the credential from that file, then start a new run."
    )


def _candidate_progress(
    activity: tuple[str, str] | None,
    result: CandidateResultRead | None,
    failure_message: str | None = None,
) -> CandidateProgressRead:
    if activity is None or (result and isinstance(result.publication, PublicationDraftRead)):
        return CandidateProgressRead(status="idle", result=result)
    kind, status = activity
    if status == "DONE" or (result and result.publication_target and kind != PUBLISH_CANDIDATE):
        return CandidateProgressRead(status="idle", result=result)
    stage, failure = CANDIDATE_STAGES[kind]
    if kind == VALIDATE_CANDIDATE and result:
        stage, failure = "preparing", "The worker could not prepare the reviewed commit."
    if status == "FAILED":
        return CandidateProgressRead(
            status="failed", message=failure_message or failure, result=result
        )
    return CandidateProgressRead(status=stage, result=result)


class PlanApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    revision: int
    sha256: str
    approved_by: int
    approved_at: datetime


class RepositoryPlanRead(BaseModel):
    artifact: PlanArtifact
    content: PlanContent
    readiness: PlanReadiness
    approval: PlanApprovalRead | None
    feedback_status: Literal["pending", "failed", "unchanged"] | None = None


class PlanRevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=1, le=2_147_483_647)
    proposal: PlanProposal


class PlanApprovalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID
    revision: int = Field(strict=True, ge=1, le=2_147_483_647)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _plan_response(
    session: Session, plan: RunPlan, response: Response, *, latest: bool = False
) -> RepositoryPlanRead:
    approval = session.get(RunPlanApproval, plan.run_id)
    feedback_status = plan_feedback_status(session, plan) if approval is None else None
    if latest and feedback_status == "DONE":
        current = load_refactoring_plan(session, plan.run_id)
        if current is not None and current.revision > plan.revision:
            return _plan_response(session, current, response)
    try:
        content = PlanContent.model_validate_json(plan.document)
    except ValidationError:
        raise HTTPException(
            500, {"code": "plan_unavailable", "message": "Saved plan could not be read."}
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return RepositoryPlanRead(
        artifact=PlanArtifact(
            id=plan.id,
            run_id=plan.run_id,
            revision=plan.revision,
            sha256=plan.sha256,
            size=len(plan.document),
            created_at=plan.created_at,
        ),
        content=content,
        readiness=evaluate_plan_readiness(content),
        feedback_status={
            "PENDING": "pending",
            "CLAIMED": "pending",
            "FAILED": "failed",
            "DONE": "unchanged",
        }.get(feedback_status),
        approval=(
            PlanApprovalRead.model_validate(approval)
            if approval is not None
            and (approval.revision, approval.sha256) == (plan.revision, plan.sha256)
            else None
        ),
    )


def create_repository_router(session_factory, *, enable_analysis=False) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Repositories"])
    oauth = GitHubOAuthConfig.from_env()

    def error(status, detail):
        return JSONResponse(
            status_code=status, content={"error": detail}, headers={"Cache-Control": "no-store"}
        )

    def authorize(request, repository_id, *, expected_installation=None):
        try:
            policy = RepositoryAccessPolicy.from_env()
            with session_factory() as session:
                identity = policy.require_eligible_session(
                    session, request.cookies.get(oauth.session_cookie_name), repository_id
                )
            installation_id = int(os.environ["REFACTRA_GITHUB_INSTALLATION_ID"])
            client_id = os.environ["REFACTRA_GITHUB_CLIENT_ID"].strip()
            if installation_id <= 0 or not client_id:
                raise ValueError("Invalid App configuration")
            if expected_installation is not None and installation_id != expected_installation:
                raise HTTPException(403, "Repository installation changed.")
            app = GitHubAppClient(
                client_id,
                Path(os.environ["REFACTRA_GITHUB_PRIVATE_KEY_PATH"]).read_bytes(),
            )
        except (OSError, ValueError, KeyError):
            raise HTTPException(
                503,
                {
                    "code": "repository_access_unconfigured",
                    "message": "Repository access configuration is unavailable.",
                },
            ) from None
        token = require_github_write_access(app, installation_id, repository_id, identity)
        return identity, installation_id, token, app.transport

    @router.get("/repositories/{repository_id}", response_model=RepositoryRead)
    def repository(repository_id: int, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            _, _, token, transport = authorize(request, repository_id)
            with httpx.Client(
                timeout=10.0, follow_redirects=False, trust_env=False, transport=transport
            ) as client:
                result = client.get(
                    f"https://api.github.com/repositories/{repository_id}",
                    headers={
                        "Authorization": f"Bearer {token.value}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                )
            result.raise_for_status()
            data = result.json()
            if type(data["id"]) is not int or data["id"] != repository_id:
                raise ValueError("Unexpected repository identity")
            return RepositoryRead(
                repository_id=repository_id,
                full_name=data["full_name"],
                default_branch=data["default_branch"],
            )
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)
        except (GitHubAppError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return error(
                502,
                {
                    "code": "github_unavailable",
                    "message": "Repository access could not be verified.",
                },
            )

    def submit_analysis(
        body: RepositoryRunCreate,
        request: Request,
        response: Response,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ):
        if not is_trusted_mutation(request, oauth):
            return error(
                403, {"code": "csrf_rejected", "message": "Submission requires a trusted origin."}
            )
        try:
            identity, installation_id, token, transport = authorize(request, body.repository_id)
            run_id = uuid5(NAMESPACE_URL, f"refactra:analysis:{identity.user_id}:{idempotency_key}")
            with session_factory() as session:
                run = session.get(Run, run_id)
            if run is None:
                revision = resolve_revision(
                    token, base_branch=body.base_branch, transport=transport
                )
                try:
                    with session_factory.begin() as session:
                        run = create_repository_run(
                            session,
                            identity=identity,
                            installation_id=installation_id,
                            revision=revision,
                            task_text=body.task_text,
                            run_id=run_id,
                        )
                except IntegrityError:
                    # A concurrent request may have won the pin with this key.
                    with session_factory() as session:
                        run = session.get(Run, run_id)
                    if run is None:
                        raise
            if (
                run.workflow_version != WORKFLOW
                or run.requested_by != identity.user_id
                or run.github_installation_id != installation_id
                or run.github_repository_id != body.repository_id
                or run.base_branch != body.base_branch
                or run.task_text != body.task_text
            ):
                return error(
                    409,
                    {
                        "code": "idempotency_conflict",
                        "message": "Use a new key for a different run.",
                    },
                )
            with session_factory() as session:
                saved = session.get(RunAnalysisContext, run_id) is not None
            if not saved:
                snapshot = fetch_source_snapshot(run, token, transport=transport)
                context = prepare_analysis_context(run, snapshot)
            else:
                context = None
            with session_factory.begin() as session:
                RepositoryAccessPolicy.from_env().require_eligible_session(
                    session, request.cookies.get(oauth.session_cookie_name), body.repository_id
                )
                session.get(Run, run_id, with_for_update=True)
                if context is not None and session.get(RunAnalysisContext, run_id) is None:
                    save_analysis_context(session, context)
                enqueue_repository_analysis(session, run_id)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Location"] = f"/api/v1/runs/{run_id}"
            return RepositoryRunReceipt(
                run_id=run_id,
                repository_id=run.github_repository_id,
                base_branch=run.base_branch,
                base_sha=run.base_sha,
                created_at=run.created_at,
            )
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)
        except GitHubAppError:
            return error(
                502,
                {
                    "code": "github_unavailable",
                    "message": "Could not verify or load the repository.",
                },
            )
        except ValueError:
            return error(
                422,
                {
                    "code": "analysis_context_invalid",
                    "message": "Could not prepare analysis input.",
                },
            )

    def require_run_access(request: Request, run_id: UUID) -> int:
        with session_factory() as session:
            identity = resolve_session(session, request.cookies.get(oauth.session_cookie_name))
            if identity is None:
                raise HTTPException(
                    401, {"code": "authentication_required", "message": "Sign in to access runs."}
                )
            binding = session.execute(
                select(Run.github_repository_id, Run.github_installation_id).where(
                    Run.id == run_id,
                    Run.requested_by == identity.user_id,
                    Run.workflow_version == WORKFLOW,
                )
            ).one_or_none()
        if binding is None:
            raise HTTPException(404, RUN_NOT_FOUND)
        repository_id, installation_id = binding
        try:
            authorize(request, repository_id, expected_installation=installation_id)
        except HTTPException as exc:
            if exc.status_code == 403:
                raise HTTPException(404, RUN_NOT_FOUND) from None
            raise
        except GitHubAppError:
            raise HTTPException(
                502,
                {
                    "code": "github_unavailable",
                    "message": "Repository access could not be verified.",
                },
            ) from None
        return repository_id

    @router.get("/runs/{run_id}", response_model=RepositoryRunRead)
    def read_analysis(run_id: UUID, request: Request, response: Response):
        try:
            require_run_access(request, run_id)
            with session_factory() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return error(404, RUN_NOT_FOUND)
                response.headers["Cache-Control"] = "no-store"
                return RepositoryRunRead(
                    run_id=run.id,
                    repository_id=run.github_repository_id,
                    base_branch=run.base_branch,
                    base_sha=run.base_sha,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                    status=run.status,
                    stop_reason=(
                        _analysis_stop_reason(session, run_id) if run.status == "FAILED" else None
                    ),
                )
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)

    @router.get("/runs/{run_id}/plan", response_model=RepositoryPlanRead)
    def read_plan(
        run_id: UUID,
        request: Request,
        response: Response,
        revision: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
    ):
        try:
            require_run_access(request, run_id)
            with session_factory() as session:
                status = None
                if revision is None:
                    status = session.scalar(
                        select(RunCommand.status)
                        .where(RunCommand.run_id == run_id, RunCommand.kind == GENERATE_PLAN)
                        .order_by(RunCommand.created_at)
                        .limit(1)
                    )
                plan = load_refactoring_plan(session, run_id, revision=revision)
                if plan is None:
                    if status in {"PENDING", "CLAIMED"}:
                        return error(404, {"code": "plan_pending", "message": "Preparing plan."})
                    if status == "FAILED":
                        return error(
                            409,
                            {
                                "code": "plan_generation_failed",
                                "message": "The worker could not generate a plan.",
                            },
                        )
                    return error(
                        404, {"code": "plan_not_found", "message": "Plan revision not found."}
                    )
                return _plan_response(session, plan, response, latest=revision is None)
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)

    @router.get("/runs/{run_id}/result", response_model=CandidateResultRead | CandidateProgressRead)
    def read_candidate_result(
        run_id: UUID,
        request: Request,
        response: Response,
        revision: Annotated[int | None, Query(ge=1, le=2_147_483_647)] = None,
        progress: bool = False,
    ):
        try:
            require_run_access(request, run_id)
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)

        with session_factory() as session:
            # Read the queue first. A finished job's artifacts must already be visible.
            activity = None
            live = progress and revision is None
            if live:
                row = session.execute(
                    select(RunCommand.kind, RunCommand.status)
                    .where(RunCommand.run_id == run_id, RunCommand.kind.in_(CANDIDATE_STAGES))
                    .order_by(RunCommand.created_at.desc(), RunCommand.id.desc())
                    .limit(1)
                ).one_or_none()
                if row is not None:
                    activity = (row.kind, row.status)
            failure_message = (
                candidate_failure_message(session, run_id)
                if activity == (GENERATE_CANDIDATE, "FAILED")
                else None
            )
            query = select(RunCandidateResult).where(RunCandidateResult.run_id == run_id)
            if revision is not None:
                query = query.where(RunCandidateResult.revision == revision)
            result = session.scalar(query.order_by(RunCandidateResult.revision.desc()).limit(1))
            if result is None:
                if live:
                    response.headers["Cache-Control"] = "no-store"
                    return _candidate_progress(activity, None, failure_message)
                return error(
                    404,
                    {
                        "code": "candidate_result_not_found",
                        "message": "Candidate result not found.",
                    },
                )

            try:
                saved = json.loads(result.document)
                stored_target = session.get(RunPublicationTarget, result.id)
                target = (
                    PublicationTargetRead(
                        sha256=stored_target.sha256, **json.loads(stored_target.document)
                    )
                    if stored_target is not None
                    else None
                )
                approval = load_publication_approval(session, run_id)
                publication = None
                if (
                    target is not None
                    and approval is not None
                    and (approval.result_id, approval.result_revision, approval.target_sha256)
                    == (result.id, result.revision, target.sha256)
                ):
                    publication = load_publication_progress(
                        session,
                        run_id,
                        target_sha256=target.sha256,
                        commit_sha=target.commit_sha,
                        head_branch=target.destination.head_branch,
                    )
                payload = CandidateResultRead(
                    id=result.id,
                    run_id=run_id,
                    revision=result.revision,
                    sha256=result.sha256,
                    created_at=result.created_at,
                    repository=saved["repository"],
                    approved_plan=saved["approved_plan"],
                    diff=saved["diff"],
                    diff_sha256=saved["diff_sha256"],
                    validation=saved["validation"],
                    review=saved.get("review"),
                    publication_target=target,
                    publication_approval=approval,
                    publication_waivers=load_publication_waivers(session, run_id, result),
                    publication=publication,
                )
            except (ValueError, KeyError, TypeError):
                return error(
                    500,
                    {
                        "code": "candidate_result_unavailable",
                        "message": "Saved result could not be read.",
                    },
                )
            response.headers["Cache-Control"] = "no-store"
            return _candidate_progress(activity, payload, failure_message) if live else payload

    @router.post("/runs/{run_id}/result/approve", response_model=PublicationApproval)
    def approve_result(
        run_id: UUID, body: PublicationApprovalCreate, request: Request, response: Response
    ):
        if not is_trusted_mutation(request, oauth):
            return error(
                403,
                {
                    "code": "csrf_rejected",
                    "message": "Publication approval requires a trusted origin.",
                },
            )
        try:
            repository_id = require_run_access(request, run_id)
            with session_factory.begin() as session:
                actor = RepositoryAccessPolicy.from_env().require_eligible_session(
                    session, request.cookies.get(oauth.session_cookie_name), repository_id
                )
                approval = approve_publication_target(
                    session, run_id, **body.model_dump(), actor_id=actor.user_id
                )
            response.headers["Cache-Control"] = "no-store"
            return approval
        except HTTPException as exc:
            return (
                error(404, RUN_NOT_FOUND)
                if exc.status_code == 403
                else error(exc.status_code, exc.detail)
            )
        except PublicationTargetError as exc:
            return error(409, {"code": "publication_approval_conflict", "message": str(exc)})

    @router.post("/runs/{run_id}/result/waivers", response_model=PublicationWaiver)
    def waive_result_check(
        run_id: UUID, body: PublicationWaiverCreate, request: Request, response: Response
    ):
        if not is_trusted_mutation(request, oauth):
            return error(
                403,
                {"code": "csrf_rejected", "message": "Check waivers require a trusted origin."},
            )
        try:
            repository_id = require_run_access(request, run_id)
            with session_factory.begin() as session:
                actor = RepositoryAccessPolicy.from_env().require_eligible_session(
                    session, request.cookies.get(oauth.session_cookie_name), repository_id
                )
                waiver = waive_publication_check(
                    session, run_id, **body.model_dump(), actor_id=actor.user_id
                )
            response.headers["Cache-Control"] = "no-store"
            return waiver
        except HTTPException as exc:
            return (
                error(404, RUN_NOT_FOUND)
                if exc.status_code == 403
                else error(exc.status_code, exc.detail)
            )
        except PublicationTargetError as exc:
            return error(409, {"code": "publication_waiver_conflict", "message": str(exc)})

    @router.post("/runs/{run_id}/plan", response_model=RepositoryPlanRead)
    def revise_plan(
        run_id: UUID, body: PlanRevisionCreate | PlanFeedback, request: Request, response: Response
    ):
        if not is_trusted_mutation(request, oauth):
            return error(
                403,
                {"code": "csrf_rejected", "message": "Plan revision requires a trusted origin."},
            )
        try:
            repository_id = require_run_access(request, run_id)
            with session_factory.begin() as session:
                RepositoryAccessPolicy.from_env().require_eligible_session(
                    session, request.cookies.get(oauth.session_cookie_name), repository_id
                )
                if isinstance(body, PlanFeedback):
                    return _plan_response(
                        session, queue_plan_feedback(session, run_id, body), response
                    )
                session.get(Run, run_id, with_for_update=True)
                current = load_refactoring_plan(session, run_id)
                if current and plan_feedback_status(session, current) in {"PENDING", "CLAIMED"}:
                    raise PlanRevisionConflict("Wait for the updated plan before saving edits.")
                plan = save_refactoring_plan(
                    session,
                    run_id,
                    body.proposal.model_dump(),
                    expected_revision=body.expected_revision,
                )
                return _plan_response(session, plan, response)
        except HTTPException as exc:
            return (
                error(404, RUN_NOT_FOUND)
                if exc.status_code == 403
                else error(exc.status_code, exc.detail)
            )
        except PlanRevisionConflict as exc:
            return error(409, {"code": "plan_revision_conflict", "message": str(exc)})
        except ValueError:
            return error(
                422, {"code": "plan_unavailable", "message": "The draft could not be saved."}
            )

    @router.post("/runs/{run_id}/plan/approve", response_model=PlanApprovalRead)
    def approve_plan(run_id: UUID, body: PlanApprovalCreate, request: Request, response: Response):
        if not is_trusted_mutation(request, oauth):
            return error(
                403,
                {"code": "csrf_rejected", "message": "Plan approval requires a trusted origin."},
            )
        try:
            repository_id = require_run_access(request, run_id)
            with session_factory.begin() as session:
                actor = RepositoryAccessPolicy.from_env().require_eligible_session(
                    session, request.cookies.get(oauth.session_cookie_name), repository_id
                )
                approval = approve_refactoring_plan(
                    session, run_id, **body.model_dump(), actor_id=actor.user_id
                )
                response.headers["Cache-Control"] = "no-store"
                return PlanApprovalRead.model_validate(approval)
        except HTTPException as exc:
            return (
                error(404, RUN_NOT_FOUND)
                if exc.status_code == 403
                else error(exc.status_code, exc.detail)
            )
        except PlanRevisionConflict as exc:
            return error(409, {"code": "plan_approval_conflict", "message": str(exc)})

    if enable_analysis:
        router.add_api_route(
            "/runs",
            submit_analysis,
            methods=["POST"],
            status_code=202,
            response_model=RepositoryRunReceipt,
        )

    return router
