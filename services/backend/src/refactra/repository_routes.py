"""Protected repository metadata and read-only run submission."""

import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from refactra.analysis_context import prepare_analysis_context, save_analysis_context
from refactra.github_app import GitHubAppClient, GitHubAppError
from refactra.github_auth import GitHubOAuthConfig
from refactra.github_repository_access import require_github_write_access
from refactra.models import Run, RunAnalysisContext
from refactra.repository_access import RepositoryAccessPolicy
from refactra.repository_analysis_worker import WORKFLOW, enqueue_repository_analysis
from refactra.repository_revision import resolve_revision
from refactra.repository_runs import create_repository_run
from refactra.repository_snapshot import fetch_source_snapshot
from refactra.schemas import RepositoryRunCreate, RepositoryRunReceipt
from refactra.session_routes import is_trusted_mutation


class RepositoryRead(BaseModel):
    repository_id: int
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_branch: str = Field(min_length=1)
    access: Literal["read-only"] = "read-only"


def create_repository_router(session_factory, *, enable_analysis=False) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["Repositories"])
    oauth = GitHubOAuthConfig.from_env()

    def error(status, detail):
        return JSONResponse(
            status_code=status, content={"error": detail}, headers={"Cache-Control": "no-store"}
        )

    def authorize(request, repository_id):
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

    if enable_analysis:
        router.add_api_route(
            "/runs",
            submit_analysis,
            methods=["POST"],
            status_code=202,
            response_model=RepositoryRunReceipt,
        )

    return router
