"""POST /api/v1/account/delete: remove a user's Refactra data.

Deleted: tasks, saved source excerpts, analyses, plans, generated changes, validation results,
publication targets, run events and queued commands, every session (with its encrypted GitHub
token) and the user's invite row. The worker removes the user's prepared artifacts.

The shared budget keeps run IDs and model cost records with the account and repository
fields cleared. Published branches and draft pull requests belong to the GitHub repository.
"""

import re
import shutil
import stat
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from refactra.auth_sessions import resolve_session
from refactra.diagnostics import workflow_log
from refactra.github_auth import GitHubOAuthConfig
from refactra.models import (
    AuthSession,
    Run,
    RunAnalysisContext,
    RunAnalysisResult,
    RunCandidateResult,
    RunCommand,
    RunEvent,
    RunGeneratedCandidate,
    RunPlan,
    RunPlanApproval,
    RunPublicationTarget,
    UserAccess,
)
from refactra.repository_analysis_worker import WORKFLOW
from refactra.session_routes import is_trusted_mutation

EMPTY_CONTEXT = {"schema_version": "analysis-context-v1"}
ACCOUNT_LOCK = 72341003  # Shared with run submission.


class AccountDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Typing the login confirms the user means it.
    confirm_login: str = Field(min_length=1, max_length=255)


class RunInProgress(Exception):
    """A worker holds one of the user's commands right now."""


def lock_account(session: Session, user_id: int) -> None:
    session.execute(select(func.pg_advisory_xact_lock(ACCOUNT_LOCK, user_id % 2**31)))


def delete_account(session: Session, user_id: int) -> int:
    """Delete the user's data in the caller's transaction; return how many runs were cleared."""
    try:
        with session.begin_nested():
            run_ids = session.scalars(
                select(Run.id)
                .where(Run.requested_by == user_id, Run.workflow_version == WORKFLOW)
                .with_for_update(nowait=True)
            ).all()
            # A pending command must stay unclaimable until the deletion commits.
            states = session.scalars(
                select(RunCommand.status)
                .where(
                    RunCommand.run_id.in_(run_ids), RunCommand.status.in_(("PENDING", "CLAIMED"))
                )
                .with_for_update(nowait=True)
            ).all()
            if "CLAIMED" in states:
                raise RunInProgress()
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":  # lock_not_available
            raise RunInProgress() from exc
        raise
    if run_ids:
        results = select(RunCandidateResult.id).where(RunCandidateResult.run_id.in_(run_ids))
        # Children before parents: the foreign keys do not cascade.
        session.execute(
            delete(RunPublicationTarget).where(RunPublicationTarget.result_id.in_(results))
        )
        for table in (
            RunCandidateResult,
            RunGeneratedCandidate,
            RunPlanApproval,
            RunPlan,
            RunAnalysisResult,
            RunEvent,
            RunCommand,
        ):
            session.execute(delete(table).where(table.run_id.in_(run_ids)))
        # The context and its model cost entries stay so the monthly budget remains accurate,
        # but the saved source excerpts are removed.
        session.execute(
            update(RunAnalysisContext)
            .where(RunAnalysisContext.run_id.in_(run_ids))
            .values(payload=EMPTY_CONTEXT)
        )
        session.execute(
            update(Run)
            .where(Run.id.in_(run_ids))
            .values(
                requested_by=0,
                github_installation_id=0,
                github_repository_id=0,
                repository_full_name=None,
                base_branch="deleted",
                base_sha="0" * 40,
                task_text="Deleted by its owner.",
                status="FAILED",
                limits={},
                usage={},
            )
        )
    session.execute(delete(AuthSession).where(AuthSession.github_user_id == user_id))
    session.execute(delete(UserAccess).where(UserAccess.github_user_id == user_id))
    return len(run_ids)


def clean_deleted_artifacts(session_factory, directory: Path) -> None:
    """Reconcile the worker's own artifact root against committed account deletions."""
    with session_factory() as session:
        deleted = set(
            map(
                str,
                session.scalars(
                    select(Run.id).where(Run.workflow_version == WORKFLOW, Run.requested_by == 0)
                ),
            )
        )
    if not deleted:
        return
    root = directory.resolve(strict=True)

    def retry_readonly(operation, name, error):
        path = Path(name)
        if not isinstance(error, PermissionError) or not path.resolve().is_relative_to(root):
            raise error
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
        operation(name)

    for item in root.iterdir():
        run_id = item.name[:36]
        if run_id not in deleted or not re.fullmatch(r"(?:-r[1-9][0-9]*|\.asset)", item.name[36:]):
            continue
        try:
            if item.is_symlink():
                item.unlink()
            elif item.is_junction():
                item.rmdir()
            elif item.resolve().parent == root:
                if item.is_dir():
                    shutil.rmtree(item, onexc=retry_readonly)
                else:
                    item.unlink(missing_ok=True)
        except OSError as exc:
            workflow_log(
                "account_artifact_cleanup_failed",
                warning=True,
                run_id=run_id,
                error_type=type(exc).__name__,
            )


def create_account_router(session_factory) -> APIRouter:
    router = APIRouter(prefix="/api/v1/account", tags=["Account"])
    oauth = GitHubOAuthConfig.from_env()

    def error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/delete")
    def delete_my_account(body: AccountDeletion, request: Request):
        if not is_trusted_mutation(request, oauth):
            return error(403, "csrf_rejected", "Account deletion requires a trusted origin.")
        with session_factory.begin() as session:
            token = request.cookies.get(oauth.session_cookie_name)
            identity = resolve_session(session, token)
            if identity is not None:
                lock_account(session, identity.user_id)
                identity = resolve_session(session, token)
            if identity is None:
                return error(401, "authentication_required", "Sign in to delete your account.")
            if body.confirm_login.strip().lower() != identity.login.lower():
                return error(422, "confirmation_mismatch", "Type your GitHub login to confirm.")
            try:
                cleared = delete_account(session, identity.user_id)
            except RunInProgress:
                return error(
                    409,
                    "run_in_progress",
                    "A worker is processing one of your runs. Try again in a minute.",
                )
        response = JSONResponse(
            {"deleted": True, "runs_cleared": cleared}, headers={"Cache-Control": "no-store"}
        )
        response.delete_cookie(
            oauth.session_cookie_name,
            path="/",
            secure=oauth.secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return response

    return router
