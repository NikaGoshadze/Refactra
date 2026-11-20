"""Protected repository metadata, analysis results, and plan revisions."""

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from refactra.analysis_context import prepare_analysis_context, save_analysis_context
from refactra.auth_sessions import resolve_session
from refactra.github_app import GitHubAppClient, GitHubAppError
from refactra.github_auth import GitHubOAuthConfig
from refactra.github_repository_access import require_github_write_access
from refactra.models import Run, RunAnalysisContext, RunAnalysisResult, RunEvent
from refactra.refactoring_plan import (
    PlanContent,
    PlanReadiness,
    evaluate_plan_readiness,
    load_refactoring_plan,
)
from refactra.repository_access import RepositoryAccessPolicy
from refactra.repository_analysis_worker import WORKFLOW, enqueue_repository_analysis
from refactra.repository_revision import resolve_revision
from refactra.repository_runs import create_repository_run
from refactra.repository_snapshot import fetch_source_snapshot
from refactra.schemas import (
    RepositoryRunCreate,
    RepositoryRunRead,
    RepositoryRunReceipt,
    StopReason,
)
from refactra.session_routes import is_trusted_mutation

ANALYSIS_FAILURE_MESSAGES = {
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


class PlanArtifact(BaseModel):
    id: UUID
    run_id: UUID
    kind: Literal["PLAN"] = "PLAN"
    revision: int
    sha256: str
    size: int
    created_at: datetime


class RepositoryPlanRead(BaseModel):
    artifact: PlanArtifact
    content: PlanContent
    readiness: PlanReadiness


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

    def require_run_access(request: Request, run_id: UUID) -> None:
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

    @router.get("/runs/{run_id}", response_model=RepositoryRunRead)
    def read_analysis(run_id: UUID, request: Request, response: Response):
        try:
            require_run_access(request, run_id)
            with session_factory() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return error(404, RUN_NOT_FOUND)
                result = (
                    session.get(RunAnalysisResult, run_id)
                    if run.status == "NEEDS_HUMAN_REVIEW"
                    else None
                )
                response.headers["Cache-Control"] = "no-store"
                return RepositoryRunRead(
                    run_id=run.id,
                    repository_id=run.github_repository_id,
                    base_branch=run.base_branch,
                    base_sha=run.base_sha,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                    status=run.status,
                    analysis_text=result.analysis_text if result is not None else None,
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
        except HTTPException as exc:
            return error(exc.status_code, exc.detail)

        with session_factory() as session:
            plan = load_refactoring_plan(session, run_id, revision=revision)
            if plan is None:
                return error(404, {"code": "plan_not_found", "message": "Plan revision not found."})
            document = plan.document
            artifact = PlanArtifact(
                id=plan.id,
                run_id=plan.run_id,
                revision=plan.revision,
                sha256=plan.sha256,
                size=len(document),
                created_at=plan.created_at,
            )
        try:
            content = PlanContent.model_validate_json(document)
        except ValidationError:
            return error(
                500, {"code": "plan_unavailable", "message": "Saved plan could not be read."}
            )
        response.headers["Cache-Control"] = "no-store"
        return RepositoryPlanRead(
            artifact=artifact, content=content, readiness=evaluate_plan_readiness(content)
        )

    if enable_analysis:
        router.add_api_route(
            "/runs",
            submit_analysis,
            methods=["POST"],
            status_code=202,
            response_model=RepositoryRunReceipt,
        )

    return router
