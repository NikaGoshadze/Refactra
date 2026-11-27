"""POST /api/v1/github/webhook: GitHub App events that end repository access.

When the App is uninstalled or suspended, or a repository is removed from an installation,
the affected unfinished runs are stopped at once with a clear reason. The workers would refuse
them anyway at their next access re-check; this makes it immediate and visible.

Every delivery must carry a valid X-Hub-Signature-256 for REFACTRA_GITHUB_WEBHOOK_SECRET.
Point the App's webhook straight at the API, not through the web proxy, so the signed bytes
arrive unchanged. Handling is idempotent, so GitHub's redeliveries are harmless.
"""

import hmac
import json
import os
from hashlib import sha256

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from refactra.diagnostics import workflow_log
from refactra.models import Run, RunCommand, RunEvent
from refactra.publication_target import PUBLICATION_PR_OPENED
from refactra.request_limits import WEBHOOK_PATH

SECRET_ENV = "REFACTRA_GITHUB_WEBHOOK_SECRET"
WORKFLOW = "repository-v0.1"  # Same as repository_analysis_worker.WORKFLOW.
STOP_CODE = "github_access_removed"


def webhook_secret() -> str | None:
    return os.getenv(SECRET_ENV, "").strip() or None


def valid_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def stop_runs(session: Session, installation_id: int, repository_ids: list[int] | None) -> int:
    """Stop unfinished runs for an installation (or some of its repositories); return a count.

    Published and already failed runs are left as they are. Queued commands are failed; a
    command a worker already holds finishes or fails on its own access re-check.
    """
    published = exists().where(
        RunEvent.run_id == Run.id, RunEvent.event_type == PUBLICATION_PR_OPENED
    )
    statement = select(Run).where(
        Run.workflow_version == WORKFLOW,
        Run.github_installation_id == installation_id,
        Run.status != "FAILED",
        ~published,
    )
    if repository_ids is not None:
        statement = statement.where(Run.github_repository_id.in_(repository_ids))
    runs = session.scalars(statement.with_for_update()).all()
    for run in runs:
        run.status = "FAILED"
        run.version += 1
        session.add(RunEvent(run_id=run.id, event_type="RUN_STOPPED", payload={"code": STOP_CODE}))
    if runs:
        session.execute(
            update(RunCommand)
            .where(RunCommand.run_id.in_([run.id for run in runs]), RunCommand.status == "PENDING")
            .values(status="FAILED")
        )
    return len(runs)


def _positive_ids(values) -> list[int]:
    ids = [item.get("id") for item in values if isinstance(item, dict)]
    return [value for value in ids if type(value) is int and value > 0]


def create_github_webhook_router(session_factory) -> APIRouter:
    router = APIRouter(tags=["GitHub webhooks"])

    def reply(status: int, body: dict) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @router.post(WEBHOOK_PATH)
    async def github_webhook(request: Request):
        secret = webhook_secret()
        if secret is None:
            return reply(
                503, {"error": {"code": "webhook_unconfigured", "message": "Webhooks are off."}}
            )
        body = await request.body()
        if not valid_signature(secret, body, request.headers.get("x-hub-signature-256")):
            return reply(
                401, {"error": {"code": "invalid_signature", "message": "Signature mismatch."}}
            )
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")[:64]
        try:
            payload = json.loads(body)
            action = payload.get("action") if isinstance(payload, dict) else None
            installation = payload["installation"]["id"] if event != "ping" else None
        except (ValueError, KeyError, TypeError):
            return reply(
                400, {"error": {"code": "invalid_payload", "message": "Unreadable event."}}
            )
        stopped = 0
        if event == "installation" and action in {"deleted", "suspend"}:
            repositories = None
        elif event == "installation_repositories" and action == "removed":
            repositories = _positive_ids(payload.get("repositories_removed") or [])
        else:
            workflow_log("github_webhook", github_event=event, action=action, delivery=delivery)
            return reply(202, {"status": "ignored"})
        if type(installation) is not int or installation <= 0:
            return reply(
                400, {"error": {"code": "invalid_payload", "message": "Unreadable event."}}
            )
        with session_factory.begin() as session:
            stopped = stop_runs(session, installation, repositories)
        workflow_log(
            "github_webhook",
            github_event=event,
            action=action,
            delivery=delivery,
            installation_id=installation,
            stopped_runs=stopped,
        )
        return reply(200, {"status": "processed", "stopped_runs": stopped})

    return router
